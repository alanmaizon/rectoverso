"""ngrok tunnel helper — exposes the local upload endpoint publicly for
the duration of one Editor Managed Agents dispatch.

Context: the Managed Agents sandbox container has outbound HTTPS (Q1
probe 2026-04-23) but no inbound / bidirectional file primitive. The
agent curls its artifact bundle to our HMAC-auth'd Flask endpoint.
That endpoint listens on localhost; ngrok gives it a public URL so
the sandbox can reach it.

Security posture: the HMAC token in the upload Authorization header is
the real boundary. An anonymous ngrok tunnel + random subdomain is
adequate — unauth'd POSTs get rejected at `verify_token()` before the
multipart body is parsed, so tunnel URL disclosure doesn't matter.

Shape:

    with ngrok_tunnel(port=8765) as public_url:
        # public_url is e.g. "https://abc-12-34-56-78.ngrok-free.app"
        # hand to the agent kickoff message
        ...
    # tunnel closed automatically on context exit

Free-tier rules (2025 onward): requires `ngrok` binary on PATH. If
NGROK_AUTHTOKEN is set (via env or .env), the tunnel gets account
features (longer idle timeout, reserved slot). Without it, anonymous
tunnels still work with shorter lifetimes — fine for dev and the live
integration probe; DEMO_MODE replay needs no tunnel.

Not in scope here:
    - ngrok alternative (Cloudflare Tunnel, localtunnel, etc.) — swap
      at the context-manager boundary if needed later.
    - TLS cert pinning — the agent trusts ngrok's cert chain; hostile
      MITM on ngrok itself is out of our threat model.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ._common import resolve_env_key


# ngrok's local inspection API — this is how we pull the assigned public
# URL after starting the tunnel. Documented part of ngrok v3.
NGROK_INSPECT_URL = "http://127.0.0.1:4040/api/tunnels"

# How long we wait for ngrok to boot + assign a URL. The binary starts
# fast (~1s) but DNS propagation can take a beat. 15s is generous.
NGROK_STARTUP_TIMEOUT_S = 15.0
NGROK_POLL_INTERVAL_S = 0.25


class NgrokError(RuntimeError):
    """Raised when ngrok fails to start, authenticate, or expose a URL
    within the startup timeout. Wraps the underlying cause so the
    orchestrator can surface it as a dispatch failure."""


@contextmanager
def ngrok_tunnel(
    *,
    port: int,
    startup_timeout_s: float = NGROK_STARTUP_TIMEOUT_S,
    authtoken: str | None = None,
) -> Iterator[str]:
    """Start an ngrok tunnel pointing at `localhost:<port>`, yield the
    public https URL, tear down the tunnel on exit.

    `authtoken` is an optional override. Default path: resolve
    NGROK_AUTHTOKEN from env / .env via the shared helper. Falls back to
    an anonymous tunnel if nothing is found.

    Raises NgrokError on any pre-yield failure (binary missing, auth
    rejected, inspect API never returns a URL). Post-yield errors during
    teardown are logged to stderr but not raised — the caller has already
    completed its work.
    """
    if port <= 0 or port > 65535:
        raise NgrokError(f"invalid port: {port}")

    token = authtoken if authtoken is not None else resolve_env_key("NGROK_AUTHTOKEN")

    # 1. Register authtoken if we have one. Idempotent — ngrok caches
    #    the config at ~/.config/ngrok/ngrok.yml, so re-running with the
    #    same token is a no-op.
    if token:
        try:
            subprocess.run(
                ["ngrok", "config", "add-authtoken", token],
                capture_output=True,
                check=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise NgrokError(
                "ngrok binary not found on PATH. Install from "
                "https://ngrok.com/download or `brew install ngrok`."
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise NgrokError(
                f"ngrok config add-authtoken failed: "
                f"{(exc.stderr or b'').decode('utf-8', errors='replace')[:200]}"
            ) from exc

    # 2. Spawn the tunnel. `--log=stdout` so we can tail errors if the
    #    inspect API never comes up.
    try:
        proc = subprocess.Popen(
            ["ngrok", "http", str(port), "--log=stdout", "--log-format=json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise NgrokError(
            "ngrok binary not found on PATH. Install from "
            "https://ngrok.com/download or `brew install ngrok`."
        ) from exc

    try:
        public_url = _poll_for_public_url(
            proc, timeout_s=startup_timeout_s
        )
        yield public_url
    finally:
        # Clean shutdown — terminate + wait briefly, then hard-kill if it
        # stuck around. Zero-exit from ngrok on SIGTERM is normal.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        except Exception:
            # Teardown best-effort. If ngrok lingers, next startup will
            # conflict on port 4040 — but that's a later problem and
            # visible if it happens.
            pass


def _poll_for_public_url(proc: subprocess.Popen, *, timeout_s: float) -> str:
    """Poll ngrok's inspect API until a public https URL appears for our
    tunnel, or raise NgrokError after the timeout."""
    deadline = time.time() + timeout_s
    last_error: str = ""

    while time.time() < deadline:
        # Process died before becoming ready — pick up stdout for
        # diagnostics and bail.
        if proc.poll() is not None:
            stdout_tail = (proc.stdout.read() or b"").decode(
                "utf-8", errors="replace"
            )[-500:] if proc.stdout else ""
            raise NgrokError(
                f"ngrok exited before the tunnel was ready "
                f"(rc={proc.returncode}): {stdout_tail}"
            )

        try:
            with urllib.request.urlopen(NGROK_INSPECT_URL, timeout=1.0) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                for tunnel in body.get("tunnels", []):
                    url = tunnel.get("public_url", "")
                    if url.startswith("https://"):
                        return url
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        except json.JSONDecodeError as exc:
            last_error = f"ngrok inspect returned non-JSON: {exc}"

        time.sleep(NGROK_POLL_INTERVAL_S)

    raise NgrokError(
        f"ngrok did not expose an https tunnel within {timeout_s}s "
        f"(last_error: {last_error or 'none'})"
    )


def ngrok_available() -> bool:
    """Cheap feature-probe for the CLI: returns True iff the ngrok binary
    is on PATH. Used by film_cmd to fall back to `--no-editor` cleanly
    when ngrok is missing (rather than raising mid-dispatch)."""
    try:
        subprocess.run(
            ["ngrok", "version"],
            capture_output=True,
            check=True,
            timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
