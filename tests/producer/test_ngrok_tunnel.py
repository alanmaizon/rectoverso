"""Hermetic tests for the ngrok tunnel helper.

No real tunnels, no subprocess spawns — all exercised via monkeypatched
`subprocess.run`, `subprocess.Popen`, and `urllib.request.urlopen`. The
goal is to lock the control flow (auth → spawn → poll → yield → cleanup)
and the failure modes (binary missing, auth rejected, URL never appears,
process dies early, port out of range) without any flakiness from real
network/binary availability.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from src.producer import ngrok_tunnel as ng


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProc:
    """Stands in for subprocess.Popen. `die_after_polls` simulates an
    early-exit failure (ngrok crashes before exposing a tunnel)."""

    def __init__(
        self,
        *,
        returncode: int | None = None,
        stdout_final: bytes = b"",
        die_after_polls: int | None = None,
    ):
        self.returncode = returncode
        self.stdout = BytesIO(stdout_final)
        self._die_after = die_after_polls
        self._polls = 0
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        self._polls += 1
        if self._die_after is not None and self._polls > self._die_after:
            return self.returncode if self.returncode is not None else 1
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


class _FakeURLResponse:
    """Stands in for the urlopen result. Payload is a JSON body bytes."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def _tunnels_response(https_url: str | None) -> bytes:
    """Build the JSON payload ngrok's inspect API returns. If https_url
    is None, the tunnels list is empty (simulates 'still booting')."""
    tunnels = []
    if https_url is not None:
        tunnels.append({"public_url": https_url, "proto": "https"})
    return json.dumps({"tunnels": tunnels}).encode("utf-8")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_invalid_port_rejected_immediately() -> None:
    with pytest.raises(ng.NgrokError, match="invalid port"):
        with ng.ngrok_tunnel(port=0):
            pass
    with pytest.raises(ng.NgrokError, match="invalid port"):
        with ng.ngrok_tunnel(port=99999):
            pass


# ---------------------------------------------------------------------------
# Auth token registration
# ---------------------------------------------------------------------------


def test_authtoken_registered_when_provided(monkeypatch) -> None:
    """When authtoken kwarg is given, ngrok_tunnel calls
    `ngrok config add-authtoken <token>` before spawning."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    class _EarlyUrl:
        def __init__(self):
            self._served = False

        def __call__(self, url, timeout):
            self._served = True
            return _FakeURLResponse(
                _tunnels_response("https://abc.ngrok-free.app")
            )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(ng.urllib.request, "urlopen", _EarlyUrl())

    with ng.ngrok_tunnel(port=8765, authtoken="test-token") as url:
        assert url == "https://abc.ngrok-free.app"

    assert any(
        cmd[:3] == ["ngrok", "config", "add-authtoken"] and cmd[3] == "test-token"
        for cmd in calls
    ), f"expected add-authtoken call, got {calls}"


def test_no_authtoken_skips_config_step(monkeypatch) -> None:
    """With authtoken=None and no env var, skip the config step entirely —
    anonymous tunnel path."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(ng, "resolve_env_key", lambda *names: None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(
        ng.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeURLResponse(
            _tunnels_response("https://anon.ngrok-free.app")
        ),
    )

    with ng.ngrok_tunnel(port=8765) as url:
        assert url == "https://anon.ngrok-free.app"
    assert calls == []   # no subprocess.run calls at all


def test_authtoken_from_env_resolved(monkeypatch) -> None:
    """When authtoken kwarg is None, fall back to resolve_env_key
    (which reads .env + shell env)."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(ng, "resolve_env_key", lambda *names: "env-provided-token")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(
        ng.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeURLResponse(_tunnels_response("https://x.ngrok-free.app")),
    )

    with ng.ngrok_tunnel(port=8765):
        pass

    assert any(
        cmd[-1] == "env-provided-token" for cmd in calls
    ), f"expected env-resolved token in config call, got {calls}"


def test_authtoken_registration_failure_is_raised(monkeypatch) -> None:
    """If `ngrok config add-authtoken` fails, raise NgrokError with the
    ngrok stderr for operator diagnosis."""

    def fake_run(cmd, **kw):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd, stderr=b"invalid token format"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ng.NgrokError, match="invalid token format"):
        with ng.ngrok_tunnel(port=8765, authtoken="bad"):
            pass


# ---------------------------------------------------------------------------
# Binary missing
# ---------------------------------------------------------------------------


def test_missing_ngrok_binary_on_config(monkeypatch) -> None:
    def fake_run(*a, **k):
        raise FileNotFoundError("No such file or directory: 'ngrok'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ng.NgrokError, match="ngrok binary not found"):
        with ng.ngrok_tunnel(port=8765, authtoken="t"):
            pass


def test_missing_ngrok_binary_on_spawn(monkeypatch) -> None:
    """No authtoken path → we skip the config step, go straight to
    Popen, and there we hit FileNotFoundError."""

    def fake_popen(*a, **k):
        raise FileNotFoundError("No such file or directory: 'ngrok'")

    monkeypatch.setattr(ng, "resolve_env_key", lambda *names: None)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(ng.NgrokError, match="ngrok binary not found"):
        with ng.ngrok_tunnel(port=8765):
            pass


def test_ngrok_available_true_when_version_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, b"ngrok v3", b""),
    )
    assert ng.ngrok_available() is True


def test_ngrok_available_false_when_binary_missing(monkeypatch) -> None:
    def fake_run(*a, **k):
        raise FileNotFoundError("no ngrok")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ng.ngrok_available() is False


# ---------------------------------------------------------------------------
# Poll-for-URL logic
# ---------------------------------------------------------------------------


def test_poll_returns_first_https_url(monkeypatch) -> None:
    """Multiple tunnels may be listed (http + https). We pick the https
    one. Non-https tunnels are ignored."""
    monkeypatch.setattr(ng, "resolve_env_key", lambda *n: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())

    body = json.dumps({
        "tunnels": [
            {"public_url": "http://insecure.example", "proto": "http"},
            {"public_url": "https://secure.ngrok-free.app", "proto": "https"},
        ]
    }).encode()
    monkeypatch.setattr(
        ng.urllib.request, "urlopen",
        lambda *a, **k: _FakeURLResponse(body),
    )

    with ng.ngrok_tunnel(port=8765) as url:
        assert url == "https://secure.ngrok-free.app"


def test_poll_retries_on_empty_tunnel_list(monkeypatch) -> None:
    """First few inspect-API responses are empty (ngrok still booting);
    eventually the URL appears. Polling must tolerate the empty period."""
    monkeypatch.setattr(ng, "resolve_env_key", lambda *n: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())

    responses = [
        _tunnels_response(None),       # boot
        _tunnels_response(None),       # still booting
        _tunnels_response("https://ready.ngrok-free.app"),
    ]

    def urlopen(url, timeout):
        return _FakeURLResponse(responses.pop(0))

    monkeypatch.setattr(ng.urllib.request, "urlopen", urlopen)
    # Speed up the poll so this runs in <0.5s
    monkeypatch.setattr(ng, "NGROK_POLL_INTERVAL_S", 0.01)

    with ng.ngrok_tunnel(port=8765) as url:
        assert url == "https://ready.ngrok-free.app"


def test_poll_retries_on_connection_refused(monkeypatch) -> None:
    """ngrok's inspect API isn't up yet → URLError on first call; we
    keep polling."""
    monkeypatch.setattr(ng, "resolve_env_key", lambda *n: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())

    calls: list[int] = []

    def urlopen(url, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError("Connection refused")
        return _FakeURLResponse(
            _tunnels_response("https://late.ngrok-free.app")
        )

    monkeypatch.setattr(ng.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(ng, "NGROK_POLL_INTERVAL_S", 0.01)

    with ng.ngrok_tunnel(port=8765) as url:
        assert url == "https://late.ngrok-free.app"
    assert len(calls) == 3


def test_timeout_when_url_never_appears(monkeypatch) -> None:
    monkeypatch.setattr(ng, "resolve_env_key", lambda *n: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(
        ng.urllib.request,
        "urlopen",
        lambda url, timeout: _FakeURLResponse(_tunnels_response(None)),
    )
    monkeypatch.setattr(ng, "NGROK_POLL_INTERVAL_S", 0.01)

    with pytest.raises(ng.NgrokError, match="did not expose"):
        with ng.ngrok_tunnel(port=8765, startup_timeout_s=0.1):
            pass


def test_process_dies_during_startup_raises(monkeypatch) -> None:
    """ngrok exits before a tunnel is ready. We detect via proc.poll()
    returning non-None, capture stdout tail, raise NgrokError."""
    monkeypatch.setattr(ng, "resolve_env_key", lambda *n: None)
    dying = _FakeProc(
        returncode=1,
        stdout_final=b'{"lvl":"error","msg":"authentication failed"}',
        die_after_polls=1,
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: dying)

    def urlopen(url, timeout):
        raise urllib.error.URLError("not up")

    monkeypatch.setattr(ng.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(ng, "NGROK_POLL_INTERVAL_S", 0.01)

    with pytest.raises(ng.NgrokError, match="exited before"):
        with ng.ngrok_tunnel(port=8765, startup_timeout_s=2.0):
            pass


# ---------------------------------------------------------------------------
# Teardown invariants
# ---------------------------------------------------------------------------


def test_terminate_called_on_clean_exit(monkeypatch) -> None:
    monkeypatch.setattr(ng, "resolve_env_key", lambda *n: None)
    proc = _FakeProc()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(
        ng.urllib.request, "urlopen",
        lambda *a, **k: _FakeURLResponse(
            _tunnels_response("https://x.ngrok-free.app")
        ),
    )

    with ng.ngrok_tunnel(port=8765):
        pass

    assert proc.terminated is True


def test_terminate_called_even_when_body_raises(monkeypatch) -> None:
    """Exception inside the with-block must not leak the tunnel — the
    finally block tears it down."""
    monkeypatch.setattr(ng, "resolve_env_key", lambda *n: None)
    proc = _FakeProc()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(
        ng.urllib.request, "urlopen",
        lambda *a, **k: _FakeURLResponse(
            _tunnels_response("https://x.ngrok-free.app")
        ),
    )

    with pytest.raises(RuntimeError, match="body-failure"):
        with ng.ngrok_tunnel(port=8765):
            raise RuntimeError("body-failure")

    assert proc.terminated is True
