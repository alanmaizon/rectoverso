"""Unit tests for src.producer.screenwriter — ScreenwriterTool adapter.

Intent:        brief -> shot list shape; validation catches LLM misbehavior
Architecture: LLMClient injected via StubClient; no network
Edge cases:    missing fields, bad motion_level, duration bounds, dialogue shape,
               JSON object vs array, dispatch integration
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from src.producer import ScreenwriterTool
from src.producer.llm import LLMResponse


@dataclass
class StubClient:
    text: str
    model: str = "test-model"

    def create_message(
        self,
        *,
        model: str,
        system: Any,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        return LLMResponse(text=self.text, model=model, usage={"input_tokens": 100, "output_tokens": 50})


BRIEF = {
    "logline": "A woman waits on an empty platform.",
    "target_duration_s": 30.0,
    "tone": ["quiet", "melancholic"],
    "genre": "drama",
    "artistic_style": "film noir, handheld",
}


def _valid_shot(order: int, **overrides: Any) -> dict[str, Any]:
    base = {
        "scene": 1,
        "order": order,
        "description": f"Shot {order} description.",
        "duration_s": 3.75,
        "has_humans": False,
        "is_hero": False,
        "motion_level": "low",
        "continuity_refs": [],
        "dialogue": [],
    }
    base.update(overrides)
    return base


def _valid_shots(n: int = 8, per: float = 3.75) -> list[dict[str, Any]]:
    return [_valid_shot(i + 1, duration_s=per) for i in range(n)]


# -- happy path ---------------------------------------------------------


def test_happy_path_returns_shots_and_summary() -> None:
    shots = _valid_shots(8, 3.75)  # 30s total
    client = StubClient(text=json.dumps(shots))
    tool = ScreenwriterTool(client=client)

    result = tool(None, {"brief": BRIEF})

    assert tool.name == "screenwriter"
    assert len(result["shots"]) == 8
    assert result["summary"]["shot_count"] == 8
    assert result["summary"]["total_duration_s"] == 30.0
    assert result["summary"]["within_duration_bound"] is True
    # The stub echoes the model arg (same as the real SDK).
    assert result["model"].startswith("claude-opus")
    assert "raw" not in result  # default: not included


def test_include_raw_attaches_model_text() -> None:
    shots = _valid_shots(8)
    client = StubClient(text=json.dumps(shots))
    tool = ScreenwriterTool(client=client)
    result = tool(None, {"brief": BRIEF, "include_raw": True})
    assert "raw" in result


def test_accepts_shots_wrapped_in_object() -> None:
    """Some models prefer {"shots": [...]} over a bare array — both OK."""
    shots = _valid_shots(8)
    client = StubClient(text=json.dumps({"shots": shots}))
    tool = ScreenwriterTool(client=client)
    result = tool(None, {"brief": BRIEF})
    assert len(result["shots"]) == 8


def test_hero_count_and_duration_delta() -> None:
    shots = _valid_shots(8)
    shots[0]["is_hero"] = True
    shots[3]["is_hero"] = True
    shots[6]["is_hero"] = True
    client = StubClient(text=json.dumps(shots))
    tool = ScreenwriterTool(client=client)
    result = tool(None, {"brief": BRIEF})
    assert result["summary"]["hero_count"] == 3
    assert abs(result["summary"]["duration_delta_pct"]) < 0.01


# -- outside duration bound is a warning, not a hard failure ------------


def test_duration_outside_bound_is_flagged_not_raised() -> None:
    # 8 shots * 2.0 = 16s, target=30s -> 47% under target
    shots = _valid_shots(8, per=2.0)
    client = StubClient(text=json.dumps(shots))
    tool = ScreenwriterTool(client=client)
    result = tool(None, {"brief": BRIEF})
    assert result["summary"]["within_duration_bound"] is False
    assert result["summary"]["duration_delta_pct"] < -5.0


# -- validation errors --------------------------------------------------


def test_rejects_non_film_level_call() -> None:
    tool = ScreenwriterTool(client=StubClient(text="[]"))
    with pytest.raises(ValueError, match="film-level"):
        tool("sh_001", {"brief": BRIEF})


def test_rejects_brief_missing_fields() -> None:
    tool = ScreenwriterTool(client=StubClient(text="[]"))
    with pytest.raises(ValueError, match="missing required fields"):
        tool(None, {"brief": {"logline": "only a logline"}})


def test_rejects_empty_shot_list() -> None:
    tool = ScreenwriterTool(client=StubClient(text="[]"))
    with pytest.raises(ValueError, match="zero shots"):
        tool(None, {"brief": BRIEF})


def test_rejects_bad_motion_level() -> None:
    shots = _valid_shots(8)
    shots[0]["motion_level"] = "frantic"
    tool = ScreenwriterTool(client=StubClient(text=json.dumps(shots)))
    with pytest.raises(ValueError, match="motion_level"):
        tool(None, {"brief": BRIEF})


def test_rejects_shot_duration_out_of_range() -> None:
    shots = _valid_shots(8)
    shots[0]["duration_s"] = 0.5  # below minimum 1.5
    tool = ScreenwriterTool(client=StubClient(text=json.dumps(shots)))
    with pytest.raises(ValueError, match="duration_s"):
        tool(None, {"brief": BRIEF})


def test_rejects_duplicate_orders() -> None:
    shots = _valid_shots(8)
    shots[1]["order"] = shots[0]["order"]
    tool = ScreenwriterTool(client=StubClient(text=json.dumps(shots)))
    with pytest.raises(ValueError, match="duplicated"):
        tool(None, {"brief": BRIEF})


def test_rejects_malformed_dialogue() -> None:
    shots = _valid_shots(8)
    shots[0]["dialogue"] = [{"line_id": "l1", "character": "woman"}]  # missing text
    tool = ScreenwriterTool(client=StubClient(text=json.dumps(shots)))
    with pytest.raises(ValueError, match="dialogue"):
        tool(None, {"brief": BRIEF})


def test_rejects_non_array_top_level() -> None:
    client = StubClient(text=json.dumps({"not_shots": 1}))
    tool = ScreenwriterTool(client=client)
    with pytest.raises(ValueError, match="expected JSON array"):
        tool(None, {"brief": BRIEF})


# -- dispatch integration -----------------------------------------------


def test_through_dispatch_writes_events(tmp_path) -> None:
    """Verify the adapter plays nicely with src.producer.dispatch."""
    from src.producer import dispatch, open_event_log
    from tests.producer.conftest import minimal_manifest

    shots = _valid_shots(8)
    tool = ScreenwriterTool(client=StubClient(text=json.dumps(shots)))
    manifest = minimal_manifest()

    with open_event_log(tmp_path / "events.db") as log:
        result = dispatch(
            agent="screenwriter",
            shot_id=None,
            manifest=manifest,
            ctx={"brief": BRIEF},
            tool=tool,
            events=log,
        )
        assert len(result.result["shots"]) == 8
        kinds = [e.kind for e in log.recent()]
        assert "dispatch_intent" in kinds
        assert "dispatch_result" in kinds
