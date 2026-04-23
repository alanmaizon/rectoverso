"""Thin Anthropic Messages API wrapper — the read-side of Tier-3 adapters.

Both Screenwriter and PromptSmith are single-turn Messages API calls. They
share the same shape: a system prompt (loaded from `prompts/*.md`, cached on
the Anthropic side via `cache_control`), a user payload, and a JSON response
parsed into a Python dict.

This module factors that shape into one call site so the two adapters stay
narrow and their tests stay hermetic.

Intent:
    - One function, `call_json(system, user, *, model, client)`, returns the
      parsed JSON object plus the raw text plus usage metadata.
    - System prompt is marked `cache_control: {"type": "ephemeral"}` — it's
      long and invariant per agent, so caching slashes cost across a run.
    - Client is injected. Tests pass FakeAnthropicClient; production passes
      the real SDK client via `default_client()`.

Architecture:
    - SDK import is lazy (inside `default_client()`) so importing this module
      never fails in test/CI contexts that don't have ANTHROPIC_API_KEY set.
    - JSON extraction is robust to ```json fences and leading/trailing prose.
      Anthropic models usually return clean JSON when the system prompt says
      "return strict JSON"; the extractor is a safety net, not a feature.

Edge cases:
    - Model returns non-JSON text -> LLMJSONDecodeError with the raw text in
      `.raw` so callers can log it.
    - Model returns a JSON array where a dict was expected (or vice versa) is
      left to the adapter to validate — this layer doesn't know the schema.
    - Empty/refusal responses raise LLMEmptyResponse.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"

DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_MAX_TOKENS = 4096


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMError(RuntimeError):
    """Base class for LLM-wrapper errors. Carries the raw text for debugging."""

    def __init__(self, msg: str, *, raw: str = "") -> None:
        super().__init__(msg)
        self.raw = raw


class LLMEmptyResponse(LLMError):
    """The model returned no content blocks, or all text blocks were empty."""


class LLMJSONDecodeError(LLMError):
    """Model text was not parseable JSON, even after fence stripping."""


# ---------------------------------------------------------------------------
# Client protocol + real + fake
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    """Minimal surface the wrapper uses. Both the real SDK client's
    `messages.create` and our FakeAnthropicClient satisfy this shape via a
    duck-typed `messages.create(...)` attribute."""

    def create_message(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> "LLMResponse": ...


@dataclass
class LLMResponse:
    """Normalized shape returned by both real and fake clients.

    The real SDK returns a richer object (Message); we project the two fields
    the wrapper needs — `text` (concatenated text blocks) and `usage` (dict).
    """

    text: str
    model: str
    usage: Mapping[str, Any]
    stop_reason: str | None = None


class RealAnthropicClient:
    """Adapter around the official anthropic SDK client. Lazy-imports the SDK
    so this module loads in environments without the package.

    Accepts an optional pre-built SDK client for dependency injection in
    integration tests that want to use the real SDK but patch transport.
    """

    def __init__(self, sdk_client: Any | None = None) -> None:
        if sdk_client is None:
            import anthropic  # type: ignore  # noqa: WPS433

            sdk_client = anthropic.Anthropic()
        self._sdk = sdk_client

    def create_message(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        resp = self._sdk.messages.create(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
        )
        text = _extract_text(resp.content)
        usage = getattr(resp, "usage", None)
        usage_dict = _usage_to_dict(usage)
        return LLMResponse(
            text=text,
            model=getattr(resp, "model", model),
            usage=usage_dict,
            stop_reason=getattr(resp, "stop_reason", None),
        )


def default_client() -> RealAnthropicClient:
    """Construct a real Anthropic client using the ambient env.

    Conveniences for hackathon workflows: if `ANTHROPIC_API_KEY` is missing
    from the shell env, load it from `<repo-root>/.env` (gitignored). This
    mirrors how `scratch/real_dispatch_probe.py` and
    `scratch/managed_agents_hyperframes_probe.py` bootstrap the key.
    """
    _ensure_anthropic_api_key()
    return RealAnthropicClient()


def _ensure_anthropic_api_key() -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    # Walk up from this file to find a .env (repo root sits two levels above
    # src/producer/llm.py).
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        env_path = parent / ".env"
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == "ANTHROPIC_API_KEY":
                    v = v.strip().strip('"').strip("'")
                    if v and not v.startswith("<") and v != "your-key-here":
                        os.environ["ANTHROPIC_API_KEY"] = v
                        return
            return  # found a .env but no key — don't keep walking


# ---------------------------------------------------------------------------
# System prompt loading
# ---------------------------------------------------------------------------

_PROMPT_CACHE: dict[str, str] = {}


def load_system_prompt(agent_name: str) -> str:
    """Read `prompts/<agent_name>.md` and cache it for the process lifetime.

    Cached because the Anthropic prompt cache is a network feature; the local
    file-read cache just avoids re-hitting disk on every call.
    """
    if agent_name in _PROMPT_CACHE:
        return _PROMPT_CACHE[agent_name]
    path = PROMPTS_DIR / f"{agent_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"system prompt for agent {agent_name!r} not found at {path}")
    text = path.read_text(encoding="utf-8")
    _PROMPT_CACHE[agent_name] = text
    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def call_json(
    *,
    system: str,
    user: str,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[Any, LLMResponse]:
    """Call the model with a cached system prompt and a user message; parse JSON.

    Returns (parsed_json, raw_response). The parser accepts:
        - bare JSON (array or object),
        - JSON wrapped in ```json ... ``` fences,
        - JSON wrapped in ``` ... ``` fences,
        - JSON with leading/trailing prose the model insisted on adding.

    Raises:
        LLMEmptyResponse: the model returned no usable text.
        LLMJSONDecodeError: the text was not parseable JSON.
    """
    eff_client = client or default_client()
    eff_model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    # system with explicit prompt caching on the invariant block
    system_blocks = [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    resp = eff_client.create_message(
        model=eff_model,
        system=system_blocks,
        messages=[{"role": "user", "content": user}],
        max_tokens=max_tokens,
    )

    text = (resp.text or "").strip()
    if not text:
        raise LLMEmptyResponse(
            f"model {resp.model} returned no text (stop_reason={resp.stop_reason})",
            raw="",
        )

    try:
        parsed = _extract_json(text)
    except ValueError as exc:
        raise LLMJSONDecodeError(str(exc), raw=text) from exc

    return parsed, resp


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> Any:
    """Robust JSON extraction: bare, fenced, or with trailing prose."""
    candidates: list[str] = []
    # 1. Try fenced first (most common when the model adds explanation).
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    # 2. Fall back to the full text.
    candidates.append(text.strip())
    # 3. Last resort: substring between first '{' or '[' and matching end.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            candidates.append(text[start : end + 1])

    last_err: Exception | None = None
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError as exc:
            last_err = exc
            continue
    raise ValueError(f"no JSON candidate parsed; last error: {last_err}")


def _extract_text(content: Any) -> str:
    """Concatenate text blocks from an Anthropic Message content list."""
    if content is None:
        return ""
    # SDK returns a list of ContentBlock objects with .type and .text attrs.
    parts: list[str] = []
    for block in content:
        btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, Mapping) else None)
        if btype == "text":
            text = getattr(block, "text", None)
            if text is None and isinstance(block, Mapping):
                text = block.get("text")
            if text:
                parts.append(str(text))
    return "".join(parts)


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    # SDK Usage object has input_tokens, output_tokens, cache_creation_input_tokens,
    # cache_read_input_tokens — read them defensively.
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    out: dict[str, Any] = {}
    for k in keys:
        v = getattr(usage, k, None)
        if v is None and isinstance(usage, Mapping):
            v = usage.get(k)
        if v is not None:
            out[k] = v
    return out
