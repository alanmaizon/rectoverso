"""Contract 4 — CD reads only approved-attempt judge feedback.

Silent-breakage case per docs/contracts.md § Contract 4:
Shot 3 had attempts 1 (rejected "too cool, too slow"), 2 (rejected), 3 (approved).
Stale judge_feedback from attempts 1 and 2 still sits on the shot. CD, invoked
mid-production, sees "too cool, too slow" and echoes "tonal drift on sh_003" —
but attempt 3 actually resolved it. CD's suggestion is wrong.

This contract filters the stale entries BEFORE CD sees them, so it can't happen.
"""

from __future__ import annotations

from src.contracts import (
    ContractName,
    Severity,
    validate_before_dispatch,
)
from src.contracts.cd_reads_approved_judge_feedback import filter_judge_feedback_for_cd
from tests.contracts.conftest import (
    make_attempt,
    make_judge_feedback,
    make_manifest,
    make_shot,
)


# -- happy paths (no stale feedback) ----------------------------------------


def test_all_feedback_in_approved_window_no_warn() -> None:
    """Single approved attempt, feedback within its time window — no warn."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                status="approved",
                final_attempt_id=1,
                attempts=[
                    make_attempt(
                        1,
                        outcome="approved",
                        started_at="2026-04-22T10:00:00Z",
                        completed_at="2026-04-22T10:02:00Z",
                    )
                ],
                judge_feedback=[
                    make_judge_feedback(ts="2026-04-22T10:01:00Z"),
                ],
            )
        ]
    )
    warns = validate_before_dispatch("creative_director", None, manifest, {})
    assert warns == []


def test_empty_judge_feedback_no_warn() -> None:
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                status="approved",
                final_attempt_id=1,
                attempts=[make_attempt(1, outcome="approved")],
                judge_feedback=[],
            )
        ]
    )
    warns = validate_before_dispatch("creative_director", None, manifest, {})
    assert warns == []


# -- silent-breakage case: stale feedback filtered out -----------------------


def test_stale_feedback_from_rejected_attempts_emits_warn_and_filter_prunes() -> None:
    """The silent-breakage case: attempts 1, 2 rejected, attempt 3 approved.

    Three judge_feedback entries: entries from attempts 1 and 2 are stale.
    The contract emits a warn; filter_judge_feedback_for_cd returns only fresh.
    """
    shot = make_shot(
        "sh_003",
        status="approved",
        final_attempt_id=3,
        attempts=[
            make_attempt(
                1,
                outcome="rejected",
                rejection_reason="auto_judge",
                started_at="2026-04-22T09:00:00Z",
                completed_at="2026-04-22T09:02:00Z",
                judge_notes="too cool, too slow",
            ),
            make_attempt(
                2,
                outcome="rejected",
                rejection_reason="auto_judge",
                started_at="2026-04-22T09:10:00Z",
                completed_at="2026-04-22T09:12:00Z",
                judge_notes="still too cool",
            ),
            make_attempt(
                3,
                outcome="approved",
                started_at="2026-04-22T09:20:00Z",
                completed_at="2026-04-22T09:22:00Z",
            ),
        ],
        judge_feedback=[
            make_judge_feedback(ts="2026-04-22T09:01:30Z"),  # in attempt 1
            make_judge_feedback(ts="2026-04-22T09:11:30Z"),  # in attempt 2
            make_judge_feedback(ts="2026-04-22T09:21:30Z"),  # in attempt 3 (fresh)
        ],
    )
    manifest = make_manifest(shots=[shot])

    warns = validate_before_dispatch("creative_director", None, manifest, {})
    assert len(warns) == 1
    v = warns[0]
    assert v.contract == ContractName.CD_READS_APPROVED_JUDGE_FEEDBACK
    assert v.severity == Severity.WARN
    assert v.shot_id == "sh_003"
    assert v.detail["stale_count"] == 2
    assert v.detail["fresh_count"] == 1
    assert v.detail["approved_attempt_id"] == 3

    fresh = filter_judge_feedback_for_cd(shot)
    assert len(fresh) == 1
    assert fresh[0]["ts"] == "2026-04-22T09:21:30Z"


def test_filter_preserves_original_when_no_approved_attempt() -> None:
    """Shot not yet approved — filter returns full list (nothing to prune against)."""
    shot = make_shot(
        "sh_001",
        status="rejected",
        attempts=[make_attempt(1, outcome="rejected", rejection_reason="auto_judge")],
        judge_feedback=[make_judge_feedback()],
    )
    assert filter_judge_feedback_for_cd(shot) == shot["judge_feedback"]


def test_unapproved_shot_skipped_by_contract() -> None:
    """Shots not in 'approved' status don't emit warns — they shouldn't be CD's input."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                status="rejected",
                attempts=[make_attempt(1, outcome="rejected", rejection_reason="auto_judge")],
                judge_feedback=[make_judge_feedback()],
            )
        ]
    )
    warns = validate_before_dispatch("creative_director", None, manifest, {})
    assert warns == []


# -- edge cases -------------------------------------------------------------


def test_approved_attempt_missing_completed_at_still_filters() -> None:
    """When completed_at is absent, window is [started_at, ∞)."""
    shot = make_shot(
        "sh_001",
        status="approved",
        final_attempt_id=2,
        attempts=[
            make_attempt(
                1,
                outcome="rejected",
                rejection_reason="auto_judge",
                started_at="2026-04-22T09:00:00Z",
                completed_at="2026-04-22T09:02:00Z",
            ),
            make_attempt(
                2,
                outcome="approved",
                started_at="2026-04-22T09:10:00Z",
                completed_at=None,  # still in flight when feedback was written
            ),
        ],
        judge_feedback=[
            make_judge_feedback(ts="2026-04-22T09:01:00Z"),  # stale
            make_judge_feedback(ts="2026-04-22T09:15:00Z"),  # fresh
        ],
    )
    fresh = filter_judge_feedback_for_cd(shot)
    assert len(fresh) == 1
    assert fresh[0]["ts"] == "2026-04-22T09:15:00Z"


def test_per_shot_dispatch_limits_to_named_shot() -> None:
    """shot_id=sh_002 means only sh_002 is checked even if sh_001 has stale entries."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                status="approved",
                final_attempt_id=2,
                attempts=[
                    make_attempt(1, outcome="rejected", rejection_reason="auto_judge",
                                 started_at="2026-04-22T09:00:00Z",
                                 completed_at="2026-04-22T09:02:00Z"),
                    make_attempt(2, outcome="approved",
                                 started_at="2026-04-22T09:10:00Z",
                                 completed_at="2026-04-22T09:12:00Z"),
                ],
                judge_feedback=[make_judge_feedback(ts="2026-04-22T09:01:00Z")],  # stale
            ),
            make_shot(
                "sh_002",
                status="approved",
                final_attempt_id=1,
                attempts=[make_attempt(1, outcome="approved",
                                       started_at="2026-04-22T10:00:00Z",
                                       completed_at="2026-04-22T10:02:00Z")],
                judge_feedback=[make_judge_feedback(ts="2026-04-22T10:01:00Z")],  # fresh
            ),
        ]
    )
    warns = validate_before_dispatch("creative_director", "sh_002", manifest, {})
    assert warns == []  # sh_002 is clean; sh_001 wasn't checked


def test_contract_never_blocks() -> None:
    """Even with egregiously stale feedback, this contract never raises — only warns."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                status="approved",
                final_attempt_id=1,
                attempts=[make_attempt(1, outcome="approved",
                                       started_at="2026-04-22T10:00:00Z",
                                       completed_at="2026-04-22T10:02:00Z")],
                judge_feedback=[
                    make_judge_feedback(ts="2026-04-22T08:00:00Z") for _ in range(10)
                ],
            )
        ]
    )
    # Should not raise; should return warns.
    warns = validate_before_dispatch("creative_director", None, manifest, {})
    assert len(warns) == 1
    assert warns[0].severity == Severity.WARN
    assert warns[0].detail["stale_count"] == 10
