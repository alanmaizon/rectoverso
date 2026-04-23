"""EditorTool dispatcher tests.

Focus: the Producer-side orchestration — what the tool does BEFORE and AFTER
the session, and how it translates session outcomes into dispatch-result
dicts. The session itself is stubbed (real Managed Agents API is out of
scope for hermetic tests; integration with live sessions happens in
scratch/ probes).

Coverage target per user spec:
    - happy path: session PASS → dispatch_result with cost_usd surfaced
    - shot_id != None → shot_id_not_none
    - manifest_missing → stage set, no session call
    - workspace creation error → workspace_error
    - session raises TimeoutError → timeout stage
    - session raises arbitrary Exception → session_error stage
    - session returns verdict="failed" → pass through its failure_stage
    - session returns verdict="ok" but final_payload empty → unparseable_verdict
    - agent reports FAIL → agent_reported_fail, notes captured
    - agent PASS with missing required fields → missing_artifacts
    - parse_editor_result module-level utility
    - system prompt + skills frozen at construction (cache discipline)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.producer import (
    EditorSession,
    EditorSessionResult,
    EditorTool,
    parse_editor_result,
)


# ---------------------------------------------------------------------------
# Stub sessions — implement the EditorSession Protocol for tests
# ---------------------------------------------------------------------------


@dataclass
class _StubSession:
    """Canned-result session. Records each call for assertions."""

    canned: EditorSessionResult | BaseException = None   # type: ignore[assignment]
    calls: list[dict[str, Any]] = None                    # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

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
        self.calls.append({
            "system_prompt": system_prompt,
            "skills": tuple(skills),
            "model": model,
            "apt_packages": tuple(apt_packages),
            "workspace_dir": workspace_dir,
            "initial_message": initial_message,
            "timeout_s": timeout_s,
        })
        if isinstance(self.canned, BaseException):
            raise self.canned
        return self.canned


def _ok_session_result(
    *,
    composition_path: str = "artifacts/edit/index.html",
    composition_archive_path: str = "artifacts/edit/composition.zip",
    render_path: str = "artifacts/edit/out.mp4",
    render_md5: str = "abcdef0123456789abcdef0123456789",
    duration_s: float = 58.5,
    renderer_version: str = "0.4.12",
    uploaded_sha256: str = "f" * 64,
    cost_usd: float = 9.72,
) -> EditorSessionResult:
    return EditorSessionResult(
        verdict="ok",
        failure_stage=None,
        final_payload={
            "verdict": "PASS",
            "composition_path": composition_path,
            "composition_archive_path": composition_archive_path,
            "render_path": render_path,
            "render_md5": render_md5,
            "duration_s": duration_s,
            "renderer_version": renderer_version,
            "uploaded_sha256": uploaded_sha256,
            "notes": "3 render iterations, lint clean",
        },
        cost_usd=cost_usd,
        latency_s=1847.2,
        transcript_tail="[text] render succeeded; md5=abcdef...",
        stderr_tail="",
    )


def _build_tool(session: EditorSession) -> EditorTool:
    """Build an EditorTool with a stub system prompt so tests don't depend
    on the prompts file contents."""
    return EditorTool(
        session=session,
        system_prompt="# STUB Editor Agent prompt\nBe deterministic.",
    )


def _base_payload(tmp_path: Path) -> dict[str, Any]:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    return {
        "manifest_path": manifest,
        "workspace_dir": tmp_path / "workspace",
        "brief_slice": {"title": "Test Film", "target_duration_s": 60.0},
        "estimated_cost_usd": 10.0,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_dispatch_result_with_cost(tmp_path: Path) -> None:
    """Session PASS → status=ok, all artifact fields populated, cost surfaced."""
    sess = _StubSession(canned=_ok_session_result(cost_usd=9.72))
    tool = _build_tool(sess)

    result = tool(None, _base_payload(tmp_path))

    assert result["status"] == "ok"
    assert result["provider"] == "managed_agents"
    assert result["mode"] == "editor_session"
    assert result["composition_path"] == "artifacts/edit/index.html"
    assert result["composition_archive_path"] == "artifacts/edit/composition.zip"
    assert result["render_path"] == "artifacts/edit/out.mp4"
    assert result["render_md5"] == "abcdef0123456789abcdef0123456789"
    assert result["duration_s"] == 58.5
    assert result["renderer_version"] == "0.4.12"
    # cost_usd IS NON-ZERO — this is where Editor hits the Anthropic budget
    assert result["cost_usd"] == 9.72
    assert result["estimated_cost_usd"] == 10.0
    assert result["quota_cost"] == 0
    # Session was called exactly once with the frozen-at-construction prompt
    assert len(sess.calls) == 1
    call = sess.calls[0]
    assert "STUB Editor Agent" in call["system_prompt"]
    assert call["skills"] == ("hyperframes", "hyperframes-cli", "gsap")
    assert call["apt_packages"] == ("ffmpeg",)
    assert call["model"] == "claude-opus-4-7"


def test_initial_message_mentions_manifest_and_workspace(tmp_path: Path) -> None:
    """Smoke check on the kickoff message — must reference manifest + workspace
    and include the EDITOR_RESULT protocol spec so the agent knows how to
    terminate."""
    sess = _StubSession(canned=_ok_session_result())
    tool = _build_tool(sess)

    tool(None, _base_payload(tmp_path))
    msg = sess.calls[0]["initial_message"]
    assert "manifest" in msg
    assert "EDITOR_RESULT" in msg
    assert '"verdict":"PASS"' in msg or '"verdict":"FAIL"' in msg
    # Crucial pointer so agent doesn't ingest codec-heterogeneous raw renders
    assert "final.normalized_path" in msg


# ---------------------------------------------------------------------------
# Pre-session failures (zero cost)
# ---------------------------------------------------------------------------


def test_shot_id_not_none_fails_fast(tmp_path: Path) -> None:
    sess = _StubSession(canned=_ok_session_result())
    tool = _build_tool(sess)

    result = tool("sh_001", _base_payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "shot_id_not_none"
    assert result["cost_usd"] == 0.0
    # Session MUST NOT be called on a malformed dispatch
    assert len(sess.calls) == 0


def test_manifest_missing_fails_before_session(tmp_path: Path) -> None:
    sess = _StubSession(canned=_ok_session_result())
    tool = _build_tool(sess)

    payload = _base_payload(tmp_path)
    payload["manifest_path"] = tmp_path / "does_not_exist.json"
    result = tool(None, payload)
    assert result["status"] == "failed"
    assert result["failure_stage"] == "manifest_missing"
    assert result["cost_usd"] == 0.0
    assert len(sess.calls) == 0


def test_workspace_error_fails_before_session(tmp_path: Path, monkeypatch) -> None:
    """If mkdir fails (e.g., permission denied on a real production mount),
    we fail out cleanly without burning Anthropic budget."""
    sess = _StubSession(canned=_ok_session_result())
    tool = _build_tool(sess)

    # Force mkdir to raise
    original_mkdir = Path.mkdir

    def boom(self, *args, **kwargs):
        if "workspace" in str(self):
            raise OSError("permission denied")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", boom)
    result = tool(None, _base_payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "workspace_error"
    assert result["cost_usd"] == 0.0
    assert len(sess.calls) == 0


# ---------------------------------------------------------------------------
# Session-level failures (cost may be non-zero — tokens spent before failure)
# ---------------------------------------------------------------------------


def test_session_timeout_surfaces_timeout_stage(tmp_path: Path) -> None:
    """TimeoutError bubbling out of session.run → failure_stage=timeout."""
    sess = _StubSession(canned=TimeoutError("session exceeded 7200s cap"))
    tool = _build_tool(sess)

    result = tool(None, _base_payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "timeout"
    # stderr_tail captures the timeout message for post-mortem
    assert "7200s" in result["stderr_tail"]
    # pre-session estimated_cost_usd still echoed (audit trail)
    assert result["estimated_cost_usd"] == 10.0


def test_session_exception_surfaces_session_error_stage(tmp_path: Path) -> None:
    """Arbitrary exception from session.run → failure_stage=session_error."""
    sess = _StubSession(canned=RuntimeError("Anthropic API 500"))
    tool = _build_tool(sess)

    result = tool(None, _base_payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "session_error"
    assert "RuntimeError" in result["stderr_tail"]
    assert "Anthropic API 500" in result["stderr_tail"]


def test_session_reports_failure_passes_through_stage(tmp_path: Path) -> None:
    """Session itself returns verdict=failed (e.g., environment provisioning
    broke before agent even ran) — pass through failure_stage."""
    sess = _StubSession(
        canned=EditorSessionResult(
            verdict="failed",
            failure_stage="session_error",
            final_payload={},
            cost_usd=0.02,
            latency_s=5.1,
            transcript_tail="failed to provision sandbox",
            stderr_tail="apt install timed out",
        )
    )
    tool = _build_tool(sess)

    result = tool(None, _base_payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "session_error"
    # Partial cost is still billable — session did some work
    assert result["cost_usd"] == 0.02


# ---------------------------------------------------------------------------
# Verdict-parsing failures
# ---------------------------------------------------------------------------


def test_unparseable_verdict_when_session_returns_empty_payload(tmp_path: Path) -> None:
    """Session ran clean (verdict=ok) but agent never produced an
    EDITOR_RESULT line — treated as unparseable_verdict, NOT ok."""
    sess = _StubSession(
        canned=EditorSessionResult(
            verdict="ok",
            failure_stage=None,
            final_payload={},
            cost_usd=3.5,
            latency_s=900.0,
            transcript_tail="[text] I'll start by running hyperframes init...",
        )
    )
    tool = _build_tool(sess)

    result = tool(None, _base_payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "unparseable_verdict"
    # Transcript tail is preserved for debugging
    assert "hyperframes init" in result["transcript_tail"]
    # Cost still accrued
    assert result["cost_usd"] == 3.5


def test_agent_reported_fail_captures_notes(tmp_path: Path) -> None:
    """Agent's final message says PROBE_RESULT: FAIL → agent_reported_fail
    stage, failed_at and notes bubble up to the dispatch_result."""
    sess = _StubSession(
        canned=EditorSessionResult(
            verdict="ok",
            failure_stage=None,
            final_payload={
                "verdict": "FAIL",
                "failed_at": "render",
                "stderr_tail": "ffmpeg: unknown codec",
                "notes": "render loop exhausted after 3 iterations",
            },
            cost_usd=6.4,
            latency_s=3200.0,
            transcript_tail="[text] render attempt 3 failed; escalating",
        )
    )
    tool = _build_tool(sess)

    result = tool(None, _base_payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "agent_reported_fail"
    assert result["failed_at"] == "render"
    assert "render loop exhausted" in result["agent_notes"]
    assert result["cost_usd"] == 6.4


def test_pass_with_missing_fields_fails_as_missing_artifacts(tmp_path: Path) -> None:
    """Agent claims PASS but didn't fill in render_md5. The dispatch must
    NOT claim success — the artifact chain is incomplete."""
    sess = _StubSession(
        canned=EditorSessionResult(
            verdict="ok",
            failure_stage=None,
            final_payload={
                "verdict": "PASS",
                "composition_path": "artifacts/edit/index.html",
                "render_path": "artifacts/edit/out.mp4",
                # render_md5 + duration_s deliberately omitted
            },
            cost_usd=7.1,
            latency_s=1800.0,
        )
    )
    tool = _build_tool(sess)

    result = tool(None, _base_payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "missing_artifacts"
    assert "render_md5" in result["missing_fields"]
    assert "duration_s" in result["missing_fields"]


# ---------------------------------------------------------------------------
# Cache discipline (system prompt + skills frozen at construction)
# ---------------------------------------------------------------------------


def test_system_prompt_frozen_after_construction(tmp_path: Path) -> None:
    """Mutating the prompt file between dispatches MUST NOT affect the
    second call — each EditorTool instance holds its own snapshot. This
    is load-bearing for Managed Agents prompt-cache preservation."""
    sess = _StubSession(canned=_ok_session_result())
    tool = EditorTool(session=sess, system_prompt="PROMPT_V1")

    tool(None, _base_payload(tmp_path))
    # First call captured PROMPT_V1
    assert sess.calls[0]["system_prompt"] == "PROMPT_V1"

    # Try to "change" it — there's no public setter, so this is just asserting
    # the tool does not read any external state between calls.
    tool(None, _base_payload(tmp_path))
    assert sess.calls[1]["system_prompt"] == "PROMPT_V1"


def test_skills_and_model_frozen_after_construction(tmp_path: Path) -> None:
    """Custom skills/model at construction stick for the tool's lifetime."""
    sess = _StubSession(canned=_ok_session_result())
    tool = EditorTool(
        session=sess,
        system_prompt="STUB",
        skills=("hyperframes", "custom-skill"),
        model="claude-opus-4-6",
    )

    tool(None, _base_payload(tmp_path))
    call = sess.calls[0]
    assert call["skills"] == ("hyperframes", "custom-skill")
    assert call["model"] == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# parse_editor_result helper
# ---------------------------------------------------------------------------


def test_parse_editor_result_happy_path() -> None:
    """Module-level parser — this is what concrete EditorSession
    implementations will call to extract the verdict from the transcript."""
    text = (
        "Some intro text.\n"
        "I ran the lint command and it passed.\n"
        "\n"
        'EDITOR_RESULT: {"verdict":"PASS","composition_path":"x.html",'
        '"render_path":"y.mp4","render_md5":"abc","duration_s":10.0}'
    )
    result = parse_editor_result(text)
    assert result["verdict"] == "PASS"
    assert result["duration_s"] == 10.0


def test_parse_editor_result_handles_fail_verdict() -> None:
    text = 'EDITOR_RESULT: {"verdict":"FAIL","failed_at":"lint"}'
    result = parse_editor_result(text)
    assert result["verdict"] == "FAIL"
    assert result["failed_at"] == "lint"


def test_parse_editor_result_no_marker_returns_empty_dict() -> None:
    result = parse_editor_result("No marker here, just prose.")
    assert result == {}


def test_parse_editor_result_malformed_json_returns_empty_dict() -> None:
    result = parse_editor_result('EDITOR_RESULT: {"verdict": "PASS", bad json')
    assert result == {}


def test_parse_editor_result_non_object_returns_empty_dict() -> None:
    result = parse_editor_result('EDITOR_RESULT: ["list", "not", "object"]')
    assert result == {}
