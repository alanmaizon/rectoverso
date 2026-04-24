"""Unit tests for src.producer.llm — Anthropic wrapper + JSON extraction.

Intent:       call_json handles clean JSON, fenced JSON, JSON with prose
Architecture: LLMClient is Protocol-typed; we inject a minimal stub
Edge cases:   empty response, malformed JSON, object-vs-array mismatch
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.producer.llm import (
    TIER3_MAX_TOKENS,
    LLMEmptyResponse,
    LLMJSONDecodeError,
    LLMResponse,
    call_json,
    load_system_prompt,
)


@dataclass
class StubClient:
    """Minimal LLMClient stub. Records the one call it receives."""

    text: str = ""
    model: str = "test-model"
    calls: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def create_message(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        self.calls.append(
            {"model": model, "system": system, "messages": messages, "max_tokens": max_tokens}
        )
        return LLMResponse(text=self.text, model=model, usage={"input_tokens": 12, "output_tokens": 3})


# -- JSON extraction -----------------------------------------------------


def test_call_json_parses_bare_object() -> None:
    client = StubClient(text='{"ok": true, "n": 1}')
    parsed, resp = call_json(system="sys", user="u", client=client)
    assert parsed == {"ok": True, "n": 1}
    assert resp.usage == {"input_tokens": 12, "output_tokens": 3}


def test_call_json_parses_bare_array() -> None:
    client = StubClient(text="[1, 2, 3]")
    parsed, _ = call_json(system="sys", user="u", client=client)
    assert parsed == [1, 2, 3]


def test_call_json_strips_json_fences() -> None:
    client = StubClient(text='```json\n{"ok": true}\n```')
    parsed, _ = call_json(system="sys", user="u", client=client)
    assert parsed == {"ok": True}


def test_call_json_strips_generic_fences() -> None:
    client = StubClient(text='```\n[1, 2]\n```')
    parsed, _ = call_json(system="sys", user="u", client=client)
    assert parsed == [1, 2]


def test_call_json_extracts_from_prose() -> None:
    client = StubClient(
        text=(
            "Sure, here is the JSON you requested:\n"
            '{"hello": "world"}\n'
            "Let me know if you need anything else."
        )
    )
    parsed, _ = call_json(system="sys", user="u", client=client)
    assert parsed == {"hello": "world"}


# -- cache control + messages wiring ------------------------------------


def test_system_prompt_is_marked_for_caching() -> None:
    client = StubClient(text="{}")
    call_json(system="THE SYSTEM", user="THE USER", client=client)
    call = client.calls[-1]
    assert isinstance(call["system"], list)
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["system"][0]["text"] == "THE SYSTEM"
    assert call["messages"] == [{"role": "user", "content": "THE USER"}]


def test_default_model_and_max_tokens() -> None:
    client = StubClient(text="{}")
    call_json(system="sys", user="u", client=client)
    call = client.calls[-1]
    assert call["max_tokens"] == TIER3_MAX_TOKENS
    assert call["model"].startswith("claude-haiku")


def test_explicit_model_override() -> None:
    client = StubClient(text="{}")
    call_json(system="sys", user="u", client=client, model="sonnet-4-6")
    assert client.calls[-1]["model"] == "sonnet-4-6"


# -- errors -------------------------------------------------------------


def test_empty_text_raises() -> None:
    client = StubClient(text="")
    with pytest.raises(LLMEmptyResponse):
        call_json(system="sys", user="u", client=client)


def test_malformed_json_raises() -> None:
    client = StubClient(text="not json at all {{{")
    with pytest.raises(LLMJSONDecodeError) as excinfo:
        call_json(system="sys", user="u", client=client)
    assert excinfo.value.raw  # the raw text is preserved for debugging


# -- system prompt loading ----------------------------------------------


def test_load_system_prompt_reads_file() -> None:
    text = load_system_prompt("screenwriter")
    assert "Screenwriter" in text
    # Cached on second call — same object returned.
    assert load_system_prompt("screenwriter") is text


def test_load_system_prompt_missing_agent_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_system_prompt("no_such_agent_xyz")
