"""Contract 4 — CD reads only approved-attempt judge feedback.

See docs/contracts.md § Contract 4.

Warn-severity only: stale judge_feedback entries are filtered out of CD's
context via `filter_judge_feedback_for_cd(shot)`. The contract check reports
how many entries were filtered so the Producer can log it to history. No
dispatch is blocked — CD can always run; it just runs on a cleaner input.

Attempt-to-feedback linkage:
    shots[i].judge_feedback[] has a `ts` field. shots[i].attempts[j] has
    started_at and (optionally) completed_at. A feedback entry is considered
    "tied to" the approved attempt iff its ts falls within [started_at, completed_at].
    If completed_at is absent, we use started_at as a floor only (any feedback
    after started_at is considered fresh — pessimistic but safe for in-flight).

Which attempt is "approved":
    shots[i].final.attempt_id, if present, names the attempt that produced the
    approved render. If shots[i].status != "approved" OR final is absent, the
    contract is skipped (nothing to sanitize yet — CD shouldn't be invoked
    against a shot that isn't approved).
"""

from __future__ import annotations

from typing import Any, Mapping

from .registry import register
from .types import ContractName, Severity, Violation


def _find_attempt(
    shot: Mapping[str, Any], attempt_id: int
) -> Mapping[str, Any] | None:
    for a in shot.get("attempts", []):
        if a.get("attempt_id") == attempt_id:
            return a
    return None


def _fresh_window(approved_attempt: Mapping[str, Any]) -> tuple[str, str | None]:
    started = approved_attempt.get("started_at", "")
    completed = approved_attempt.get("completed_at")
    return started, completed


def _is_in_window(
    entry_ts: str, window_start: str, window_end: str | None
) -> bool:
    if entry_ts < window_start:
        return False
    if window_end is not None and entry_ts > window_end:
        return False
    return True


def filter_judge_feedback_for_cd(shot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return only judge_feedback entries tied to the shot's approved attempt.

    If the shot isn't approved (no `final.attempt_id`), returns the full list
    unchanged — there's no basis to filter. Callers should not invoke CD on
    unapproved shots anyway.
    """
    feedback = list(shot.get("judge_feedback", []))
    final = shot.get("final")
    if not final or "attempt_id" not in final:
        return feedback
    approved = _find_attempt(shot, final["attempt_id"])
    if approved is None:
        return feedback
    window_start, window_end = _fresh_window(approved)
    return [
        f for f in feedback if _is_in_window(f.get("ts", ""), window_start, window_end)
    ]


def check(
    manifest: Mapping[str, Any],
    shot_id: str | None,
    ctx: Mapping[str, Any],
) -> list[Violation]:
    violations: list[Violation] = []
    shots = manifest.get("shots", [])
    targets = [s for s in shots if shot_id is None or s.get("shot_id") == shot_id]

    for shot in targets:
        if shot.get("status") != "approved":
            continue
        final = shot.get("final")
        if not final:
            continue
        fresh = filter_judge_feedback_for_cd(shot)
        total = len(shot.get("judge_feedback", []))
        stale = total - len(fresh)
        if stale > 0:
            violations.append(
                Violation(
                    contract=ContractName.CD_READS_APPROVED_JUDGE_FEEDBACK,
                    severity=Severity.WARN,
                    reason=(
                        f"{stale} stale judge_feedback entries filtered from CD context "
                        f"for shot {shot.get('shot_id')} (approved attempt "
                        f"#{final.get('attempt_id')})"
                    ),
                    shot_id=shot.get("shot_id"),
                    detail={
                        "stale_count": stale,
                        "fresh_count": len(fresh),
                        "approved_attempt_id": final.get("attempt_id"),
                    },
                )
            )
    return violations


register(ContractName.CD_READS_APPROVED_JUDGE_FEEDBACK, check)
