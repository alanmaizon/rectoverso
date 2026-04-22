"""Unit tests for src.producer.prompt_smith — PromptSmithTool adapter.

Intent:        shot+routing -> provider prompt; contracts fire via dispatch
Architecture: LLMClient injected via StubClient; no network
Edge cases:    missing shot/routing, shot_id mismatch, judge_notes contract,
               bad JSON shape, reference paths
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from src.producer import PromptSmithTool
from src.producer.llm import LLMResponse


@dataclass
class StubClient:
    text: str
    model: str = "test-model"
    last_user: str = ""

    def create_message(
        self,
        *,
        model: str,
        system: Any,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        self.last_user = messages[-1]["content"]
        return LLMResponse(text=self.text, model=model, usage={"input_tokens": 80, "output_tokens": 20})


def _make_shot(shot_id: str = "sh_001", **overrides: Any) -> dict[str, Any]:
    base = {
        "shot_id": shot_id,
        "description": "Wide shot of an empty train platform at dawn.",
        "duration_s": 4.0,
        "has_humans": False,
        "is_hero": True,
        "motion_level": "low",
        "continuity_refs": [],
        "attempts": [],
        "creative_feedback": [],
    }
    base.update(overrides)
    return base


def _routing(provider: str = "vertex_veo_3_1_fast") -> dict[str, Any]:
    return {
        "chosen_provider": provider,
        "chosen_model": "veo-3.1-fast-generate-001",
        "supports_negative_prompt": False,
        "max_duration_s": 8,
    }


BRIEF = {
    "tone": ["quiet"],
    "genre": "drama",
    "artistic_style": "film noir, handheld",
}


def _valid_output(primary: str = "An empty platform at dawn, mist on rails.") -> str:
    return json.dumps(
        {"primary": primary, "negative": "", "reference_image_paths": []}
    )


# -- happy path ---------------------------------------------------------


def test_happy_path_returns_prompt_fields() -> None:
    client = StubClient(text=_valid_output())
    tool = PromptSmithTool(client=client)

    result = tool(
        "sh_001",
        {"shot": _make_shot(), "routing": _routing(), "brief": BRIEF},
    )

    assert tool.name == "prompt_smith"
    assert result["primary"].startswith("An empty platform")
    assert result["negative"] == ""
    assert result["reference_image_paths"] == []
    assert result["model"].startswith("claude-opus")


def test_user_payload_includes_shot_routing_brief() -> None:
    """The system prompt says PromptSmith reads shot/routing/brief. Verify
    we pass all three through the user message."""
    client = StubClient(text=_valid_output())
    tool = PromptSmithTool(client=client)
    tool(
        "sh_001",
        {"shot": _make_shot(), "routing": _routing(), "brief": BRIEF},
    )
    user = client.last_user
    assert "sh_001" in user
    assert "vertex_veo_3_1_fast" in user
    assert "film noir" in user  # artistic_style
    assert "FLAGS: revision=False creative_driven=False" in user


# -- revision path (judge_notes) ---------------------------------------


def test_revision_payload_includes_judge_notes_and_prior_prompt() -> None:
    shot = _make_shot(
        attempts=[
            {
                "attempt_id": 1,
                "provider": "vertex_veo_3_1_fast",
                "started_at": "2026-04-22T10:00:00Z",
                "outcome": "rejected",
                "rejection_reason": "auto_judge",
                "prompt_revision": "Earlier prompt text.",
                "judge_notes": "horizon tilt, face morph",
            }
        ]
    )
    client = StubClient(text=_valid_output("Revised prompt with level horizon."))
    tool = PromptSmithTool(client=client)
    tool(
        "sh_001",
        {
            "shot": shot,
            "routing": _routing(),
            "brief": BRIEF,
            "revision": True,
        },
    )
    user = client.last_user
    assert "PREVIOUS ATTEMPT" in user
    assert "horizon tilt" in user
    assert "Earlier prompt text." in user


# -- validation errors --------------------------------------------------


def test_rejects_missing_shot_id() -> None:
    tool = PromptSmithTool(client=StubClient(text=_valid_output()))
    with pytest.raises(ValueError, match="shot_id"):
        tool(None, {"shot": _make_shot(), "routing": _routing(), "brief": BRIEF})


def test_rejects_shot_id_mismatch() -> None:
    tool = PromptSmithTool(client=StubClient(text=_valid_output()))
    with pytest.raises(ValueError, match="mismatch"):
        tool(
            "sh_002",
            {"shot": _make_shot("sh_001"), "routing": _routing(), "brief": BRIEF},
        )


def test_rejects_missing_shot_fields() -> None:
    tool = PromptSmithTool(client=StubClient(text=_valid_output()))
    with pytest.raises(ValueError, match="shot missing"):
        tool(
            "sh_001",
            {"shot": {"shot_id": "sh_001"}, "routing": _routing(), "brief": BRIEF},
        )


def test_rejects_missing_routing_fields() -> None:
    tool = PromptSmithTool(client=StubClient(text=_valid_output()))
    with pytest.raises(ValueError, match="routing missing"):
        tool(
            "sh_001",
            {"shot": _make_shot(), "routing": {}, "brief": BRIEF},
        )


def test_rejects_output_missing_primary() -> None:
    client = StubClient(text=json.dumps({"negative": "nope"}))
    tool = PromptSmithTool(client=client)
    with pytest.raises(ValueError, match="primary"):
        tool(
            "sh_001",
            {"shot": _make_shot(), "routing": _routing(), "brief": BRIEF},
        )


def test_rejects_bad_reference_paths_type() -> None:
    client = StubClient(
        text=json.dumps(
            {"primary": "fine", "negative": "", "reference_image_paths": "nope"}
        )
    )
    tool = PromptSmithTool(client=client)
    with pytest.raises(ValueError, match="reference_image_paths"):
        tool(
            "sh_001",
            {"shot": _make_shot(), "routing": _routing(), "brief": BRIEF},
        )


def test_accepts_reference_paths() -> None:
    client = StubClient(
        text=json.dumps(
            {
                "primary": "fine",
                "negative": "",
                "reference_image_paths": ["inputs/refs/woman.png"],
            }
        )
    )
    tool = PromptSmithTool(client=client)
    result = tool(
        "sh_001",
        {"shot": _make_shot(), "routing": _routing(), "brief": BRIEF},
    )
    assert result["reference_image_paths"] == ["inputs/refs/woman.png"]


# -- dispatch integration: contract 2 blocks bad revisions --------------


def test_contract_blocks_revision_without_judge_notes(tmp_path) -> None:
    """dispatch() must refuse to call the adapter when revision=True but the
    most recent attempt has no judge_notes — Contract 2."""
    from src.contracts import ContractViolation
    from src.producer import dispatch, open_event_log
    from tests.producer.conftest import minimal_manifest

    manifest = minimal_manifest()
    # Seed a shot with a rejected attempt but NO judge_notes.
    manifest["shots"].append(
        {
            "shot_id": "sh_001",
            "scene": 1,
            "order": 1,
            "description": "x",
            "duration_s": 3.0,
            "has_humans": False,
            "is_hero": False,
            "motion_level": "low",
            "continuity_refs": [],
            "prompt": {"authored_by": "prompt_smith", "primary": "old"},
            "routing": {
                "chosen_provider": "alibaba_wan_2_7_plus",
                "chosen_model": "wan-2.7-plus",
                "rationale": "x",
                "decided_by": "router",
                "decided_at": "2026-04-22T00:00:00Z",
                "alternates": [],
            },
            "attempts": [
                {
                    "attempt_id": 1,
                    "provider": "alibaba_wan_2_7_plus",
                    "started_at": "2026-04-22T00:00:00Z",
                    "outcome": "rejected",
                    "rejection_reason": "auto_judge",
                    # judge_notes intentionally missing
                }
            ],
            "status": "rejected",
            "history": [],
            "judge_feedback": [],
            "creative_feedback": [],
        }
    )

    tool = PromptSmithTool(client=StubClient(text=_valid_output()))
    with open_event_log(tmp_path / "events.db") as log:
        with pytest.raises(ContractViolation):
            dispatch(
                agent="prompt_smith",
                shot_id="sh_001",
                manifest=manifest,
                ctx={"shot": manifest["shots"][0], "routing": _routing(), "brief": BRIEF, "revision": True},
                tool=tool,
                events=log,
            )
