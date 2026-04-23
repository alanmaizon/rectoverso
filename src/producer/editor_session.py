"""AnthropicManagedAgentsSession — the real EditorSession implementation.

Composes the three infrastructure layers built in prior commits:
    - upload_endpoint.py: Flask sink for artifact extraction
    - ngrok_tunnel.py: public URL for the sandbox to reach the sink
    - anthropic SDK: beta environments/agents/sessions/events surface

into one Protocol-compliant `run()` that `EditorTool` calls synchronously.

Architecture (single caller, so single class — we'd split if we grew a
second consumer, per the design discussion pinned 2026-04-23):

    run()
      ├─ exit_stack.enter_context(_spawn_upload_sink)  # Flask + ngrok
      │     yields (url, token, sink_state)
      ├─ _create_session_stack(stack)                  # env/agent/session
      │     each resource.id pushed as exit_stack.callback for archive
      ├─ _build_full_kickoff(base_msg, url, token)     # append upload block
      ├─ _drain_stream(session_id, timeout_s)          # events → transcript
      ├─ _extract_editor_result(transcript)            # parse EDITOR_RESULT
      ├─ _retrieve_usage(session_id)                   # cost reconciliation
      ├─ _verify_artifact_integrity(envelope, sink)    # sha256 round-trip
      └─ return EditorSessionResult(...)

ExitStack pattern means cleanup correctness is implicit in resource-push
order — any exception between sink setup and session idle unwinds in LIFO
order without a per-branch try/finally ladder.

Three typed failure classes from editor.py govern what the orchestrator
does with each failure mode:
    - SessionInfrastructureError → retry with backoff (infra hiccup)
    - SessionProtocolError → escalate (agent behavior, deterministic)
    - SessionBudgetError → halt (operator intervention required)

Everything else uses the EditorSessionResult shape so non-raised failures
(agent FAIL verdict, missing artifacts) still round-trip cleanly through
EditorTool's _project_session_result without touching the exception path.
"""

from __future__ import annotations

import contextlib
import hashlib
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from werkzeug.serving import make_server

from ._common import compute_opus_47_cost
from .editor import (
    EDITOR_RESULT_RE,
    EditorSessionResult,
    SessionBudgetError,
    SessionInfrastructureError,
    SessionProtocolError,
    parse_editor_result,
)
from .ngrok_tunnel import ngrok_tunnel
from .upload_endpoint import (
    UploadToken,
    create_app,
    generate_secret,
    mint_token,
)


BETA_HEADER = "managed-agents-2026-04-01"

# Stream drain idle-check cadence. Longer than you'd think — the event
# stream is push-based so we block on .read; this is the max we'll wait
# between consecutive events before checking our own wall-clock timeout.
STREAM_EVENT_TIMEOUT_S = 2.0

# Session-idle stop reasons that mean "agent thinks it's done." Distinct
# from 'requires_action' (tool confirmation — we don't use those) and
# 'retries_exhausted' (agent gave up).
IDLE_END_TURN_REASONS = frozenset({"end_turn"})


# ---------------------------------------------------------------------------
# Sink state — what _spawn_upload_sink yields
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SinkState:
    """Everything downstream steps need about the live upload sink.

    `sha256_of` lets `_verify_artifact_integrity` cross-check the agent's
    reported sha256 against what the endpoint actually stored.
    `storage_dir` is where the tar.gz landed — downstream commits will
    extract it into `workspace_dir` but for now we just confirm presence.
    """

    public_url: str
    token: UploadToken
    session_id: str
    storage_dir: Path
    sha256_of: Callable[[str], str | None]  # filename -> sha256 hex or None


# ---------------------------------------------------------------------------
# The session class
# ---------------------------------------------------------------------------


class AnthropicManagedAgentsSession:
    """Live Managed Agents session dispatcher. Implements EditorSession.

    Construct once per EditorTool (cache the Anthropic client to keep
    Pydantic type-import cost amortized); call `run()` per dispatch.
    The class is stateless between dispatches — each run builds and
    tears down its own env/agent/session stack.

    Construction:
        client              : anthropic.Anthropic instance (caller holds)
        storage_root        : Path — artifacts land in <root>/<session_id>/;
                              rectoverso sets this to artifacts/edit/uploads/
                              so the existing film_status clear covers it.
        upload_port         : int | None — explicit port or 0 for autopick.
                              Tests use a fixed port to assert wiring.
        startup_timeout_s   : float — max wait for Flask + ngrok to come up.
                              Default 20s (ngrok's 15s + slack).
    """

    def __init__(
        self,
        *,
        client: Any,
        storage_root: Path,
        upload_port: int = 0,
        startup_timeout_s: float = 20.0,
    ) -> None:
        self._client = client
        self._storage_root = Path(storage_root)
        self._upload_port_hint = int(upload_port)
        self._startup_timeout_s = float(startup_timeout_s)

    # -- public entry point ---------------------------------------------

    def run(
        self,
        *,
        system_prompt: str,
        skills: tuple[str, ...],
        model: str,
        apt_packages: tuple[str, ...],
        workspace_dir: Path,
        initial_message: str,
        timeout_s: float,
    ) -> EditorSessionResult:
        """Run one Editor session end-to-end.

        Returns EditorSessionResult even on non-exceptional failures
        (agent FAIL verdict, missing artifacts, unparseable envelope).
        Raises only the three SessionError subclasses for exceptional
        failures that the orchestrator needs to class-match on.

        `workspace_dir` parameter is received from EditorTool but the
        session itself doesn't mount it — the sandbox has no bidirectional
        file primitive (Q1 probe confirmed). `workspace_dir` is where the
        orchestrator will extract the uploaded tar.gz post-dispatch.
        """
        start = time.time()

        with contextlib.ExitStack() as stack:
            # 1. Spawn upload sink (Flask + ngrok). Raise
            #    SessionInfrastructureError on any startup failure.
            try:
                sink = stack.enter_context(self._spawn_upload_sink())
            except Exception as exc:
                raise SessionInfrastructureError(
                    f"upload sink failed to start: {type(exc).__name__}: {exc}"
                ) from exc

            # 2. Create the Anthropic stack. Each .create succeeds or
            #    raises; on raise, the already-created resources archive
            #    via the ExitStack callbacks that registered them.
            try:
                env_id, agent_id, session_id = self._create_session_stack(
                    stack=stack,
                    system_prompt=system_prompt,
                    skills=skills,
                    model=model,
                    apt_packages=apt_packages,
                )
            except Exception as exc:
                raise SessionInfrastructureError(
                    f"Managed Agents stack create failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            # Rebind sink with real session_id now that we have it — the
            # token was minted on a hint, refresh it to the actual id.
            sink = self._rebind_sink_to_session(sink, session_id)

            # 3. Build the full kickoff: base message + upload block.
            full_message = self._build_full_kickoff(
                base_message=initial_message,
                sink=sink,
            )

            # 4. Dispatch + drain. Returns the final agent text for
            #    envelope parsing; cumulative cost comes from usage later.
            try:
                final_text = self._drain_stream(
                    session_id=session_id,
                    initial_message=full_message,
                    timeout_s=timeout_s,
                )
            except TimeoutError as exc:
                # Retrieve whatever usage accrued so far, then surface the
                # timeout as a recoverable result (not an exception).
                usage = self._retrieve_usage_safe(session_id)
                cost = compute_opus_47_cost(usage)
                return EditorSessionResult(
                    verdict="failed",
                    failure_stage="timeout",
                    final_payload={},
                    cost_usd=cost,
                    latency_s=round(time.time() - start, 3),
                    transcript_tail=str(exc)[:500],
                    stderr_tail="",
                )

            # 5. Retrieve usage → cost.
            usage = self._retrieve_usage_safe(session_id)
            cost = compute_opus_47_cost(usage)

            # 6. Parse the envelope.
            envelope = parse_editor_result(final_text)
            latency = round(time.time() - start, 3)

            if not envelope:
                # Agent never emitted EDITOR_RESULT — EditorTool's
                # _project_session_result will classify this as
                # "unparseable_verdict" and fail the dispatch cleanly.
                return EditorSessionResult(
                    verdict="ok",   # session itself was fine
                    failure_stage=None,
                    final_payload={},
                    cost_usd=cost,
                    latency_s=latency,
                    transcript_tail=final_text[-500:] if final_text else "",
                    stderr_tail="",
                )

            # 7. If agent claims PASS, verify the upload sha256 matches
            #    what the endpoint actually stored. Mismatch ⇒ protocol
            #    error: the artifact we have on disk isn't what the agent
            #    says it uploaded, so we can't trust the rest of the
            #    envelope either.
            if str(envelope.get("verdict", "")).upper() == "PASS":
                try:
                    self._verify_artifact_integrity(envelope, sink)
                except SessionProtocolError:
                    raise   # re-raise; orchestrator classifies
                except Exception as exc:
                    raise SessionProtocolError(
                        f"artifact integrity check failed: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc

            return EditorSessionResult(
                verdict="ok",
                failure_stage=None,
                final_payload=envelope,
                cost_usd=cost,
                latency_s=latency,
                transcript_tail=final_text[-500:] if final_text else "",
                stderr_tail="",
            )

    # -- private helpers (each with tight responsibility) ---------------

    @contextlib.contextmanager
    def _spawn_upload_sink(
        self, session_id_hint: str | None = None
    ) -> Iterator[_SinkState]:
        """Start the Flask upload endpoint + ngrok tunnel. Yield a
        _SinkState with the public URL + token. Tear down both on exit.

        Uses a session_id_hint for the initial token mint; when the real
        Anthropic session id is available, `_rebind_sink_to_session`
        re-mints the token with the real id (same secret, same endpoint).
        The endpoint itself accepts whatever session_id the token was
        minted for — no reconfiguration needed."""
        secret = generate_secret()
        effective_id = session_id_hint or f"pending_{int(time.time())}"

        app = create_app(
            storage_root=self._storage_root,
            secret=secret,
        )

        port = self._pick_port()
        server = make_server("127.0.0.1", port, app)
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"upload-sink-{port}",
            daemon=True,
        )
        thread.start()

        try:
            with ngrok_tunnel(port=port) as public_url:
                token = mint_token(effective_id, secret)
                state = _SinkState(
                    public_url=public_url.rstrip("/"),
                    token=token,
                    session_id=effective_id,
                    storage_dir=self._storage_root / effective_id,
                    sha256_of=lambda name, _sr=self._storage_root, _sid=effective_id: (
                        _safe_sha256_of(Path(_sr) / _sid / name)
                    ),
                )
                # Stash the secret on the state via a closure for rebind.
                state.__dict__["_secret"] = secret  # type: ignore[attr-defined]
                yield state
        finally:
            try:
                server.shutdown()
            except Exception:
                pass

    def _rebind_sink_to_session(
        self, sink: _SinkState, real_session_id: str
    ) -> _SinkState:
        """Re-mint the token with the real Anthropic session_id so the
        path/claim binding check in the endpoint passes. The secret is
        the same (same process), so the endpoint auto-validates."""
        secret: bytes = sink.__dict__.get("_secret")
        if not secret:
            raise SessionInfrastructureError("sink state missing secret")
        new_token = mint_token(real_session_id, secret)
        rebound = _SinkState(
            public_url=sink.public_url,
            token=new_token,
            session_id=real_session_id,
            storage_dir=self._storage_root / real_session_id,
            sha256_of=lambda name, _sr=self._storage_root, _sid=real_session_id: (
                _safe_sha256_of(Path(_sr) / _sid / name)
            ),
        )
        rebound.__dict__["_secret"] = secret
        return rebound

    def _create_session_stack(
        self,
        *,
        stack: contextlib.ExitStack,
        system_prompt: str,
        skills: tuple[str, ...],
        model: str,
        apt_packages: tuple[str, ...],
    ) -> tuple[str, str, str]:
        """Create environment → agent → session, registering each for
        archive-on-exit via the ExitStack. Returns the three ids."""
        betas = [BETA_HEADER]

        env = self._client.beta.environments.create(
            name=f"rv-editor-{int(time.time())}",
            config={
                "type": "cloud",
                "networking": {"type": "unrestricted"},
                "packages": {"apt": list(apt_packages), "npm": list(skills)},
            },
            betas=betas,
        )
        stack.callback(self._archive_safely, "environment", env.id)

        agent = self._client.beta.agents.create(
            model=model,
            name="rv-editor-agent",
            system=system_prompt,
            tools=[
                {
                    "type": "agent_toolset_20260401",
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                }
            ],
            betas=betas,
        )
        stack.callback(self._archive_safely, "agent", agent.id)

        session = self._client.beta.sessions.create(
            agent=agent.id,
            environment_id=env.id,
            title="rectoverso-editor",
            betas=betas,
        )
        stack.callback(self._archive_safely, "session", session.id)

        return env.id, agent.id, session.id

    def _build_full_kickoff(
        self, *, base_message: str, sink: _SinkState
    ) -> str:
        """Append the upload details block to the base kickoff message.
        The base message references 'see upload section below' so the
        agent knows to scan for UPLOAD_URL + UPLOAD_TOKEN verbatim."""
        token_str = sink.token.encode()
        return (
            base_message
            + "\n"
            + "=== UPLOAD DETAILS (for this dispatch only) ===\n"
            + f"UPLOAD_URL={sink.public_url}/upload/{sink.session_id}\n"
            + f"UPLOAD_TOKEN={token_str}\n"
            + "\n"
            + "To ship artifacts, bundle artifacts/edit/ into a tar.gz and POST:\n"
            + "\n"
            + '    tar czf /tmp/edit.tar.gz -C artifacts edit/\n'
            + "    curl --fail --show-error -X POST $UPLOAD_URL \\\n"
            + "         -H \"Authorization: Bearer $UPLOAD_TOKEN\" \\\n"
            + '         -F "file=@/tmp/edit.tar.gz"\n'
            + "\n"
            + "The response JSON carries {sha256, size_bytes, stored_at}. Echo\n"
            + "the sha256 verbatim in EDITOR_RESULT.uploaded_sha256 — the dispatcher\n"
            + "cross-checks it against the server-side stored bytes.\n"
        )

    def _drain_stream(
        self,
        *,
        session_id: str,
        initial_message: str,
        timeout_s: float,
    ) -> str:
        """Open the event stream, send the kickoff user.message, drain
        until session.status_idle (end_turn), return the agent's final
        text block. Raises TimeoutError if the session doesn't idle within
        `timeout_s` wall-clock.

        Non-idle stop reasons (retries_exhausted, errors) produce an
        empty final_text return — the caller treats empty as
        'unparseable_verdict'."""
        deadline = time.time() + timeout_s
        transcript_texts: list[str] = []

        with self._client.beta.sessions.events.stream(
            session_id, betas=[BETA_HEADER]
        ) as stream:
            self._client.beta.sessions.events.send(
                session_id,
                events=[{
                    "type": "user.message",
                    "content": [{"type": "text", "text": initial_message}],
                }],
                betas=[BETA_HEADER],
            )

            for event in stream:
                # Collect text blocks as they arrive; we only need the
                # last substantial one but keep them all for transcript
                # continuity in logs.
                etype = getattr(event, "type", "")
                if etype == "agent.message":
                    content = getattr(event, "content", None) or []
                    for block in content:
                        if getattr(block, "type", "") == "text":
                            txt = getattr(block, "text", "")
                            if txt:
                                transcript_texts.append(txt)

                if etype in {
                    "session.status_idle",
                    "session.idle",
                    "session.ended",
                    "session.status_terminated",
                }:
                    break

                if time.time() > deadline:
                    raise TimeoutError(
                        f"session {session_id} did not idle within {timeout_s}s"
                    )

        return transcript_texts[-1] if transcript_texts else ""

    def _retrieve_usage_safe(self, session_id: str) -> Mapping[str, Any] | None:
        """Fetch the session object, return its `usage` field. Returns
        None on any failure — cost just reads as zero, caller continues.
        """
        try:
            retrieved = self._client.beta.sessions.retrieve(
                session_id, betas=[BETA_HEADER]
            )
            return getattr(retrieved, "usage", None)
        except Exception:
            return None

    def _verify_artifact_integrity(
        self, envelope: Mapping[str, Any], sink: _SinkState
    ) -> None:
        """Cross-check the agent-reported sha256 against what the
        endpoint stored. Raises SessionProtocolError on any mismatch or
        missing artifact."""
        reported = str(envelope.get("uploaded_sha256") or "")
        if not reported:
            raise SessionProtocolError(
                "PASS envelope missing uploaded_sha256 field; "
                "cannot verify artifact integrity"
            )

        # Look for a tar.gz — default naming per the upload instructions
        # in _build_full_kickoff. If the agent uploaded with a different
        # filename, we'd need to scan the storage_dir. For v1 we assume
        # edit.tar.gz and fall back to a directory scan.
        for candidate in ("edit.tar.gz", "composition.tar.gz"):
            server_sha = sink.sha256_of(candidate)
            if server_sha is not None:
                break
        else:
            # Fall back: scan the storage dir for any file, take the
            # largest (most likely the tar.gz).
            if sink.storage_dir.exists():
                files = [p for p in sink.storage_dir.iterdir() if p.is_file()]
                if files:
                    largest = max(files, key=lambda p: p.stat().st_size)
                    server_sha = _safe_sha256_of(largest)
                else:
                    server_sha = None
            else:
                server_sha = None

        if server_sha is None:
            raise SessionProtocolError(
                "agent reported uploaded_sha256 but no artifact was found "
                f"in the upload sink at {sink.storage_dir}"
            )

        if reported.lower() != server_sha.lower():
            raise SessionProtocolError(
                f"uploaded_sha256 mismatch: agent reported {reported}, "
                f"endpoint computed {server_sha}"
            )

    def _archive_safely(self, kind: str, resource_id: str) -> None:
        """Archive an env/agent/session; swallow exceptions so cleanup
        never masks the original error that triggered unwinding."""
        try:
            beta = self._client.beta
            if kind == "session":
                beta.sessions.archive(resource_id, betas=[BETA_HEADER])
            elif kind == "agent":
                beta.agents.archive(resource_id, betas=[BETA_HEADER])
            elif kind == "environment":
                beta.environments.archive(resource_id, betas=[BETA_HEADER])
        except Exception:
            # Best-effort; never raise from cleanup.
            pass

    def _pick_port(self) -> int:
        """Pick a concrete port. When the hint is 0, let the OS assign.
        When explicit, use it (tests rely on this for predictability)."""
        if self._upload_port_hint != 0:
            return self._upload_port_hint
        # Autopick via a throwaway socket bind.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _safe_sha256_of(path: Path) -> str | None:
    """Streaming sha256 of a file; returns None if the file doesn't exist
    or can't be read. Used by sink integrity checks — we never raise from
    there, we return None and let the caller classify."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None
