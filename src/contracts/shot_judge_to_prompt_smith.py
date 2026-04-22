"""Contract 2 — Shot Judge -> PromptSmith.

See docs/contracts.md § Contract 2.

Only fires when `ctx['revision'] == True` (registry already guards this). A
revision request is the Producer asking PromptSmith to rewrite the prompt for
a shot that just had an attempt rejected. PromptSmith reads the judge's notes
to know WHAT to rewrite. If notes are empty, PromptSmith paraphrases the
original prompt and the next attempt fails the same way.

Requirements:
    * last attempt's outcome must be "rejected"
    * rejection_reason must be one where notes are expected (auto_judge /
      continuity / artifact). "user" and "timeout" rejections are excluded —
      the user supplies the rationale out-of-band; timeouts are provider-side
      and PromptSmith revision is not the right intervention anyway.
    * judge_notes must be non-empty and not whitespace-only
"""

from __future__ import annotations

from typing import Any, Mapping

from .registry import register
from .types import ContractName, Severity, Violation


# Which rejection_reasons require judge_notes for a PromptSmith revision.
# "user" and "timeout" are deliberately excluded (see module docstring).
REJECTION_REASONS_REQUIRING_NOTES = frozenset({"auto_judge", "continuity", "artifact"})


def _find_shot(manifest: Mapping[str, Any], shot_id: str) -> Mapping[str, Any] | None:
    for s in manifest.get("shots", []):
        if s.get("shot_id") == shot_id:
            return s
    return None


def check(
    manifest: Mapping[str, Any],
    shot_id: str | None,
    ctx: Mapping[str, Any],
) -> list[Violation]:
    if shot_id is None:
        # Revision dispatch must name a shot. If we're called without one,
        # flag it loudly rather than silently passing.
        return [
            Violation(
                contract=ContractName.SHOT_JUDGE_TO_PROMPT_SMITH,
                severity=Severity.BLOCK,
                reason="PromptSmith revision dispatched without shot_id",
            )
        ]

    shot = _find_shot(manifest, shot_id)
    if shot is None:
        return [
            Violation(
                contract=ContractName.SHOT_JUDGE_TO_PROMPT_SMITH,
                severity=Severity.BLOCK,
                reason=f"shot_id {shot_id!r} not found in manifest",
                shot_id=shot_id,
            )
        ]

    attempts = shot.get("attempts", [])
    if not attempts:
        return [
            Violation(
                contract=ContractName.SHOT_JUDGE_TO_PROMPT_SMITH,
                severity=Severity.BLOCK,
                reason=(
                    f"revision requested on {shot_id} but shot has no attempts yet "
                    "— call PromptSmith without revision=True for the initial prompt"
                ),
                shot_id=shot_id,
            )
        ]

    last = attempts[-1]
    outcome = last.get("outcome")

    if outcome != "rejected":
        return [
            Violation(
                contract=ContractName.SHOT_JUDGE_TO_PROMPT_SMITH,
                severity=Severity.BLOCK,
                reason=(
                    f"revision requested on {shot_id} but last attempt outcome is "
                    f"{outcome!r} (expected 'rejected')"
                ),
                shot_id=shot_id,
                detail={"attempt_id": last.get("attempt_id"), "outcome": outcome},
            )
        ]

    reason = last.get("rejection_reason")
    if reason not in REJECTION_REASONS_REQUIRING_NOTES:
        # A rejection with reason 'user' or 'timeout' doesn't gate on judge_notes.
        # The revision can proceed — but we emit a warn so it lands in history.
        return [
            Violation(
                contract=ContractName.SHOT_JUDGE_TO_PROMPT_SMITH,
                severity=Severity.WARN,
                reason=(
                    f"revision on {shot_id}: rejection_reason={reason!r}; "
                    "judge_notes check skipped (reason is out-of-scope for this contract)"
                ),
                shot_id=shot_id,
                detail={"rejection_reason": reason},
            )
        ]

    notes = (last.get("judge_notes") or "").strip()
    if not notes:
        return [
            Violation(
                contract=ContractName.SHOT_JUDGE_TO_PROMPT_SMITH,
                severity=Severity.BLOCK,
                reason=(
                    f"revision on {shot_id}: attempts[-1].judge_notes is empty — "
                    "PromptSmith has no signal to rewrite on"
                ),
                shot_id=shot_id,
                detail={
                    "attempt_id": last.get("attempt_id"),
                    "rejection_reason": reason,
                },
            )
        ]

    return []


register(ContractName.SHOT_JUDGE_TO_PROMPT_SMITH, check)
