"""Upload endpoint — artifact extraction from the Managed Agents sandbox.

The Q1 probe (2026-04-23, see research/managed_agents_platform_api.md
§ Day 5 probe findings) confirmed that the Anthropic Managed Agents
platform has no bidirectional file primitive on the current beta:
    - `sessions.resources.add()` returns valid resource ids but the
      container-side mount never materializes (24s grace period, empty
      /workspace/).
    - `files.download()` rejects every uploaded file with 400
      "not downloadable" regardless of `purpose` kwarg attempts.
    - No `artifacts`, `outputs`, `blobs`, or `storage` primitive exists
      anywhere in the SDK surface.

What IS available: outbound HTTPS from the container works (verified —
the probe's agent-side curl reached api.anthropic.com and got back
HTTP/2 with TLS + envoy routing intact). So the extraction path is:

    1. orchestrator spawns this Flask endpoint before dispatching the Editor
    2. ngrok exposes it publicly (see ngrok helper — separate commit)
    3. agent kickoff message carries the tunnel URL + a signed token
    4. Editor agent curls its tar.gz to POST /upload/<session_id>
    5. endpoint stores + returns sha256; agent echoes sha256 in EDITOR_RESULT
    6. orchestrator verifies the agent-reported sha256 matches the
       server-side sha256 before accepting the dispatch as successful

Single channel for everything binary — MP4, composition zip, any asset
bundle. The EDITOR_RESULT envelope stays metadata-only.

Security:
    - Bearer token is HMAC-SHA256 of `(session_id, expiry_ts)` with a
      per-dispatch secret generated at orchestrator startup. The secret
      never leaves the host process.
    - Session-id in the URL path MUST match the session-id in the token
      claims — prevents a leaked token from being reused for a different
      session's slot.
    - Max body size is enforced by Flask's MAX_CONTENT_LENGTH so we
      can't be OOMed by a runaway agent or hostile caller.
    - Files land under `<storage_root>/<session_id>/` — caller picks
      the root, which for rectoverso's orchestrator is
      `artifacts/edit/uploads/` so the existing clear_edit_artifacts
      invariant covers it on every transition into film_status=pending.
    - Filenames are regex-sanitized to prevent traversal or shell
      metacharacter bleed.

Not for production. This is a hackathon-scope artifact sink running on
the dev machine. Authentication is bearer-token-in-URL, not mTLS, and
the tunnel URL is short-lived by design (one dispatch).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, request

# Max size per upload — 200 MB. Bigger than any realistic 30-60s film
# (typical 5-20 MB), with headroom for composition assets bundled in.
# Flask enforces via MAX_CONTENT_LENGTH before our handler ever runs.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Token TTL — short. The endpoint only needs to be reachable during one
# Editor dispatch (hours at most); 6h is belt-and-braces.
DEFAULT_TOKEN_TTL_S = 6 * 3600

# Safe-filename regex — alphanumerics, `.`, `_`, `-` only. Rejects
# traversal (`..`) and path separators.
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# Token minting + verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadToken:
    """Parsed bearer token. Immutable; fields match what was signed.

    Format on the wire: `<session_id>.<expiry_ts>.<hmac_hex>`. Three
    components, dot-separated, URL-safe. The signature covers the first
    two components verbatim so tampering with either invalidates the hmac.
    """

    session_id: str
    expiry_ts: int
    signature_hex: str

    def encode(self) -> str:
        return f"{self.session_id}.{self.expiry_ts}.{self.signature_hex}"


def mint_token(session_id: str, secret: bytes, *, ttl_s: int = DEFAULT_TOKEN_TTL_S) -> UploadToken:
    """Mint a bearer token scoped to `session_id`, expiring in `ttl_s`.

    `secret` is a per-dispatch key the orchestrator generates once at
    startup via `secrets.token_bytes(32)` and holds in-process — never
    persisted, never logged. Callers pass the same secret to the Flask
    app factory so verification uses the matching key.
    """
    if not session_id:
        raise ValueError("session_id must be non-empty")
    if not _is_safe_session_id(session_id):
        raise ValueError(
            f"session_id contains disallowed characters: {session_id!r}. "
            "Only alphanumerics, '.', '_', '-' are allowed."
        )
    expiry = int(time.time()) + int(ttl_s)
    payload = f"{session_id}.{expiry}".encode("utf-8")
    sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return UploadToken(session_id=session_id, expiry_ts=expiry, signature_hex=sig)


def verify_token(token_str: str, secret: bytes) -> UploadToken:
    """Parse + validate a bearer token string. Raises ValueError with a
    specific reason on any failure (bad shape, bad sig, expired)."""
    if not token_str or "." not in token_str:
        raise ValueError("token missing or malformed (expected sid.exp.sig)")

    parts = token_str.split(".")
    if len(parts) != 3:
        raise ValueError(f"token must have 3 parts, got {len(parts)}")

    session_id, expiry_str, sig_hex = parts
    if not _is_safe_session_id(session_id):
        raise ValueError(f"session_id contains disallowed characters: {session_id!r}")

    try:
        expiry_ts = int(expiry_str)
    except ValueError as exc:
        raise ValueError(f"expiry is not an integer: {expiry_str!r}") from exc

    now = int(time.time())
    if expiry_ts <= now:
        raise ValueError(
            f"token expired at {expiry_ts}, now is {now} "
            f"(lagged by {now - expiry_ts}s)"
        )

    expected_payload = f"{session_id}.{expiry_ts}".encode("utf-8")
    expected_sig = hmac.new(secret, expected_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig_hex, expected_sig):
        raise ValueError("signature mismatch")

    return UploadToken(
        session_id=session_id, expiry_ts=expiry_ts, signature_hex=sig_hex
    )


def _is_safe_session_id(sid: str) -> bool:
    return bool(_SAFE_FILENAME.match(sid))


# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------


def create_app(*, storage_root: Path, secret: bytes) -> Flask:
    """Build a Flask app wired to a concrete storage root + HMAC secret.

    Callers (orchestrator's dispatch path, tests) construct one app per
    dispatch so the secret never outlives the session it was minted for.
    Reusing a secret across dispatches would be safe but gives no
    diagnostic value — rotate per-dispatch instead.
    """
    if not secret:
        raise ValueError("secret must be non-empty bytes")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    @app.post("/upload/<session_id>")
    def upload(session_id: str):  # noqa: ARG001 — captured from URL
        # 1. Pull bearer token.
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            abort(401, description="missing bearer token")
        raw_token = auth[len("Bearer "):].strip()

        # 2. Verify signature + expiry.
        try:
            token = verify_token(raw_token, secret)
        except ValueError as exc:
            abort(401, description=f"token rejected: {exc}")

        # 3. Bind token's session_id to URL path.
        if token.session_id != session_id:
            abort(
                403,
                description=(
                    f"token session_id {token.session_id!r} does not match "
                    f"URL session_id {session_id!r}"
                ),
            )

        # 4. Pull the multipart file. curl -F 'file=@...' sends field
        #    name "file" by convention; tests + editor_agent.md contract.
        if "file" not in request.files:
            abort(400, description="multipart field 'file' missing")
        file_storage = request.files["file"]

        # 5. Sanitize filename. Werkzeug's secure_filename is the canonical
        #    defense; we layer our own regex because secure_filename
        #    silently empties some edge cases (e.g., "..") and we want an
        #    explicit reject, not a silent drop.
        raw_name = file_storage.filename or ""
        if not _is_safe_session_id(raw_name):   # same charset rule
            abort(
                400,
                description=(
                    f"filename {raw_name!r} contains disallowed characters. "
                    "Allowed: alphanumerics, '.', '_', '-'."
                ),
            )

        # 6. Stream-write to disk, computing sha256 as we go. Avoids
        #    loading the whole body into memory (200MB would be fine
        #    but this is the right shape anyway).
        dest_dir = storage_root / session_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / raw_name

        hasher = hashlib.sha256()
        total_bytes = 0
        with open(dest_path, "wb") as out:
            while True:
                chunk = file_storage.stream.read(64 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                out.write(chunk)
                total_bytes += len(chunk)

        rel_path = os.path.relpath(dest_path, start=Path.cwd())

        return jsonify({
            "sha256": hasher.hexdigest(),
            "size_bytes": total_bytes,
            "stored_at": rel_path,
            "filename": raw_name,
            "session_id": session_id,
        })

    @app.get("/health")
    def health():
        """Liveness probe — the ngrok tunnel spawner uses this to confirm
        the app is up before returning the public URL. Unauthenticated
        by design; leaks no state."""
        return jsonify({"ok": True, "service": "rectoverso-upload", "version": 1})

    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(400)
    @app.errorhandler(413)   # Flask's MAX_CONTENT_LENGTH overflow
    def _err(e):
        return (
            jsonify({"ok": False, "status": e.code, "error": e.description}),
            e.code,
        )

    return app


# ---------------------------------------------------------------------------
# Convenience: generate a per-dispatch secret
# ---------------------------------------------------------------------------


def generate_secret() -> bytes:
    """Return 32 cryptographically-random bytes for one dispatch's HMAC
    key. Orchestrator calls this once per EditorTool invocation, holds
    in-memory, never persists."""
    return secrets.token_bytes(32)
