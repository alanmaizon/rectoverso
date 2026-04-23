"""Tests for the Editor artifact upload endpoint.

The endpoint is the only bidirectional channel we have between the
Managed Agents sandbox and our host (see
research/managed_agents_platform_api.md § Day 5 probe findings for why).
It MUST reject everything that isn't a live, sig-valid, session-scoped
bearer request — a leaked or forged token substituting a malicious MP4
for a legitimate render is the primary threat model.

Coverage:
    - Token minting + verification round-trips
    - Tampering with session_id / expiry / signature all fail verify
    - Expired tokens are rejected
    - Endpoint happy path: sha256 + size + filename + session_id returned
    - Endpoint rejects: missing Authorization, malformed bearer, bad sig,
      expired, session_id-mismatch, missing multipart field, unsafe filename
    - MAX_CONTENT_LENGTH rejects oversize bodies with 413
    - Storage path is scoped to <storage_root>/<session_id>/; cannot escape
    - Health endpoint is unauthenticated, returns 200 + ok:true
    - Different secrets don't cross-validate
"""

from __future__ import annotations

import hashlib
import io
import time
from pathlib import Path

import pytest

from src.producer.upload_endpoint import (
    MAX_UPLOAD_BYTES,
    UploadToken,
    create_app,
    generate_secret,
    mint_token,
    verify_token,
)


# ---------------------------------------------------------------------------
# Token layer
# ---------------------------------------------------------------------------


def test_mint_and_verify_round_trip() -> None:
    secret = b"a" * 32
    minted = mint_token("sess_abc123", secret, ttl_s=60)
    verified = verify_token(minted.encode(), secret)
    assert verified.session_id == "sess_abc123"
    assert verified.expiry_ts == minted.expiry_ts
    assert verified.signature_hex == minted.signature_hex


def test_tampered_session_id_rejected() -> None:
    secret = b"a" * 32
    minted = mint_token("sess_abc", secret, ttl_s=60)
    tampered = f"sess_xyz.{minted.expiry_ts}.{minted.signature_hex}"
    with pytest.raises(ValueError, match="signature mismatch"):
        verify_token(tampered, secret)


def test_tampered_expiry_rejected() -> None:
    secret = b"a" * 32
    minted = mint_token("sess_abc", secret, ttl_s=60)
    later = minted.expiry_ts + 10_000
    tampered = f"{minted.session_id}.{later}.{minted.signature_hex}"
    with pytest.raises(ValueError, match="signature mismatch"):
        verify_token(tampered, secret)


def test_tampered_signature_rejected() -> None:
    secret = b"a" * 32
    minted = mint_token("sess_abc", secret, ttl_s=60)
    bad_sig = "0" * len(minted.signature_hex)
    tampered = f"{minted.session_id}.{minted.expiry_ts}.{bad_sig}"
    with pytest.raises(ValueError, match="signature mismatch"):
        verify_token(tampered, secret)


def test_expired_token_rejected() -> None:
    secret = b"a" * 32
    minted = mint_token("sess_abc", secret, ttl_s=-1)  # already expired
    with pytest.raises(ValueError, match="expired"):
        verify_token(minted.encode(), secret)


def test_different_secret_does_not_validate() -> None:
    minted = mint_token("sess_abc", b"a" * 32, ttl_s=60)
    with pytest.raises(ValueError, match="signature mismatch"):
        verify_token(minted.encode(), b"b" * 32)


def test_malformed_token_rejected() -> None:
    with pytest.raises(ValueError, match="malformed"):
        verify_token("", b"a" * 32)
    with pytest.raises(ValueError, match="malformed"):
        verify_token("just-a-string", b"a" * 32)
    with pytest.raises(ValueError, match="3 parts"):
        verify_token("too.many.parts.here.yes", b"a" * 32)


def test_unsafe_session_id_rejected_at_mint() -> None:
    secret = b"a" * 32
    with pytest.raises(ValueError, match="disallowed"):
        mint_token("sess/../abc", secret)
    with pytest.raises(ValueError, match="disallowed"):
        mint_token("sess with spaces", secret)
    with pytest.raises(ValueError, match="non-empty"):
        mint_token("", secret)


def test_generate_secret_is_32_bytes_and_random() -> None:
    s1 = generate_secret()
    s2 = generate_secret()
    assert len(s1) == 32
    assert s1 != s2


# ---------------------------------------------------------------------------
# Endpoint — happy path
# ---------------------------------------------------------------------------


@pytest.fixture
def secret() -> bytes:
    return b"test-secret-32-bytes-long-padding"


@pytest.fixture
def app(tmp_path: Path, secret: bytes):
    flask_app = create_app(storage_root=tmp_path, secret=secret)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _post_file(
    client, session_id: str, token: str, *, filename: str = "out.mp4",
    body: bytes = b"fake-mp4-bytes",
):
    return client.post(
        f"/upload/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
        data={"file": (io.BytesIO(body), filename)},
        content_type="multipart/form-data",
    )


def test_upload_happy_path_returns_sha256_and_size(
    client, secret: bytes, tmp_path: Path
) -> None:
    sid = "sess_happy"
    body = b"pretend-this-is-mp4-bytes" * 100
    token = mint_token(sid, secret).encode()

    resp = _post_file(client, sid, token, filename="out.mp4", body=body)

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["session_id"] == sid
    assert data["filename"] == "out.mp4"
    assert data["size_bytes"] == len(body)
    assert data["sha256"] == hashlib.sha256(body).hexdigest()
    # File actually written where reported
    assert (tmp_path / sid / "out.mp4").read_bytes() == body


def test_upload_session_dir_scoped(
    client, secret: bytes, tmp_path: Path
) -> None:
    """Files for different sessions land in separate subdirs. Key
    guarantee for the orchestrator: one dispatch never sees another's
    artifacts."""
    for sid in ("sess_a", "sess_b", "sess_c"):
        token = mint_token(sid, secret).encode()
        _post_file(client, sid, token, filename=f"{sid}.mp4", body=sid.encode())

    for sid in ("sess_a", "sess_b", "sess_c"):
        assert (tmp_path / sid / f"{sid}.mp4").read_bytes() == sid.encode()
    # No cross-contamination
    assert set(p.name for p in tmp_path.iterdir()) == {"sess_a", "sess_b", "sess_c"}


# ---------------------------------------------------------------------------
# Endpoint — rejection paths
# ---------------------------------------------------------------------------


def test_missing_authorization_header_rejected(client) -> None:
    resp = client.post(
        "/upload/sess_x",
        data={"file": (io.BytesIO(b"x"), "out.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 401
    assert "bearer" in resp.get_json()["error"].lower()


def test_malformed_bearer_scheme_rejected(client) -> None:
    resp = client.post(
        "/upload/sess_x",
        headers={"Authorization": "Basic not-bearer"},
        data={"file": (io.BytesIO(b"x"), "out.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 401


def test_bad_signature_rejected(client, secret: bytes) -> None:
    bad = "sess_x.9999999999.deadbeef"
    resp = _post_file(client, "sess_x", bad)
    assert resp.status_code == 401
    assert "rejected" in resp.get_json()["error"].lower()


def test_expired_token_rejected_at_endpoint(client, secret: bytes) -> None:
    expired = mint_token("sess_x", secret, ttl_s=-1).encode()
    resp = _post_file(client, "sess_x", expired)
    assert resp.status_code == 401
    assert "expired" in resp.get_json()["error"].lower()


def test_session_id_mismatch_rejected(client, secret: bytes) -> None:
    """Token minted for sess_a but POSTed to /upload/sess_b → 403.
    Key isolation guarantee: a leaked token for one session cannot
    substitute artifacts for another."""
    token = mint_token("sess_a", secret).encode()
    resp = _post_file(client, "sess_b", token)
    assert resp.status_code == 403
    err = resp.get_json()["error"]
    assert "sess_a" in err and "sess_b" in err


def test_missing_multipart_field_rejected(client, secret: bytes) -> None:
    token = mint_token("sess_x", secret).encode()
    resp = client.post(
        "/upload/sess_x",
        headers={"Authorization": f"Bearer {token}"},
        data={},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "file" in resp.get_json()["error"].lower()


def test_unsafe_filename_rejected(client, secret: bytes) -> None:
    token = mint_token("sess_x", secret).encode()
    # Path traversal attempt
    resp = _post_file(client, "sess_x", token, filename="../../../etc/passwd")
    assert resp.status_code == 400
    # Space in filename
    resp = _post_file(client, "sess_x", token, filename="with spaces.mp4")
    assert resp.status_code == 400
    # Slash
    resp = _post_file(client, "sess_x", token, filename="sub/path.mp4")
    assert resp.status_code == 400


def test_size_cap_enforced(tmp_path: Path, secret: bytes) -> None:
    """MAX_CONTENT_LENGTH triggers 413 before our handler runs. We build
    a fresh app with a tiny cap to test without actually allocating 200MB
    in memory."""
    app = create_app(storage_root=tmp_path, secret=secret)
    app.config["MAX_CONTENT_LENGTH"] = 1024  # 1KB cap for this test
    app.config["TESTING"] = True
    client = app.test_client()

    token = mint_token("sess_big", secret).encode()
    big_body = b"x" * 4096   # 4KB > 1KB cap
    resp = client.post(
        "/upload/sess_big",
        headers={"Authorization": f"Bearer {token}"},
        data={"file": (io.BytesIO(big_body), "big.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 413


def test_default_max_upload_is_200mb() -> None:
    """Spec invariant — 200MB cap. Renders up to 200MB accepted; anything
    larger is a configuration error that the operator must opt into."""
    assert MAX_UPLOAD_BYTES == 200 * 1024 * 1024


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


def test_health_endpoint_unauthenticated(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["service"] == "rectoverso-upload"


# ---------------------------------------------------------------------------
# App-factory invariants
# ---------------------------------------------------------------------------


def test_create_app_rejects_empty_secret(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        create_app(storage_root=tmp_path, secret=b"")


def test_apps_with_different_secrets_do_not_cross_validate(
    tmp_path: Path,
) -> None:
    """Minting under secret A and verifying under a secret-B app fails.
    Confirms the secret is load-bearing for inter-dispatch isolation."""
    secret_a = b"a" * 32
    secret_b = b"b" * 32
    token_a = mint_token("sess_x", secret_a).encode()

    app_b = create_app(storage_root=tmp_path, secret=secret_b)
    app_b.config["TESTING"] = True
    client_b = app_b.test_client()
    resp = _post_file(client_b, "sess_x", token_a)
    assert resp.status_code == 401
