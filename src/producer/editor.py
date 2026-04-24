"""EditorTool — Producer-side dispatcher for the Editor Agent Managed Agents session.

**Tier-2 dispatcher, not a composer.** This module's only job is to spawn the
Editor Agent's Managed Agents session, wait for it to terminate, and translate
the agent's final message into a dispatch_result-shaped dict. The Editor
Agent (running inside the session, equipped with the `hyperframes`,
`hyperframes-cli`, and `gsap` skills plus the bash/read/write/edit tools)
is the thing that actually authors the HTML composition and drives
`npx hyperframes lint` / `npx hyperframes render`.

The Producer never invokes `HyperframesTool` directly — HyperframesTool
is a subprocess wrapper used *inside* the sandbox by the agent's bash tool.
The architectural split is:

    Producer  ->  EditorTool.__call__      (THIS module)
                     |
                     v
                  EditorSession.run        (Managed Agents API boundary)
                     |
                     v
                  [sandbox] Editor Agent + skills + tools
                     |
                     v
                  Editor Agent's bash tool -> npx hyperframes lint/render
                                           -> HyperframesTool-like shell calls

Do not collapse EditorTool to a mechanical composer. The Tier-2 shape — an
LLM-authored HTML composition inside a long-running session — is the
architecture decision we've already pinned. This dispatcher is thin on
purpose.

**Caching discipline.** Managed Agents caches aggressively on (system prompt,
skills list, model). Any of those changing mid-run breaks the cache, which
for an hours-long Editor session is a meaningful Anthropic-budget hit. The
EditorTool freezes all three at construction time and never mutates them.
A resume that wants the same cache hit instantiates EditorTool with
identical args. Don't tweak the prompt file between resumes.

**Failure policy.** The tool returns a status-shaped dict; it does NOT raise
on agent-reported failures. The orchestrator decides whether to escalate
the film (per user policy: escalate immediately, don't auto-retry —
Editor failures are usually upstream shot problems, not compose bugs, and
retry wastes Anthropic budget).

**Events/budget.** This tool is pure dispatcher. Event logging and
budget.spent_usd accrual happen in the orchestrator wrapper (same pattern
as NormalizeTool — the tool returns cost_usd and the orchestrator projects
it into events.db + budget counters).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from ._common import md5_file


REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_PROMPT_PATH = REPO_ROOT / "prompts" / "editor_agent.md"

DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_SKILLS = ("hyperframes", "hyperframes-cli", "gsap")
DEFAULT_APT_PACKAGES = ("ffmpeg",)
DEFAULT_TIMEOUT_S = 7200          # 2h cap — Editor sessions run multi-hour on full films

# Failure stages the adapter surfaces in `failure_stage` on non-OK results.
FAILURE_STAGES = frozenset({
    "shot_id_not_none",
    "manifest_missing",
    "workspace_error",
    "session_error",
    "timeout",
    "unparseable_verdict",
    "agent_reported_fail",
    "missing_artifacts",
})

# The Editor Agent's final message MUST contain a line matching this regex.
# Mirrors the probe's PROBE_RESULT pattern — machine-parseable JSON with
# a lead-in marker so text blocks around it don't confuse the extractor.
EDITOR_RESULT_RE = re.compile(r"EDITOR_RESULT:\s*(\{.+\})\s*$", re.MULTILINE | re.DOTALL)

# Required fields in a PASS result — absence → missing_artifacts failure.
PASS_REQUIRED_FIELDS = (
    "composition_path",
    "render_path",
    "render_md5",
    "duration_s",
    # Integrity check on the HTTPS POST extraction path: the agent echoes
    # the sha256 returned by the upload endpoint, we cross-check against
    # the server-side stored bytes. Required on every PASS so no dispatch
    # claims success without a verifiable artifact.
    "uploaded_sha256",
)


# ---------------------------------------------------------------------------
# Typed failure classes for AnthropicManagedAgentsSession (and any future
# EditorSession implementation). Separating infra / protocol / budget lets
# the orchestrator retry only the class that's worth retrying and escalate
# the rest without stringly-matching on reason messages.
# ---------------------------------------------------------------------------


class SessionError(RuntimeError):
    """Base for all EditorSession runtime failures. Sub-classes carry
    the failure category so the orchestrator can branch without parsing
    the message text."""

    failure_kind: str = "unknown"


class SessionInfrastructureError(SessionError):
    """Tunnel won't spawn, Flask port is bound, Anthropic API 5xx — the
    session never meaningfully started. Orchestrator may retry with
    backoff. Does NOT burn the dispatch's budget because the agent never
    ran."""

    failure_kind = "infra"


class SessionProtocolError(SessionError):
    """Agent finished without emitting EDITOR_RESULT, sha256 mismatch
    between agent-reported and endpoint-recorded, malformed envelope.
    Orchestrator escalates (don't retry — this is a deterministic agent
    behavior that will repeat). Budget has been burned."""

    failure_kind = "protocol"


class SessionBudgetError(SessionError):
    """Cumulative session cost projected to breach budget.cap_usd before
    idle. Orchestrator halts, writes partial state, escalates. No retry
    possible until operator resolves the budget state."""

    failure_kind = "budget"


# ---------------------------------------------------------------------------
# Session-spawn abstraction (the Managed Agents API boundary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditorSessionResult:
    """Result of one Managed Agents session run. Whatever the concrete
    EditorSession implementation is (real Anthropic API, hermetic stub,
    mock for integration tests), it returns this shape.

    Fields:
        verdict      : "ok" | "failed" — high-level session outcome. The
                       agent's own PASS/FAIL verdict is inside final_payload;
                       "failed" here also covers session-level failures
                       (timeout, API error) that aren't the agent's fault.
        failure_stage: Populated when verdict=="failed". One of FAILURE_STAGES.
        final_payload: Parsed JSON object after the EDITOR_RESULT: marker.
                       Empty dict if the session never produced a parseable
                       final message (→ failure_stage="unparseable_verdict").
        cost_usd     : Anthropic session token cost. Load-bearing — threaded
                       into budget.spent_usd by the orchestrator wrapper.
        latency_s    : Wall-clock session duration.
        transcript_tail: Last ~500 chars of the agent's event transcript for
                         post-mortem when things fail. NOT the whole log —
                         full transcript is in the session archive on the
                         Anthropic side.
        stderr_tail  : Last ~500 chars of anything the session surfaced as
                         error output. Empty on clean runs.
    """

    verdict: str
    failure_stage: str | None
    final_payload: Mapping[str, Any]
    cost_usd: float
    latency_s: float
    transcript_tail: str = ""
    stderr_tail: str = ""


@runtime_checkable
class EditorSession(Protocol):
    """The boundary between the Producer-side dispatcher and the
    Anthropic Managed Agents API. Injectable so tests can run the full
    dispatcher logic against a stub without touching the real API.

    A concrete implementation manages the environment/agent/session
    lifecycle: creates the cloud environment with apt packages, registers
    the agent with the system prompt + skills + toolset, opens the event
    stream, sends the initial user message, drains events until session_idle
    or timeout, archives the resources, and returns an EditorSessionResult.

    See `scratch/managed_agents_hyperframes_probe.py` for the canonical
    event-stream drain pattern. The real EditorSession implementation lives
    outside this module (so tests never accidentally instantiate it).
    """

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
    ) -> EditorSessionResult: ...


# ---------------------------------------------------------------------------
# The dispatcher itself — Tool Protocol
# ---------------------------------------------------------------------------


class EditorTool:
    """Tool Protocol dispatcher for the Editor Agent Managed Agents session.

    `name == "editor_agent"` (matches the registry row in
    `src/contracts/registry.py` — Contract 1 + Contract 5 + Contract 6 all
    fire on this agent).

    Payload (project-level — shot_id must be None):
        manifest_path : Path to the manifest the Editor reads (read-only).
        workspace_dir : Path where the session writes `artifacts/edit/`.
        brief_slice   : Minimal dict excerpted from manifest.brief (title,
                        target_duration_s, artistic_style) — sent in the
                        initial message so the agent doesn't have to parse
                        the whole brief for top-level context.
        estimated_cost_usd : Conservative Anthropic session estimate seeded
                             by the orchestrator's budget pre-check. Echoed
                             back in the result for audit; the real cost is
                             the session's reported token spend.

    Result dict (shape matches dispatch_result payload):
        status              : "ok" | "failed"
        provider            : "managed_agents"
        mode                : "editor_session"
        failure_stage       : populated iff status=="failed"
        composition_path    : relative path to artifacts/edit/index.html
        composition_archive_path : relative path to artifacts/edit/composition.zip
        render_path         : relative path to artifacts/edit/out.mp4
        render_md5          : MD5 of the rendered MP4
        duration_s          : total film duration (ffprobe of out.mp4)
        renderer_version    : from Hyperframes lint meta
        cost_usd            : Anthropic session token cost (NON-ZERO)
        estimated_cost_usd  : the estimate passed in (for variance audit)
        quota_cost          : 0 (token spend tracked under cost_usd)
        latency_s           : wall-clock dispatch
        transcript_tail     : last ~500 chars of session transcript
        stderr_tail         : last ~500 chars of stderr
    """

    name = "editor_agent"

    @classmethod
    def from_env(
        cls,
        *,
        demo_mode: bool | None = None,
        fixture_dir: Path | None = None,
        client: Any | None = None,
        storage_root: Path | None = None,
        system_prompt: str | None = None,
        skills: tuple[str, ...] = DEFAULT_SKILLS,
        apt_packages: tuple[str, ...] = DEFAULT_APT_PACKAGES,
        model: str = DEFAULT_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> "EditorTool":
        """Factory that selects MockEditorSession or AnthropicManagedAgentsSession
        based on RECTOVERSO_DEMO_MODE env var or the explicit demo_mode kwarg.
        Lazy-imports both implementations to avoid circular imports.
        """
        if demo_mode is None:
            demo_mode = os.environ.get("RECTOVERSO_DEMO_MODE", "0") == "1"

        if demo_mode:
            from .editor_session_mock import MockEditorSession

            session: EditorSession = MockEditorSession(
                fixture_dir=fixture_dir or REPO_ROOT / "demo" / "fixtures" / "editor",
            )
        else:
            from .editor_session import AnthropicManagedAgentsSession

            if client is None:
                raise ValueError(
                    "EditorTool.from_env: client is required in production mode"
                )
            session = AnthropicManagedAgentsSession(
                client=client,
                storage_root=storage_root or REPO_ROOT / "artifacts" / "edit" / "uploads",
            )

        return cls(
            session=session,
            system_prompt=system_prompt,
            skills=skills,
            apt_packages=apt_packages,
            model=model,
            timeout_s=timeout_s,
        )

    def __init__(
        self,
        *,
        session: EditorSession,
        system_prompt: str | None = None,
        skills: tuple[str, ...] = DEFAULT_SKILLS,
        apt_packages: tuple[str, ...] = DEFAULT_APT_PACKAGES,
        model: str = DEFAULT_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        # System prompt read once at construction — NEVER re-read. Mutating
        # this between dispatches breaks the Managed Agents prompt cache.
        self._system_prompt = (
            system_prompt
            if system_prompt is not None
            else SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        )
        # Freeze skills + apt + model at construction for the same cache
        # reason. Resume instantiates a new EditorTool with identical args.
        self._skills = tuple(skills)
        self._apt_packages = tuple(apt_packages)
        self._model = model
        self._timeout_s = float(timeout_s)
        self._session = session

    # -- Tool Protocol ----------------------------------------------------

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        started = time.time()

        if shot_id is not None:
            return _failure(
                stage="shot_id_not_none",
                stderr=(
                    "EditorTool is project-level; shot_id must be None. "
                    f"got shot_id={shot_id!r}"
                ),
                started=started,
                estimated=_estimate(payload),
            )

        manifest_path = payload.get("manifest_path")
        if not manifest_path or not Path(manifest_path).exists():
            return _failure(
                stage="manifest_missing",
                stderr=f"manifest not found at {manifest_path!r}",
                started=started,
                estimated=_estimate(payload),
            )

        workspace_dir = Path(payload.get("workspace_dir") or "artifacts/edit")
        try:
            workspace_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _failure(
                stage="workspace_error",
                stderr=f"could not create workspace {workspace_dir!r}: {exc!r}",
                started=started,
                estimated=_estimate(payload),
            )

        initial_message = _build_initial_message(
            manifest_path=Path(manifest_path),
            workspace_dir=workspace_dir,
            brief_slice=payload.get("brief_slice") or {},
        )

        # Call the session. Any bare exception here is session-level
        # (API error, network) — NOT an agent verdict. Distinguish the two
        # so the orchestrator can react appropriately (retry session-level
        # blips; don't retry agent-level escalations).
        try:
            session_result = self._session.run(
                system_prompt=self._system_prompt,
                skills=self._skills,
                model=self._model,
                apt_packages=self._apt_packages,
                workspace_dir=workspace_dir,
                initial_message=initial_message,
                timeout_s=self._timeout_s,
            )
        except TimeoutError as exc:
            return _failure(
                stage="timeout",
                stderr=str(exc)[:500],
                started=started,
                estimated=_estimate(payload),
            )
        except Exception as exc:
            return _failure(
                stage="session_error",
                stderr=f"{type(exc).__name__}: {exc}"[:500],
                started=started,
                estimated=_estimate(payload),
            )

        # Session returned. Translate into the dispatch_result shape.
        return _project_session_result(
            session_result=session_result,
            started=started,
            workspace_dir=workspace_dir,
            estimated=_estimate(payload),
        )


# ---------------------------------------------------------------------------
# Initial message template (what kicks off the agent session)
# ---------------------------------------------------------------------------


def _build_initial_message(
    *,
    manifest_path: Path,
    workspace_dir: Path,
    brief_slice: Mapping[str, Any],
) -> str:
    """Build the base `user.message` that the dispatcher sends to kick the
    session. Short, specific, deterministic — the system prompt has the
    composition rules; this message has the paths + the expected final
    line protocol.

    This is a *base* template. The real Managed Agents session
    (`AnthropicManagedAgentsSession`) appends its own "upload details"
    section after spawning the ngrok tunnel + upload endpoint, because
    those values aren't known until the infra is live. Keeping the base
    template here lets hermetic tests exercise the EditorTool path
    without needing the session-class infra to be stood up.

    The EDITOR_RESULT marker line is how we extract a machine-readable
    verdict from the agent's final text block. Matches the PROBE_RESULT
    pattern from the sandbox verification probe.
    """
    brief_json = json.dumps(dict(brief_slice), sort_keys=True)
    return (
        f"Assemble the film described in the manifest at {manifest_path}.\n"
        f"Write all artifacts under {workspace_dir}/.\n"
        f"Brief excerpt (read-only context): {brief_json}\n"
        "\n"
        "Follow the composition authoring rules in your system prompt.\n"
        "Drive `npx hyperframes lint --json` until errorCount==0 before render.\n"
        "Drive `npx hyperframes render --output out.mp4` to produce the MP4.\n"
        "After a successful render, bundle artifacts and upload per the upload\n"
        "protocol in your system prompt — the Managed Agents session layer will\n"
        "append the UPLOAD_URL and UPLOAD_TOKEN for this dispatch just below.\n"
        "Every approved shot already has a final.normalized_path — ingest those,\n"
        "NOT final.render_path (codec heterogeneity corrupts concat otherwise).\n"
        "\n"
        "On the FINAL line of your FINAL message, output EXACTLY one of:\n"
        "\n"
        '    EDITOR_RESULT: {"verdict":"PASS","composition_path":"<rel>",'
        '"composition_archive_path":"<rel>","render_path":"<rel>",'
        '"render_md5":"<hex>","duration_s":<float>,'
        '"renderer_version":"<x.y.z>","uploaded_sha256":"<hex from upload response>",'
        '"notes":"<short>"}\n'
        "\n"
        '    EDITOR_RESULT: {"verdict":"FAIL","failed_at":"<stage>",'
        '"stderr_tail":"<last 500>","notes":"<short>"}\n'
        "\n"
        "No prose after EDITOR_RESULT. The line must be parseable JSON after the prefix.\n"
    )


# ---------------------------------------------------------------------------
# Session-result -> dispatch-result projection
# ---------------------------------------------------------------------------


def _project_session_result(
    *,
    session_result: EditorSessionResult,
    started: float,
    workspace_dir: Path,
    estimated: float,
) -> dict[str, Any]:
    """Translate EditorSessionResult → dispatch_result shape.

    Four outcomes this distinguishes:
      1. session_result.verdict == "failed" (session-level problem):
         pass through failure_stage from the session.
      2. session_result.verdict == "ok" but final_payload empty or missing
         the EDITOR_RESULT marker: "unparseable_verdict".
      3. final_payload has verdict == "FAIL" (the agent itself reported
         failure): "agent_reported_fail", capture the agent's failed_at
         and notes.
      4. final_payload has verdict == "PASS" but missing required artifact
         fields: "missing_artifacts" — the agent claims success but didn't
         produce the evidence. Rare; usually a prompt compliance slip.
      5. PASS with all required fields: status=ok, project artifacts into
         the result dict.
    """
    latency = round(time.time() - started, 3)

    # Case 1: session-level failure (already has failure_stage set)
    if session_result.verdict != "ok":
        return _failed_from_session(session_result, latency, estimated)

    payload = session_result.final_payload

    # Case 2: no parseable verdict
    if not payload:
        return {
            **_base_failure("unparseable_verdict", estimated),
            "stderr_tail": (
                session_result.transcript_tail
                or session_result.stderr_tail
                or "no EDITOR_RESULT marker found in final message"
            )[:500],
            "cost_usd": round(session_result.cost_usd, 4),
            "latency_s": latency,
            "transcript_tail": session_result.transcript_tail[:500],
        }

    verdict = str(payload.get("verdict") or "").upper()

    # Case 3: agent reported FAIL
    if verdict == "FAIL":
        return {
            **_base_failure("agent_reported_fail", estimated),
            "failed_at": payload.get("failed_at"),
            "agent_notes": payload.get("notes"),
            "stderr_tail": str(payload.get("stderr_tail") or "")[:500],
            "cost_usd": round(session_result.cost_usd, 4),
            "latency_s": latency,
            "transcript_tail": session_result.transcript_tail[:500],
        }

    # Case 4: agent claims PASS but missing artifacts
    missing = [f for f in PASS_REQUIRED_FIELDS if not payload.get(f)]
    if missing:
        return {
            **_base_failure("missing_artifacts", estimated),
            "missing_fields": missing,
            "stderr_tail": (
                f"agent returned PASS but these required fields are empty or missing: "
                f"{', '.join(missing)}"
            )[:500],
            "cost_usd": round(session_result.cost_usd, 4),
            "latency_s": latency,
            "transcript_tail": session_result.transcript_tail[:500],
        }

    # Case 5: PASS with all required fields. Happy path.
    return {
        "status": "ok",
        "provider": "managed_agents",
        "mode": "editor_session",
        "composition_path": str(payload.get("composition_path") or ""),
        "composition_archive_path": str(payload.get("composition_archive_path") or ""),
        "render_path": str(payload.get("render_path") or ""),
        "render_md5": str(payload.get("render_md5") or ""),
        "duration_s": float(payload.get("duration_s") or 0.0),
        "renderer_version": str(payload.get("renderer_version") or ""),
        "uploaded_sha256": str(payload.get("uploaded_sha256") or ""),
        "agent_notes": str(payload.get("notes") or ""),
        "cost_usd": round(session_result.cost_usd, 4),
        "estimated_cost_usd": estimated,
        "quota_cost": 0,
        "latency_s": latency,
        "transcript_tail": session_result.transcript_tail[:500],
        "stderr_tail": session_result.stderr_tail[:500],
    }


# ---------------------------------------------------------------------------
# Failure builders
# ---------------------------------------------------------------------------


def _base_failure(stage: str, estimated: float) -> dict[str, Any]:
    """Common fields for every failure result."""
    return {
        "status": "failed",
        "failure_stage": stage,
        "provider": "managed_agents",
        "mode": "editor_session",
        "composition_path": "",
        "composition_archive_path": "",
        "render_path": "",
        "render_md5": "",
        "duration_s": 0.0,
        "renderer_version": "",
        "estimated_cost_usd": estimated,
        "quota_cost": 0,
    }


def _failure(
    *, stage: str, stderr: str, started: float, estimated: float
) -> dict[str, Any]:
    """Pre-session failure (didn't even get to session.run). cost_usd=0
    because no tokens were spent."""
    return {
        **_base_failure(stage, estimated),
        "stderr_tail": stderr[-500:] if stderr else "",
        "transcript_tail": "",
        "cost_usd": 0.0,
        "latency_s": round(time.time() - started, 3),
    }


def _failed_from_session(
    session_result: EditorSessionResult, latency: float, estimated: float
) -> dict[str, Any]:
    """Session itself failed (not the agent) — e.g., timeout or API error.
    cost_usd may be non-zero if tokens were spent before the failure."""
    stage = session_result.failure_stage or "session_error"
    return {
        **_base_failure(stage, estimated),
        "stderr_tail": session_result.stderr_tail[:500],
        "transcript_tail": session_result.transcript_tail[:500],
        "cost_usd": round(session_result.cost_usd, 4),
        "latency_s": latency,
    }


def _estimate(payload: Mapping[str, Any]) -> float:
    """Extract the operator-seeded cost estimate from payload. Defaults to
    0.0 when not set — the orchestrator is responsible for providing a
    sensible seed (conservative $8–$12 based on manifest size)."""
    try:
        return round(float(payload.get("estimated_cost_usd") or 0.0), 4)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Final-message parsing (what the session's real impl will call)
# ---------------------------------------------------------------------------


def parse_editor_result(text: str) -> dict[str, Any]:
    """Parse the agent's final text block for an EDITOR_RESULT line.

    Returns the parsed JSON object on success, empty dict on no match or
    JSON decode failure. Exposed as a module-level function so concrete
    EditorSession implementations can reuse it — keeps the parsing
    contract in one place.
    """
    m = EDITOR_RESULT_RE.search(text)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    if not isinstance(obj, dict):
        return {}
    return obj
