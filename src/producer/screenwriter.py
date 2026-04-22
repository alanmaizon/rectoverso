"""ScreenwriterTool — Tier-3 Messages-API adapter, brief -> shots[].

The Screenwriter is a single-turn LLM call (see prompts/screenwriter.md). It
has no agent-pair contracts (registry.contracts_for_dispatch("screenwriter", ..)
returns []), so this adapter doesn't participate in the validate_before_dispatch
layer at all. Its job is narrow:

    Input  : brief fields + router capability notes (user payload).
    Output : list of shot dicts in the screenwriter's schema (the subset of
             fields Producer later projects into manifest.shots[]).

Intent:
    - Adapter is pure w.r.t. the manifest — it does not touch disk, does not
      assign shot_ids, does not decide status transitions. The caller projects.
    - Shape validation is strict at the field level (required keys, types,
      enum values, duration bounds). Business validation (duration sums to
      target) is a warning, not a hard failure — the caller decides what to do.

Architecture:
    - Tool-Protocol compliant (name="screenwriter"). Accepts an injected
      LLMClient; defaults to the real Anthropic SDK via llm.default_client().
    - Payload dict keys:
        "brief": {...}                           required
        "capability_notes": "multi-line string"  optional; defaults to a stock
                                                 summary derived from
                                                 CLAUDE.md § Provider priority
        "include_raw": bool                      attach raw model text to result
    - Result dict keys:
        "shots": list[dict]       the screenwriter's output (pre-projection)
        "summary": {              book-keeping for the Producer/history log
            "shot_count": int,
            "total_duration_s": float,
            "target_duration_s": float,
            "duration_delta_pct": float,        # signed
            "hero_count": int,
            "within_duration_bound": bool,       # ±5% per screenwriter.md
        }
        "model": str
        "usage": {...}
        "raw": str                optional, only if include_raw=True

Edge cases:
    - LLM returns a dict instead of a list -> ValueError("expected array ...")
    - Missing required field on a shot -> ValueError pointing to the offender
    - Duration out of ±5% -> summary.within_duration_bound=False; NOT raised.
      The Producer is responsible for deciding retry/accept; mechanically it's
      often close enough to ship.
    - Dialogue present but malformed (missing line_id/character/text) -> ValueError.

The adapter is deliberately forgiving on fields it doesn't know about
(additionalProperties are kept but unchecked) — the Producer's projection is
the narrow schema gate. This layer catches *shape* bugs, not authorship style.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .llm import (
    DEFAULT_MAX_TOKENS,
    LLMClient,
    call_json,
    load_system_prompt,
)


# Per screenwriter.md § Duration rules
DURATION_TOLERANCE_PCT = 5.0
MIN_SHOT_DURATION_S = 1.5
MAX_SHOT_DURATION_S = 8.0
MIN_SHOT_COUNT = 8
MAX_SHOT_COUNT = 15

REQUIRED_SHOT_FIELDS = (
    "scene",
    "order",
    "description",
    "duration_s",
    "has_humans",
    "is_hero",
    "motion_level",
    "continuity_refs",
    "dialogue",
)
MOTION_LEVELS = frozenset(("low", "medium", "high"))

REQUIRED_BRIEF_FIELDS = ("logline", "target_duration_s", "tone", "genre")


# Default capability notes derived from CLAUDE.md § Provider priority. Kept
# short — the screenwriter should hear "what the router will allow", not the
# full provider matrix. Callers can override via payload["capability_notes"].
DEFAULT_CAPABILITY_NOTES = """\
Router capability notes (what downstream can render):
- Workhorses handle most shots: Alibaba Wan 2.7 (non-human), Kling 2.x (all humans).
- Specialty (hero shots only): Vertex Veo 3.1 Fast. NEVER for shots with humans (EU rule).
- Hero flag unlocks specialty tier; budget for 3–5 heroes per film.
- Hero + humans routes to Kling ("hero-for-Kling"); still mark is_hero=true.
- Motion bias: low/medium render reliably; high is retry-prone — use sparingly.
- Max per-shot duration: 8s. Min: 1.5s. Sub-1.5s clips look broken.
"""


@dataclass(frozen=True)
class ScreenwriterSummary:
    """Typed mirror of the result["summary"] block — for docs/tests."""

    shot_count: int
    total_duration_s: float
    target_duration_s: float
    duration_delta_pct: float
    hero_count: int
    within_duration_bound: bool


class ScreenwriterTool:
    """Tool-Protocol adapter. `name == "screenwriter"`.

    Constructed with an optional LLMClient. On dispatch it:
        1. Loads the cached system prompt (prompts/screenwriter.md).
        2. Builds a compact user payload from brief + capability_notes.
        3. Calls the model expecting a JSON array of shots.
        4. Validates field shape on each shot.
        5. Returns the list, a summary block, and usage metadata.
    """

    name = "screenwriter"

    def __init__(
        self,
        client: LLMClient | None = None,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if shot_id is not None:
            raise ValueError(
                f"screenwriter is film-level; got shot_id={shot_id!r}"
            )

        brief = payload.get("brief") or {}
        _require_brief_fields(brief)

        capability_notes = str(
            payload.get("capability_notes") or DEFAULT_CAPABILITY_NOTES
        )
        include_raw = bool(payload.get("include_raw", False))

        system = load_system_prompt("screenwriter")
        user = _build_user_payload(brief, capability_notes)

        parsed, resp = call_json(
            system=system,
            user=user,
            client=self._client,
            model=self._model,
            max_tokens=self._max_tokens,
        )

        shots = _coerce_shot_list(parsed)
        _validate_shots(shots)

        summary = _summarize(shots, float(brief["target_duration_s"]))

        result: dict[str, Any] = {
            "shots": shots,
            "summary": summary.__dict__,
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
    brief: Mapping[str, Any], capability_notes: str
) -> str:
    """Compact, LLM-friendly user message. Strict fields only, no prose."""
    lines: list[str] = []
    lines.append("Produce a shot list for this brief.")
    lines.append("")
    lines.append("BRIEF:")
    lines.append(f"- logline: {brief['logline']}")
    lines.append(f"- target_duration_s: {brief['target_duration_s']}")
    lines.append(f"- tone: {', '.join(brief['tone'])}")
    lines.append(f"- genre: {brief['genre']}")
    if brief.get("artistic_style"):
        lines.append(f"- artistic_style: {brief['artistic_style']}")
    lines.append("")
    lines.append(capability_notes.strip())
    lines.append("")
    lines.append(
        "Return strict JSON: a single array of shot objects, no prose, "
        "no markdown fences. Each shot follows the schema in your system prompt."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _require_brief_fields(brief: Mapping[str, Any]) -> None:
    missing = [f for f in REQUIRED_BRIEF_FIELDS if f not in brief]
    if missing:
        raise ValueError(
            f"brief is missing required fields: {missing} "
            f"(see screenwriter.md § Inputs)"
        )
    if not isinstance(brief["tone"], (list, tuple)) or not brief["tone"]:
        raise ValueError("brief.tone must be a non-empty list of strings")
    if float(brief["target_duration_s"]) <= 0:
        raise ValueError("brief.target_duration_s must be > 0")


def _coerce_shot_list(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        return [dict(s) for s in parsed]
    if isinstance(parsed, Mapping) and "shots" in parsed:
        return [dict(s) for s in parsed["shots"]]
    raise ValueError(
        f"expected JSON array of shots, got {type(parsed).__name__}"
    )


def _validate_shots(shots: list[dict[str, Any]]) -> None:
    if not shots:
        raise ValueError("screenwriter returned zero shots")
    seen_orders: set[int] = set()
    for i, s in enumerate(shots):
        missing = [f for f in REQUIRED_SHOT_FIELDS if f not in s]
        if missing:
            raise ValueError(
                f"shot at index {i} missing required fields: {missing}"
            )
        if s["motion_level"] not in MOTION_LEVELS:
            raise ValueError(
                f"shot[{i}].motion_level={s['motion_level']!r} not in {sorted(MOTION_LEVELS)}"
            )
        if not isinstance(s["description"], str) or not s["description"].strip():
            raise ValueError(f"shot[{i}].description must be a non-empty string")
        dur = float(s["duration_s"])
        if dur < MIN_SHOT_DURATION_S or dur > MAX_SHOT_DURATION_S:
            raise ValueError(
                f"shot[{i}].duration_s={dur} outside [{MIN_SHOT_DURATION_S}, "
                f"{MAX_SHOT_DURATION_S}] (screenwriter.md § Duration rules)"
            )
        if not isinstance(s["has_humans"], bool):
            raise ValueError(f"shot[{i}].has_humans must be a boolean")
        if not isinstance(s["is_hero"], bool):
            raise ValueError(f"shot[{i}].is_hero must be a boolean")
        order = int(s["order"])
        if order in seen_orders:
            raise ValueError(f"shot[{i}].order={order} duplicated")
        seen_orders.add(order)
        refs = s["continuity_refs"]
        if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
            raise ValueError(f"shot[{i}].continuity_refs must be a list of strings")
        _validate_dialogue(i, s["dialogue"])


def _validate_dialogue(shot_index: int, dialogue: Any) -> None:
    if not isinstance(dialogue, list):
        raise ValueError(f"shot[{shot_index}].dialogue must be a list")
    for j, line in enumerate(dialogue):
        if not isinstance(line, Mapping):
            raise ValueError(
                f"shot[{shot_index}].dialogue[{j}] must be an object"
            )
        for key in ("line_id", "character", "text"):
            if key not in line or not str(line[key]).strip():
                raise ValueError(
                    f"shot[{shot_index}].dialogue[{j}] missing/empty {key!r}"
                )


def _summarize(
    shots: Iterable[Mapping[str, Any]], target_s: float
) -> ScreenwriterSummary:
    shots_list = list(shots)
    total = float(sum(float(s["duration_s"]) for s in shots_list))
    heroes = sum(1 for s in shots_list if s.get("is_hero"))
    delta_pct = ((total - target_s) / target_s) * 100.0 if target_s > 0 else 0.0
    within = abs(delta_pct) <= DURATION_TOLERANCE_PCT
    return ScreenwriterSummary(
        shot_count=len(shots_list),
        total_duration_s=round(total, 3),
        target_duration_s=round(target_s, 3),
        duration_delta_pct=round(delta_pct, 3),
        hero_count=heroes,
        within_duration_bound=within,
    )
