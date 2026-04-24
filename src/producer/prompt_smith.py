"""PromptSmithTool — Tier-3 Messages-API adapter, shot -> provider prompt.

PromptSmith translates one shot into a provider-specific prompt (primary text,
optional negative, optional reference image paths). See prompts/prompt_smith.md
for the authoritative system prompt.

Contracts (enforced upstream by validate_before_dispatch, NOT here):
    - Contract 2 (shot_judge -> prompt_smith): when ctx.revision=True,
      the previous attempt's judge_notes must be present + non-empty.
    - Contract 3 (cd -> prompt_smith): when ctx.creative_driven=True,
      shots[i].artistic_direction must be non-empty and updated since the
      triggering creative_director feedback.

This adapter trusts dispatch() — the Producer runs the contracts first and
refuses to call us on bad inputs. We focus on translating inputs into a prompt.

Intent:
    - Build the user payload from the manifest slice for `shot_id`.
    - Hand off to the LLM with the cached prompts/prompt_smith.md as system.
    - Parse the JSON object the model returns; validate shape.
    - Return a dict the Producer can project into shots[].prompt directly.

Architecture:
    - Tool-Protocol compliant (name="prompt_smith").
    - Payload dict keys required:
        "shot": full shot object from the manifest
        "routing": the routing block (chosen_provider, chosen_model, ...)
        "brief": film-level anchors (tone/genre/artistic_style)
      Payload dict keys optional (all default False):
        "revision": bool          -- matches dispatch ctx.revision
        "creative_driven": bool   -- matches dispatch ctx.creative_driven
        "include_raw": bool       -- attach raw model text to result
    - Result dict (shape-stable for manifest projection):
        "primary": str
        "negative": str
        "reference_image_paths": list[str]
        "model": str
        "usage": {...}
        "raw": str   (optional)

Edge cases:
    - Veo router bug sentinel: if the model returns `primary` starting with
      "ERROR:", surface that verbatim — the Producer is expected to reroute
      rather than ship a broken prompt.
    - Unknown provider grammar: pass through; the system prompt covers Veo,
      Kling, and Wan — other providers are future work and their prompts
      still get written, just without provider-specific templating.
"""

from __future__ import annotations

from typing import Any, Mapping

from .llm import (
    TIER3_MAX_TOKENS,
    LLMClient,
    call_json,
    load_system_prompt,
)


# Fields the caller MUST populate on the shot dict. List-valued fields that
# tolerate empty (continuity_refs — most shots have no prior-shot dep) are
# handled by the reader's .get(..., []) patterns below, not required here.
# See screenwriter.py OPTIONAL_LIST_SHOT_FIELDS for the sibling convention.
REQUIRED_SHOT_FIELDS = (
    "shot_id",
    "description",
    "duration_s",
    "has_humans",
    "is_hero",
    "motion_level",
)
REQUIRED_ROUTING_FIELDS = ("chosen_provider", "chosen_model")


class PromptSmithTool:
    """Tool-Protocol adapter. `name == "prompt_smith"`."""

    name = "prompt_smith"

    def __init__(
        self,
        client: LLMClient | None = None,
        model: str | None = None,
        max_tokens: int = TIER3_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if shot_id is None:
            raise ValueError("prompt_smith requires a shot_id; got None")

        shot = payload.get("shot") or {}
        routing = payload.get("routing") or {}
        brief = payload.get("brief") or {}
        revision = bool(payload.get("revision", False))
        creative_driven = bool(payload.get("creative_driven", False))
        include_raw = bool(payload.get("include_raw", False))

        _require_fields("shot", shot, REQUIRED_SHOT_FIELDS)
        _require_fields("routing", routing, REQUIRED_ROUTING_FIELDS)
        if shot["shot_id"] != shot_id:
            raise ValueError(
                f"payload shot_id mismatch: dispatch={shot_id!r} "
                f"payload.shot.shot_id={shot['shot_id']!r}"
            )

        system = load_system_prompt("prompt_smith")
        user = _build_user_payload(
            shot=shot,
            routing=routing,
            brief=brief,
            revision=revision,
            creative_driven=creative_driven,
        )

        parsed, resp = call_json(
            system=system,
            user=user,
            client=self._client,
            model=self._model,
            max_tokens=self._max_tokens,
        )

        prompt = _coerce_prompt(parsed)
        result: dict[str, Any] = {
            "primary": prompt["primary"],
            "negative": prompt["negative"],
            "reference_image_paths": prompt["reference_image_paths"],
            "model": resp.model,
            "usage": dict(resp.usage),
        }
        if include_raw:
            result["raw"] = resp.text
        return result


# ---------------------------------------------------------------------------
# User payload
# ---------------------------------------------------------------------------


def _build_user_payload(
    *,
    shot: Mapping[str, Any],
    routing: Mapping[str, Any],
    brief: Mapping[str, Any],
    revision: bool,
    creative_driven: bool,
) -> str:
    """Compact user message — the system prompt does the heavy lifting."""
    lines: list[str] = []
    lines.append(
        "Write the provider prompt for this shot. "
        "Return strict JSON per your system prompt."
    )
    lines.append("")
    lines.append(f"FLAGS: revision={revision} creative_driven={creative_driven}")
    lines.append("")
    lines.append("BRIEF:")
    lines.append(f"- tone: {', '.join(brief.get('tone', []))}")
    lines.append(f"- genre: {brief.get('genre', '')}")
    if brief.get("artistic_style"):
        lines.append(f"- artistic_style: {brief['artistic_style']}")
    lines.append("")
    lines.append("ROUTING:")
    lines.append(f"- chosen_provider: {routing['chosen_provider']}")
    lines.append(f"- chosen_model:    {routing['chosen_model']}")
    for hint in (
        "supports_negative_prompt",
        "supports_reference_images",
        "supports_first_last_frame",
        "max_reference_images",
        "max_duration_s",
    ):
        if hint in routing:
            lines.append(f"- {hint}: {routing[hint]}")
    lines.append("")
    lines.append("SHOT:")
    lines.append(f"- shot_id: {shot['shot_id']}")
    lines.append(f"- description: {shot['description']}")
    lines.append(f"- duration_s: {shot['duration_s']}")
    lines.append(f"- has_humans: {shot['has_humans']}")
    lines.append(f"- is_hero: {shot['is_hero']}")
    lines.append(f"- motion_level: {shot['motion_level']}")
    refs = shot.get("continuity_refs") or []
    lines.append(f"- continuity_refs: {refs}")
    if shot.get("artistic_direction"):
        lines.append(f"- artistic_direction: {shot['artistic_direction']}")

    if revision:
        last = _last_attempt(shot)
        if last is not None:
            notes = last.get("judge_notes") or ""
            prev_prompt = last.get("prompt_revision") or ""
            lines.append("")
            lines.append("PREVIOUS ATTEMPT (revise, don't paraphrase):")
            if prev_prompt:
                lines.append(f"- prior primary: {prev_prompt}")
            if notes:
                lines.append(f"- judge_notes: {notes}")

    if creative_driven and shot.get("creative_feedback"):
        lines.append("")
        lines.append("CREATIVE FEEDBACK (informational — Producer has translated):")
        for f in shot["creative_feedback"][-3:]:
            lines.append(
                f"- [{f.get('priority', '?')}] {f.get('feedback', '')} "
                f"(suggestion: {f.get('suggestion', '')})"
            )

    return "\n".join(lines)


def _last_attempt(shot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    attempts = shot.get("attempts") or []
    if not attempts:
        return None
    return attempts[-1]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_fields(name: str, obj: Mapping[str, Any], fields: tuple[str, ...]) -> None:
    missing = [f for f in fields if f not in obj]
    if missing:
        raise ValueError(f"{name} missing required fields: {missing}")


def _coerce_prompt(parsed: Any) -> dict[str, Any]:
    if not isinstance(parsed, Mapping):
        raise ValueError(
            f"expected JSON object with primary/negative/reference_image_paths, "
            f"got {type(parsed).__name__}"
        )
    if "primary" not in parsed or not str(parsed["primary"]).strip():
        raise ValueError("prompt_smith output missing non-empty 'primary' field")
    primary = str(parsed["primary"]).strip()
    negative = str(parsed.get("negative") or "").strip()
    refs_raw = parsed.get("reference_image_paths") or []
    if not isinstance(refs_raw, list):
        raise ValueError("reference_image_paths must be a list")
    refs: list[str] = []
    for r in refs_raw:
        if not isinstance(r, str) or not r.strip():
            raise ValueError(f"reference_image_paths contains non-string entry: {r!r}")
        refs.append(r.strip())
    return {
        "primary": primary,
        "negative": negative,
        "reference_image_paths": refs,
    }
