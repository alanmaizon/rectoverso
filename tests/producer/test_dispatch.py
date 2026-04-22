"""Unit tests for src/producer/dispatch.py — the contract+event wrapper.

Intent coverage:   intent event written before anything else
Architecture:      contracts fire with the right (agent, ctx) routing
Edge cases:        tool raises → failure event + DispatchFailure
                   contract blocks → block event + ContractViolation
                   name mismatch → ValueError before touching events
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.contracts import ContractViolation
from src.producer import (
    DispatchFailure,
    EventLog,
    dispatch,
    open_event_log,
)
from tests.producer.conftest import (
    FakeTool,
    add_minimal_shot,
    minimal_manifest,
)


def _event_kinds(log: EventLog) -> list[str]:
    return [e.kind for e in log.for_shot("sh_001")] + [
        e.kind for e in log.recent() if e.shot_id is None
    ]


# -- happy path ------------------------------------------------------------


def test_successful_dispatch_writes_intent_and_result_events(tmp_path: Path) -> None:
    manifest = minimal_manifest()
    add_minimal_shot(manifest, "sh_001")
    tool = FakeTool(name="shot_judge", result={"outcome": "approved", "judge_score": 0.82})

    with open_event_log(tmp_path / "events.db") as log:
        result = dispatch(
            agent="shot_judge",
            shot_id="sh_001",
            manifest=manifest,
            ctx={},
            tool=tool,
            events=log,
        )

        assert result.agent == "shot_judge"
        assert result.shot_id == "sh_001"
        assert result.result == {"outcome": "approved", "judge_score": 0.82}
        assert result.warns == ()
        assert result.intent_event_id < result.result_event_id

        events = log.for_shot("sh_001")
        assert [e.kind for e in events] == ["dispatch_intent", "dispatch_result"]
        # Result event references intent
        assert events[1].ref_event_id == events[0].event_id


def test_tool_receives_shot_id_and_ctx(tmp_path: Path) -> None:
    manifest = minimal_manifest()
    add_minimal_shot(manifest, "sh_001")
    tool = FakeTool(name="creative_director", result={"feedback_written": 3})

    with open_event_log(tmp_path / "events.db") as log:
        dispatch(
            agent="creative_director",
            shot_id=None,
            manifest=manifest,
            ctx={"trigger": "mid_production"},
            tool=tool,
            events=log,
        )

    assert len(tool.calls) == 1
    shot_id, payload = tool.calls[0]
    assert shot_id is None
    assert payload == {"trigger": "mid_production"}


# -- contract block --------------------------------------------------------


def test_contract_block_writes_block_event_and_raises(tmp_path: Path) -> None:
    """Editor dispatch with unaddressed CD high-priority feedback -> Contract 5 blocks."""
    manifest = minimal_manifest()
    shot = add_minimal_shot(manifest, "sh_001")
    shot["creative_feedback"].append(
        {
            "ts": "2026-04-22T12:00:00Z",
            "from_agent": "creative_director",
            "feedback": "pacing sag",
            "suggestion": "reorder",
            "priority": "high",
            "addressed": False,
        }
    )
    tool = FakeTool(name="editor_agent", result={"composition_path": "artifacts/edit/index.html"})

    with open_event_log(tmp_path / "events.db") as log:
        with pytest.raises(ContractViolation):
            dispatch(
                agent="editor_agent",
                shot_id=None,
                manifest=manifest,
                ctx={},
                tool=tool,
                events=log,
            )

        # Intent and block events written; tool was NOT invoked.
        events = log.recent()
        kinds = [e.kind for e in events]
        assert "contract_block" in kinds
        assert "dispatch_intent" in kinds
        assert "dispatch_result" not in kinds
        assert tool.calls == []


def test_contract_warn_writes_warn_events_and_continues(tmp_path: Path) -> None:
    """CD invocation on a shot with stale judge_feedback -> Contract 4 warns."""
    manifest = minimal_manifest()
    shot = add_minimal_shot(manifest, "sh_001")
    shot["status"] = "approved"
    shot["final"] = {"render_path": "artifacts/sh_001.mp4", "attempt_id": 2}
    shot["attempts"] = [
        {
            "attempt_id": 1,
            "provider": "fal_kling",
            "started_at": "2026-04-22T09:00:00Z",
            "completed_at": "2026-04-22T09:02:00Z",
            "outcome": "rejected",
            "rejection_reason": "auto_judge",
        },
        {
            "attempt_id": 2,
            "provider": "fal_kling",
            "started_at": "2026-04-22T10:00:00Z",
            "completed_at": "2026-04-22T10:02:00Z",
            "outcome": "approved",
            "render_path": "artifacts/sh_001.mp4",
            "approved_by": "shot_judge",
        },
    ]
    shot["judge_feedback"] = [
        {
            "ts": "2026-04-22T09:01:00Z",  # stale — during attempt 1
            "feedback_type": "composition",
            "severity": "warn",
            "observation": "too flat",
        },
        {
            "ts": "2026-04-22T10:01:00Z",  # fresh — during attempt 2
            "feedback_type": "composition",
            "severity": "note",
            "observation": "works",
        },
    ]
    tool = FakeTool(name="creative_director", result={"feedback_written": 0})

    with open_event_log(tmp_path / "events.db") as log:
        result = dispatch(
            agent="creative_director",
            shot_id=None,
            manifest=manifest,
            ctx={},
            tool=tool,
            events=log,
        )

        # Dispatch proceeded; warn event and result event both written.
        assert len(result.warns) == 1
        kinds = [e.kind for e in log.recent()]
        assert "contract_warn" in kinds
        assert "dispatch_result" in kinds
        assert tool.calls != []


# -- tool failure ----------------------------------------------------------


def test_tool_exception_writes_failure_event_and_raises(tmp_path: Path) -> None:
    manifest = minimal_manifest()
    add_minimal_shot(manifest, "sh_001")
    boom = RuntimeError("provider returned 500")
    tool = FakeTool(name="shot_judge", raises=boom)

    with open_event_log(tmp_path / "events.db") as log:
        with pytest.raises(DispatchFailure) as excinfo:
            dispatch(
                agent="shot_judge",
                shot_id="sh_001",
                manifest=manifest,
                ctx={},
                tool=tool,
                events=log,
            )
        assert excinfo.value.cause is boom
        assert excinfo.value.agent == "shot_judge"
        assert excinfo.value.shot_id == "sh_001"

        events = log.for_shot("sh_001")
        kinds = [e.kind for e in events]
        assert kinds == ["dispatch_intent", "dispatch_failure"]
        # Failure event references intent
        assert events[1].ref_event_id == events[0].event_id
        # Payload captures the error
        assert events[1].payload["error_type"] == "RuntimeError"
        assert "500" in events[1].payload["error_str"]


# -- sanity / adapter wiring -----------------------------------------------


def test_tool_name_mismatch_is_caller_bug(tmp_path: Path) -> None:
    manifest = minimal_manifest()
    tool = FakeTool(name="editor_agent")

    with open_event_log(tmp_path / "events.db") as log:
        with pytest.raises(ValueError) as excinfo:
            dispatch(
                agent="shot_judge",
                shot_id=None,
                manifest=manifest,
                ctx={},
                tool=tool,
                events=log,
            )
        assert "does not match" in str(excinfo.value)
        # Rejected before any event was written
        assert log.count() == 0


def test_dispatch_does_not_mutate_manifest(tmp_path: Path) -> None:
    """Dispatch is read-only w.r.t. the manifest; caller projects results."""
    manifest = minimal_manifest()
    add_minimal_shot(manifest, "sh_001")
    tool = FakeTool(name="shot_judge", result={"outcome": "approved"})
    before = dict(manifest["shots"][0])

    with open_event_log(tmp_path / "events.db") as log:
        dispatch(
            agent="shot_judge",
            shot_id="sh_001",
            manifest=manifest,
            ctx={},
            tool=tool,
            events=log,
        )
    after = manifest["shots"][0]
    assert before == after
