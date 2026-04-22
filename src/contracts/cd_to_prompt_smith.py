"""Contract 3 — Creative Director -> PromptSmith.

See docs/contracts.md § Contract 3.

Fires when `ctx['creative_driven'] == True` on a PromptSmith or Renderer
dispatch. The contract enforces the *translation step* between CD's natural-
language suggestion and PromptSmith's binding input (`shots[i].artistic_direction`).

Without this check, CD's guidance never lands in the re-rendered shot: PromptSmith
reads the unchanged artistic_direction, produces a near-identical prompt, and the
new render fails the same way CD already flagged.

Invariant:
    For each unaddressed creative_feedback entry from creative_director on shot i,
    there must be a history entry `event == "artistic_direction_updated"` with
    `ts >= <that entry's ts>`, AND `shots[i].artistic_direction` must be non-empty.

    ISO-8601 UTC strings sort lexicographically iff they share the format the
    schema enforces (matching `iso8601Utc` regex — Z-suffixed, fixed width). String
    comparison is correct here; no datetime parsing needed.
"""

from __future__ import annotations

from typing import Any, Mapping

from .registry import register
from .types import ContractName, Severity, Violation


def _find_shot(manifest: Mapping[str, Any], shot_id: str) -> Mapping[str, Any] | None:
    for s in manifest.get("shots", []):
        if s.get("shot_id") == shot_id:
            return s
    return None


def _unaddressed_cd_feedback(shot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        f
        for f in shot.get("creative_feedback", [])
        if f.get("from_agent") == "creative_director" and not f.get("addressed", False)
    ]


def check(
    manifest: Mapping[str, Any],
    shot_id: str | None,
    ctx: Mapping[str, Any],
) -> list[Violation]:
    if shot_id is None:
        return [
            Violation(
                contract=ContractName.CD_TO_PROMPT_SMITH,
                severity=Severity.BLOCK,
                reason="creative-driven dispatch requires a shot_id",
            )
        ]

    shot = _find_shot(manifest, shot_id)
    if shot is None:
        return [
            Violation(
                contract=ContractName.CD_TO_PROMPT_SMITH,
                severity=Severity.BLOCK,
                reason=f"shot_id {shot_id!r} not found in manifest",
                shot_id=shot_id,
            )
        ]

    unaddressed = _unaddressed_cd_feedback(shot)
    if not unaddressed:
        # Caller labeled dispatch creative_driven but there's no CD feedback to
        # act on. Warn — either ctx was mis-set or the feedback was already marked
        # addressed. Not blocking (technical revision can still proceed).
        return [
            Violation(
                contract=ContractName.CD_TO_PROMPT_SMITH,
                severity=Severity.WARN,
                reason=(
                    f"dispatch marked creative_driven but shot {shot_id} has no "
                    "unaddressed creative_director feedback"
                ),
                shot_id=shot_id,
            )
        ]

    latest_cd_ts = max(f["ts"] for f in unaddressed)

    update_events = [
        h
        for h in shot.get("history", [])
        if h.get("event") == "artistic_direction_updated"
        and h.get("ts", "") >= latest_cd_ts
    ]
    if not update_events:
        return [
            Violation(
                contract=ContractName.CD_TO_PROMPT_SMITH,
                severity=Severity.BLOCK,
                reason=(
                    f"creative-driven re-render of {shot_id} requested but no "
                    f"'artistic_direction_updated' history entry found at or after "
                    f"CD feedback at {latest_cd_ts}"
                ),
                shot_id=shot_id,
                detail={"latest_cd_ts": latest_cd_ts},
            )
        ]

    direction = (shot.get("artistic_direction") or "").strip()
    if not direction:
        return [
            Violation(
                contract=ContractName.CD_TO_PROMPT_SMITH,
                severity=Severity.BLOCK,
                reason=(
                    f"creative-driven re-render of {shot_id} requested but "
                    "artistic_direction is empty"
                ),
                shot_id=shot_id,
            )
        ]

    return []


register(ContractName.CD_TO_PROMPT_SMITH, check)
