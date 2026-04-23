"""End-to-end: one shot through prompt_smith -> render -> shot_judge.

Intent:         demonstrate the full read-validate-dispatch-save cycle.
Architecture:   contracts fire at the right points; events.db + manifest.json
                remain consistent after each step.
Edge cases:     judge rejects first attempt (Contract 2 gate), PromptSmith
                revision with populated judge_notes, second attempt approved.

No real agents are invoked — all tools are FakeTool adapters. But every step
of the Producer runtime (dispatch + event log + atomic manifest save + schema
validation) runs for real.
"""

from __future__ import annotations

from pathlib import Path

from src.producer import (
    dispatch,
    load_manifest,
    open_event_log,
    save_manifest_atomic,
)
from tests.producer.conftest import (
    FakeTool,
    add_minimal_shot,
    minimal_manifest,
)


def _apply_prompt_smith(shot: dict, result: dict) -> None:
    shot["prompt"] = {
        "authored_by": "prompt_smith",
        "primary": result["primary"],
        "negative": result.get("negative", ""),
    }
    shot["status"] = "prompted"


def _apply_router(shot: dict, result: dict) -> None:
    shot["routing"] = {
        "chosen_provider": result["provider"],
        "chosen_model": result["model"],
        "rationale": result["rationale"],
        "decided_by": "router",
        "decided_at": "2026-04-22T10:00:00Z",
        "alternates": result.get("alternates", []),
    }
    shot["status"] = "routed"


def _apply_render_attempt(shot: dict, result: dict, started_at: str) -> dict:
    attempt = {
        "attempt_id": len(shot["attempts"]) + 1,
        "provider": result["provider"],
        "started_at": started_at,
        "completed_at": result["completed_at"],
        "render_path": result["render_path"],
        "outcome": "pending",
        "cost_usd": result.get("cost_usd", 0.0),
    }
    shot["attempts"].append(attempt)
    shot["status"] = "judging"
    return attempt


def _apply_judge(shot: dict, attempt: dict, result: dict) -> None:
    attempt["judge_score"] = result["judge_score"]
    attempt["judge_notes"] = result["judge_notes"]
    attempt["outcome"] = result["outcome"]
    if result["outcome"] == "rejected":
        attempt["rejection_reason"] = result.get("rejection_reason", "auto_judge")
        shot["status"] = "rejected"
    elif result["outcome"] == "approved":
        attempt["approved_by"] = "shot_judge"
        shot["status"] = "approved"
        shot["final"] = {
            "render_path": attempt["render_path"],
            "attempt_id": attempt["attempt_id"],
        }


def test_single_shot_happy_path(tmp_path: Path) -> None:
    """Prompt -> route -> render -> judge approves on first attempt."""
    manifest_path = tmp_path / "manifest.json"
    events_path = tmp_path / "events.db"

    manifest = minimal_manifest()
    add_minimal_shot(manifest, "sh_001")
    save_manifest_atomic(manifest_path, manifest, last_event_id=0)

    prompt_smith = FakeTool(
        name="prompt_smith",
        result={"primary": "wide shot, quiet room", "negative": "noisy, cluttered"},
    )
    router = FakeTool(
        name="router",
        result={
            "provider": "alibaba_wan_27_plus",
            "model": "wan2.7-t2v",
            "rationale": "non-hero non-human -> Wan workhorse",
            "alternates": [],
        },
    )
    renderer = FakeTool(
        name="renderer",
        result={
            "provider": "alibaba_wan_27_plus",
            "completed_at": "2026-04-22T10:05:00Z",
            "render_path": "artifacts/renders/sh_001/v1.mp4",
            "cost_usd": 0.0,
        },
    )
    judge = FakeTool(
        name="shot_judge",
        result={
            "outcome": "approved",
            "judge_score": 0.81,
            "judge_notes": "composition clean, tone matches brief",
        },
    )

    with open_event_log(events_path) as log:
        # 1) PromptSmith (first time — no revision flag)
        res = dispatch("prompt_smith", "sh_001", manifest, {}, prompt_smith, log)
        _apply_prompt_smith(manifest["shots"][0], dict(res.result))
        save_manifest_atomic(manifest_path, manifest, last_event_id=res.result_event_id)

        # 2) Router (deterministic worker; passed through dispatch for uniformity)
        res = dispatch("router", "sh_001", manifest, {}, router, log)
        _apply_router(manifest["shots"][0], dict(res.result))
        save_manifest_atomic(manifest_path, manifest, last_event_id=res.result_event_id)

        # 3) Renderer
        res = dispatch("renderer", "sh_001", manifest, {}, renderer, log)
        attempt = _apply_render_attempt(
            manifest["shots"][0], dict(res.result), started_at="2026-04-22T10:00:00Z"
        )
        save_manifest_atomic(manifest_path, manifest, last_event_id=res.result_event_id)

        # 4) Shot Judge
        res = dispatch("shot_judge", "sh_001", manifest, {}, judge, log)
        _apply_judge(manifest["shots"][0], attempt, dict(res.result))
        save_manifest_atomic(manifest_path, manifest, last_event_id=res.result_event_id)

        final_last_id = res.result_event_id

    # Reload from disk — everything persisted correctly.
    reloaded = load_manifest(manifest_path)
    shot = reloaded.manifest["shots"][0]
    assert reloaded.was_dirty is False
    assert shot["status"] == "approved"
    assert shot["final"]["attempt_id"] == 1
    assert reloaded.manifest["run_state"]["last_event_id"] == final_last_id

    # Events.db tells the same story.
    with open_event_log(events_path) as log:
        shot_events = log.for_shot("sh_001")
        kinds = [e.kind for e in shot_events]
        # 4 dispatches, each with intent + result
        assert kinds == [
            "dispatch_intent", "dispatch_result",  # prompt_smith
            "dispatch_intent", "dispatch_result",  # router
            "dispatch_intent", "dispatch_result",  # renderer
            "dispatch_intent", "dispatch_result",  # shot_judge
        ]


def test_single_shot_reject_then_revise_then_approve(tmp_path: Path) -> None:
    """First attempt rejected with notes; PromptSmith revision gated by Contract 2;
    second attempt approved. Exercises the Shot Judge -> PromptSmith handoff."""
    manifest_path = tmp_path / "manifest.json"
    events_path = tmp_path / "events.db"

    manifest = minimal_manifest()
    add_minimal_shot(manifest, "sh_001")
    save_manifest_atomic(manifest_path, manifest, last_event_id=0)

    # Tools — we reuse instances across attempts via result_fn to vary output.
    def prompt_primary_for(shot_id, payload):
        # first call: initial prompt; second call (revision=True): revised
        if payload.get("revision"):
            return {"primary": "tighter wide shot, emphasize stillness"}
        return {"primary": "wide shot of a quiet room"}

    prompt_smith = FakeTool(name="prompt_smith", result_fn=prompt_primary_for)
    router = FakeTool(
        name="router",
        result={
            "provider": "alibaba_wan_27_plus",
            "model": "wan2.7-t2v",
            "rationale": "workhorse",
            "alternates": [],
        },
    )
    renderer = FakeTool(
        name="renderer",
        result={
            "provider": "alibaba_wan_27_plus",
            "completed_at": "2026-04-22T10:05:00Z",
            "render_path": "artifacts/renders/sh_001/v1.mp4",
            "cost_usd": 0.0,
        },
    )

    def judge_result_for(shot_id, payload):
        # Two attempts: first rejected with notes, second approved.
        attempts = len(manifest["shots"][0]["attempts"])
        if attempts == 1:
            return {
                "outcome": "rejected",
                "judge_score": 0.55,
                "judge_notes": "room feels staged; subject centering conflicts with rule-of-thirds",
                "rejection_reason": "auto_judge",
            }
        return {
            "outcome": "approved",
            "judge_score": 0.82,
            "judge_notes": "adjusted framing reads quieter",
        }

    judge = FakeTool(name="shot_judge", result_fn=judge_result_for)

    with open_event_log(events_path) as log:
        # --- Attempt 1 ---
        res = dispatch("prompt_smith", "sh_001", manifest, {}, prompt_smith, log)
        _apply_prompt_smith(manifest["shots"][0], dict(res.result))
        save_manifest_atomic(manifest_path, manifest, last_event_id=res.result_event_id)

        res = dispatch("router", "sh_001", manifest, {}, router, log)
        _apply_router(manifest["shots"][0], dict(res.result))
        save_manifest_atomic(manifest_path, manifest, last_event_id=res.result_event_id)

        res = dispatch("renderer", "sh_001", manifest, {}, renderer, log)
        attempt1 = _apply_render_attempt(
            manifest["shots"][0], dict(res.result), started_at="2026-04-22T10:00:00Z"
        )
        save_manifest_atomic(manifest_path, manifest, last_event_id=res.result_event_id)

        res = dispatch("shot_judge", "sh_001", manifest, {}, judge, log)
        _apply_judge(manifest["shots"][0], attempt1, dict(res.result))
        save_manifest_atomic(manifest_path, manifest, last_event_id=res.result_event_id)

        assert manifest["shots"][0]["status"] == "rejected"
        assert attempt1["judge_notes"]  # Contract 2's precondition

        # --- Attempt 2 — revision ---
        # Contract 2 (shot_judge -> prompt_smith) should PASS because
        # judge_notes is populated on attempts[-1].
        res = dispatch(
            "prompt_smith",
            "sh_001",
            manifest,
            {"revision": True},
            prompt_smith,
            log,
        )
        _apply_prompt_smith(manifest["shots"][0], dict(res.result))
        manifest["shots"][0]["status"] = "prompted"
        save_manifest_atomic(manifest_path, manifest, last_event_id=res.result_event_id)

        res = dispatch("renderer", "sh_001", manifest, {}, renderer, log)
        attempt2 = _apply_render_attempt(
            manifest["shots"][0], dict(res.result), started_at="2026-04-22T10:10:00Z"
        )
        save_manifest_atomic(manifest_path, manifest, last_event_id=res.result_event_id)

        res = dispatch("shot_judge", "sh_001", manifest, {}, judge, log)
        _apply_judge(manifest["shots"][0], attempt2, dict(res.result))
        save_manifest_atomic(manifest_path, manifest, last_event_id=res.result_event_id)

        final_last_id = res.result_event_id

    reloaded = load_manifest(manifest_path)
    shot = reloaded.manifest["shots"][0]
    assert shot["status"] == "approved"
    assert shot["final"]["attempt_id"] == 2
    assert len(shot["attempts"]) == 2
    assert shot["attempts"][0]["outcome"] == "rejected"
    assert shot["attempts"][1]["outcome"] == "approved"
    assert reloaded.manifest["run_state"]["last_event_id"] == final_last_id


def test_revision_without_judge_notes_blocks_at_contract(tmp_path: Path) -> None:
    """Silent-breakage case: Producer attempts a revision on a rejected attempt
    whose judge_notes are empty. Contract 2 must block before PromptSmith runs."""
    manifest_path = tmp_path / "manifest.json"
    events_path = tmp_path / "events.db"

    manifest = minimal_manifest()
    shot = add_minimal_shot(manifest, "sh_001")
    shot["attempts"] = [
        {
            "attempt_id": 1,
            "provider": "alibaba_wan_27_plus",
            "started_at": "2026-04-22T10:00:00Z",
            "completed_at": "2026-04-22T10:02:00Z",
            "outcome": "rejected",
            "rejection_reason": "auto_judge",
            "judge_notes": "",  # <-- the silent-breakage trigger
        }
    ]
    shot["status"] = "rejected"
    save_manifest_atomic(manifest_path, manifest, last_event_id=0)

    prompt_smith = FakeTool(name="prompt_smith", result={"primary": "paraphrased"})

    with open_event_log(events_path) as log:
        from src.contracts import ContractViolation
        import pytest

        with pytest.raises(ContractViolation):
            dispatch(
                "prompt_smith",
                "sh_001",
                manifest,
                {"revision": True},
                prompt_smith,
                log,
            )
        # Tool was NOT invoked; event log has intent + contract_block.
        assert prompt_smith.calls == []
        kinds = [e.kind for e in log.recent()]
        assert "contract_block" in kinds
        assert "dispatch_result" not in kinds

    # Manifest unchanged.
    reloaded = load_manifest(manifest_path)
    assert reloaded.manifest["shots"][0]["status"] == "rejected"
