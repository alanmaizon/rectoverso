"""FilmOrchestrator hermetic tests.

The orchestrator wires together render → judge (→ revise) per-shot. Tests
use a stubbed ToolSet to feed canned results; we assert the orchestrator's
projection + retry + escalation logic without touching any real API or
subprocess.

The first test (test_orchestrator_reproduces_sh_005_v3_escalation) drives
the live fixture captured at tests/fixtures/orchestrator_sh_005_v3.json.
That fixture is the ANCHOR: if the orchestrator's state-machine wiring
drifts, this test catches it against a validated real-world trajectory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.producer import (
    EventLog,
    FilmOrchestrator,
    RetryPolicy,
    ToolSet,
    open_event_log,
    save_manifest_atomic,
)
from tests.producer.conftest import add_minimal_shot, minimal_manifest


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "orchestrator_sh_005_v3.json"


# ---------------------------------------------------------------------------
# Tool stubs
# ---------------------------------------------------------------------------


def _stub_render(manifest: dict, result: dict, now_iso: str = "2026-04-23T18:00:00.000000Z"):
    """Build a render stub that projects `result` into the shot the way
    render_cmd does: append an attempt, flip status to judging on success."""

    def _render(*, shot: dict, attempt_id: int, run_mode: str) -> dict:
        # Mirror render_cmd's pre-dispatch scaffold.
        shot.setdefault("attempts", []).append(
            {
                "attempt_id": attempt_id,
                "provider": result["provider"],
                "started_at": now_iso,
                "outcome": "pending",
            }
        )
        shot["status"] = "rendering"
        shot.setdefault("history", []).append(
            {"ts": now_iso, "event": "rendering", "by": "renderer",
             "detail": f"attempt {attempt_id}"}
        )
        # Project the result into the last attempt.
        attempt = shot["attempts"][-1]
        attempt["completed_at"] = now_iso
        attempt["latency_s"] = float(result.get("latency_s", 0.0))
        attempt["cost_usd"] = float(result.get("cost_usd", 0.0))
        if result.get("status") == "ok":
            attempt["outcome"] = "pending"
            attempt["render_path"] = result["render_path"]
            shot["status"] = "judging"
            shot["history"].append(
                {"ts": now_iso, "event": "judging", "by": "renderer",
                 "detail": f"attempt {attempt_id} rendered"}
            )
        else:
            attempt["outcome"] = "failed"
            attempt["error"] = result.get("stderr_tail") or "render failed"
            shot["status"] = "failed"
        return result

    return _render


def _stub_judge(manifest: dict, result: dict, now_iso: str = "2026-04-23T18:01:00.000000Z"):
    """Build a judge stub that projects `result` the way judge_cmd does:
    writes judge_score/notes/outcome onto attempts[-1] + transitions
    shot.status. Handles the outcome=escalated → attempt.outcome=rejected
    translation and populates escalation_reason."""

    def _judge(*, shot: dict, attempt_id: int) -> dict:
        attempt = shot["attempts"][-1]
        outcome = result["outcome"]
        attempt["judge_score"] = float(result["judge_score"])
        attempt["judge_notes"] = result["judge_notes"]
        attempt["outcome"] = outcome if outcome != "escalated" else "rejected"
        if outcome == "approved":
            attempt["approved_by"] = "shot_judge"
        elif outcome == "rejected":
            attempt["rejection_reason"] = result.get("rejection_reason") or "auto_judge"
        elif outcome == "escalated":
            attempt["rejection_reason"] = result.get("rejection_reason") or "auto_judge"
            if result.get("escalation_reason"):
                attempt["escalation_reason"] = result["escalation_reason"]

        # Feedback rows onto shot.judge_feedback.
        for fb in result.get("feedback") or []:
            shot.setdefault("judge_feedback", []).append({
                "ts": now_iso,
                "feedback_type": fb.get("feedback_type", "composition"),
                "severity": fb.get("severity", "note"),
                "observation": fb.get("observation", ""),
                "from_agent": "shot_judge",
            })

        # Status transition matching judge_cmd's projection.
        if outcome == "approved":
            shot["status"] = "approved"
            shot["final"] = {
                "render_path": attempt["render_path"],
                "attempt_id": attempt_id,
            }
        elif outcome == "rejected":
            shot["status"] = "rejected"
        elif outcome == "escalated":
            shot["status"] = "escalated"

        history_detail = f"score={result['judge_score']:.3f} mode=vision"
        if outcome == "escalated" and result.get("escalation_reason"):
            history_detail += f" escalation_reason={result['escalation_reason']}"
        shot.setdefault("history", []).append({
            "ts": now_iso,
            "event": f"judged_{outcome}",
            "by": "shot_judge",
            "detail": history_detail,
        })
        return result

    return _judge


def _stub_revise_never_called(**_kwargs) -> dict:
    raise AssertionError("revise should not be called in this test")


# ---------------------------------------------------------------------------
# The anchor test: sh_005 v3 fixture
# ---------------------------------------------------------------------------


def test_orchestrator_reproduces_sh_005_v3_escalation(tmp_path: Path) -> None:
    """Drive the orchestrator's per-shot loop with the canned sh_005 v3
    tool outputs. Assert the final state matches the fixture's expected_final.

    This is the regression anchor: if render/judge projection wiring drifts,
    or if the volatility escalation path breaks, this test catches it."""
    fixture = json.loads(FIXTURE_PATH.read_text())

    # Build a minimal manifest with the starting shot state already populated
    # (v1 approved + v2 rejected in attempts).
    manifest = minimal_manifest()
    manifest["shots"] = [fixture["starting_shot"]]
    manifest_path = tmp_path / "manifest.json"
    events_db = tmp_path / "events.db"

    render_stub = _stub_render(manifest, fixture["tool_results"]["render"])
    judge_stub = _stub_judge(manifest, fixture["tool_results"]["judge"])

    with open_event_log(events_db) as events:
        tools = ToolSet(
            render=render_stub,
            judge=judge_stub,
            revise=_stub_revise_never_called,   # approved in one cycle (no retry)
        )
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=manifest_path,
            events=events,
            tools=tools,
            run_mode="submission",
        )
        # Starting shot is in "rejected" status with 2 attempts. The
        # orchestrator dispatches a fresh render + judge pair. The judge's
        # escalation is already baked into the canned result (matches the
        # _decide_outcome logic under attempts_count=3 + volatile priors).
        result = orch.run()

    expected = fixture["expected_final"]
    assert result.shot_count == 1
    assert result.escalated_count == 1
    assert result.approved_count == 0
    assert result.escalations_by_reason.get("volatile_scores") == 1

    shot = manifest["shots"][0]
    assert shot["status"] == expected["shot_status"]
    assert len(shot["attempts"]) == expected["attempts_count"]

    final_attempt = shot["attempts"][-1]
    assert final_attempt["attempt_id"] == expected["final_attempt"]["attempt_id"]
    assert final_attempt["outcome"] == expected["final_attempt"]["outcome"]
    assert final_attempt["judge_score"] == expected["final_attempt"]["judge_score"]
    assert final_attempt["escalation_reason"] == expected["final_attempt"]["escalation_reason"]
    assert final_attempt["rejection_reason"] == expected["final_attempt"]["rejection_reason"]
    assert final_attempt["render_path"] == expected["final_attempt"]["render_path"]

    # History must carry the projected events.
    events_list = [h["event"] for h in shot["history"]]
    for required in expected["history_events_include"]:
        assert required in events_list, f"missing history event: {required}"

    # Per-shot summary row is populated.
    assert result.shots[0].shot_id == "sh_005"
    assert result.shots[0].final_status == "escalated"
    assert result.shots[0].escalation_reason == "volatile_scores"


# ---------------------------------------------------------------------------
# Happy-path single-shot test (fresh shot, approves first try)
# ---------------------------------------------------------------------------


def test_orchestrator_happy_path_single_shot_approves(tmp_path: Path) -> None:
    """Fresh shot, first render approves — no revise, no retry."""
    manifest = minimal_manifest()
    shot = add_minimal_shot(manifest, "sh_001")
    shot["status"] = "prompted"
    shot["routing"]["chosen_provider"] = "alibaba_wan_2_7_plus"
    shot["routing"]["chosen_model"] = "wan2.7-t2v"

    render_result = {
        "status": "ok",
        "provider": "alibaba_wan_2_7_plus",
        "render_path": "artifacts/renders/sh_001/v1.mp4",
        "cost_usd": 0.0,
        "quota_cost": 1,
        "latency_s": 45.0,
    }
    judge_result = {
        "judge_score": 0.88,
        "composition": 0.90,
        "prompt_adherence": 0.85,
        "continuity": 0.90,
        "artifact_flag": False,
        "judge_notes": "Clean take, no issues.",
        "feedback": [],
        "outcome": "approved",
        "rejection_reason": None,
        "escalation_reason": None,
    }

    with open_event_log(tmp_path / "events.db") as events:
        tools = ToolSet(
            render=_stub_render(manifest, render_result),
            judge=_stub_judge(manifest, judge_result),
            revise=_stub_revise_never_called,
        )
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=tools,
            run_mode="testing",
        )
        result = orch.run()

    assert result.approved_count == 1
    assert result.escalated_count == 0
    assert result.shots[0].final_status == "approved"
    assert shot["status"] == "approved"
    assert shot["final"]["attempt_id"] == 1


# ---------------------------------------------------------------------------
# Retry loop — rejected then approved
# ---------------------------------------------------------------------------


def test_orchestrator_retry_loop_rejected_then_approved(tmp_path: Path) -> None:
    """v1 rejected → revise → v2 approved. Covers the core Contract-2 cycle."""
    manifest = minimal_manifest()
    shot = add_minimal_shot(manifest, "sh_001")
    shot["status"] = "prompted"
    shot["routing"]["chosen_provider"] = "alibaba_wan_2_7_plus"
    shot["routing"]["chosen_model"] = "wan2.7-t2v"

    render_result = {
        "status": "ok",
        "provider": "alibaba_wan_2_7_plus",
        "render_path": "artifacts/renders/sh_001/v.mp4",
        "cost_usd": 0.0,
        "latency_s": 40.0,
    }
    # Alternate judge verdicts: first rejected, then approved.
    judge_calls = iter([
        {
            "judge_score": 0.65,
            "composition": 0.60,
            "prompt_adherence": 0.65,
            "continuity": 0.70,
            "artifact_flag": False,
            "judge_notes": "Horizon tilted; motion reads faster than prompt.",
            "feedback": [],
            "outcome": "rejected",
            "rejection_reason": "auto_judge",
        },
        {
            "judge_score": 0.82,
            "composition": 0.85,
            "prompt_adherence": 0.80,
            "continuity": 0.80,
            "artifact_flag": False,
            "judge_notes": "Horizon level; pacing matches brief.",
            "feedback": [],
            "outcome": "approved",
            "rejection_reason": None,
            "escalation_reason": None,
        },
    ])

    def _judge(**kwargs):
        return _stub_judge(manifest, next(judge_calls))(**kwargs)

    revise_calls = [0]

    def _revise(*, shot: dict) -> dict:
        # Minimal revise: flip the prompt, keep status as "rejected"
        # (render_cmd accepts rejected as retry state).
        shot["prompt"]["primary"] = "revised: level horizon, slow pace"
        shot["prompt"]["authored_by"] = "prompt_smith"
        revise_calls[0] += 1
        return {"primary": shot["prompt"]["primary"]}

    with open_event_log(tmp_path / "events.db") as events:
        tools = ToolSet(
            render=_stub_render(manifest, render_result),
            judge=_judge,
            revise=_revise,
        )
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=tools,
            run_mode="testing",
        )
        result = orch.run()

    assert result.approved_count == 1
    assert revise_calls[0] == 1, "revise must fire exactly once between v1 and v2"
    assert len(shot["attempts"]) == 2
    assert shot["attempts"][0]["outcome"] == "rejected"
    assert shot["attempts"][1]["outcome"] == "approved"


# ---------------------------------------------------------------------------
# Escalation on max-attempts-exhausted (non-approving scores all 3 tries)
# ---------------------------------------------------------------------------


def test_orchestrator_escalates_on_max_attempts_all_rejected(tmp_path: Path) -> None:
    """All 3 attempts score mid-range (rejected). Orchestrator escalates the
    shot after the 3rd attempt rather than looping forever."""
    manifest = minimal_manifest()
    shot = add_minimal_shot(manifest, "sh_001")
    shot["status"] = "prompted"
    shot["routing"]["chosen_provider"] = "alibaba_wan_2_7_plus"
    shot["routing"]["chosen_model"] = "wan2.7-t2v"

    render_result = {
        "status": "ok",
        "provider": "alibaba_wan_2_7_plus",
        "render_path": "artifacts/renders/sh_001/v.mp4",
        "cost_usd": 0.0,
        "latency_s": 40.0,
    }
    # All 3 judge calls return rejected/auto_judge until max-attempts kicks in.
    judge_sequence = [
        {
            "judge_score": 0.60, "composition": 0.60, "prompt_adherence": 0.60,
            "continuity": 0.65, "artifact_flag": False,
            "judge_notes": "Off-center; tilt.", "feedback": [],
            "outcome": "rejected", "rejection_reason": "auto_judge",
        },
        {
            "judge_score": 0.63, "composition": 0.65, "prompt_adherence": 0.60,
            "continuity": 0.65, "artifact_flag": False,
            "judge_notes": "Better but still off.", "feedback": [],
            "outcome": "rejected", "rejection_reason": "auto_judge",
        },
        # Third attempt: max-attempts hit with a non-approving score →
        # escalated/max_attempts_exhausted.
        {
            "judge_score": 0.68, "composition": 0.70, "prompt_adherence": 0.65,
            "continuity": 0.70, "artifact_flag": False,
            "judge_notes": "Closer but not landing.", "feedback": [],
            "outcome": "escalated",
            "rejection_reason": "auto_judge",
            "escalation_reason": "max_attempts_exhausted",
        },
    ]
    judge_iter = iter(judge_sequence)

    def _judge(**kwargs):
        return _stub_judge(manifest, next(judge_iter))(**kwargs)

    def _revise(*, shot: dict) -> dict:
        shot["prompt"]["primary"] = shot["prompt"]["primary"] + " [revised]"
        return {"primary": shot["prompt"]["primary"]}

    with open_event_log(tmp_path / "events.db") as events:
        tools = ToolSet(
            render=_stub_render(manifest, render_result),
            judge=_judge,
            revise=_revise,
        )
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=tools,
            run_mode="testing",
        )
        result = orch.run()

    assert result.escalated_count == 1
    assert result.escalations_by_reason.get("max_attempts_exhausted") == 1
    assert shot["status"] == "escalated"
    assert len(shot["attempts"]) == 3


# ---------------------------------------------------------------------------
# Resumability — already-approved shots skipped
# ---------------------------------------------------------------------------


def test_orchestrator_skips_approved_shots_on_resume(tmp_path: Path) -> None:
    """A shot already in status=approved is not touched. Tools must not fire."""
    manifest = minimal_manifest()
    approved = add_minimal_shot(manifest, "sh_001")
    approved["status"] = "approved"
    approved["final"] = {
        "render_path": "artifacts/renders/sh_001/v1.mp4",
        "attempt_id": 1,
    }
    approved["attempts"] = [{
        "attempt_id": 1,
        "provider": "alibaba_wan_2_7_plus",
        "started_at": "2026-04-23T10:00:00.000000Z",
        "completed_at": "2026-04-23T10:01:00.000000Z",
        "outcome": "approved",
        "render_path": "artifacts/renders/sh_001/v1.mp4",
        "judge_score": 0.85,
        "approved_by": "shot_judge",
    }]

    def _never(**_kwargs):
        raise AssertionError("tools should NOT fire on already-approved shots")

    with open_event_log(tmp_path / "events.db") as events:
        tools = ToolSet(render=_never, judge=_never, revise=_never)
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=tools,
            run_mode="testing",
        )
        result = orch.run()

    assert result.approved_count == 1
    assert result.shots[0].final_status == "approved"


# ---------------------------------------------------------------------------
# Budget halt
# ---------------------------------------------------------------------------


def test_orchestrator_halts_on_budget_refusal(tmp_path: Path) -> None:
    """A cap-zero manifest refuses any render. Orchestrator halts, remaining
    shots are marked budget_halted (not processed)."""
    manifest = minimal_manifest()
    manifest["budget"]["cap_usd"] = 0.01   # any spend exceeds this

    # Shot routed to a paid provider so the cost estimate is > cap.
    shot = add_minimal_shot(manifest, "sh_001")
    shot["status"] = "prompted"
    shot["routing"]["chosen_provider"] = "fal_kling_2_1_pro"
    shot["routing"]["chosen_model"] = "fal-ai/kling-video/v2.1/pro/image-to-video"

    shot2 = add_minimal_shot(manifest, "sh_002")
    shot2["status"] = "prompted"
    shot2["routing"]["chosen_provider"] = "fal_kling_2_1_pro"
    shot2["routing"]["chosen_model"] = "fal-ai/kling-video/v2.1/pro/image-to-video"

    def _never(**_kwargs):
        raise AssertionError("render/judge should not fire after budget halt")

    with open_event_log(tmp_path / "events.db") as events:
        tools = ToolSet(render=_never, judge=_never, revise=_never)
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=tools,
            run_mode="submission",
        )
        result = orch.run()

    assert result.budget_halted_count == 2    # both shots halted
    assert result.halted_reason is not None
    assert "sh_001" in result.halted_reason   # first halt was on sh_001
    # No attempts ever recorded.
    assert len(manifest["shots"][0]["attempts"]) == 0
    assert len(manifest["shots"][1]["attempts"]) == 0
