"""Contract 3 — Creative Director -> PromptSmith.

Silent-breakage case per docs/contracts.md § Contract 3:
CD writes "re-render with slower handheld camera" as a suggestion. Producer
dispatches a re-render WITHOUT translating that into artistic_direction.
PromptSmith regenerates from the original brief — new prompt is substantively
identical, re-render fails the same way. CD's guidance is silently lost.

Most tests below use the `renderer` agent with `creative_driven=True` so only
this one contract fires. A dedicated combo test at the bottom covers the
`prompt_smith` path where this contract runs alongside the judge contract.
"""

from __future__ import annotations

import pytest

from src.contracts import (
    ContractName,
    ContractViolation,
    Severity,
    validate_before_dispatch,
)
from tests.contracts.conftest import (
    make_attempt,
    make_creative_feedback,
    make_history_entry,
    make_manifest,
    make_shot,
)


# -- happy paths ------------------------------------------------------------


def test_artistic_direction_updated_after_cd_feedback_passes() -> None:
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                creative_feedback=[
                    make_creative_feedback(ts="2026-04-22T12:00:00Z"),
                ],
                history=[
                    make_history_entry(
                        "artistic_direction_updated",
                        ts="2026-04-22T12:01:00Z",
                    ),
                ],
                artistic_direction="slow, deliberate handheld camera, muted color",
            )
        ]
    )
    warns = validate_before_dispatch(
        "renderer",
        "sh_001",
        manifest,
        {"creative_driven": True},
    )
    assert warns == []


def test_exact_same_timestamp_counts_as_updated() -> None:
    """Edge case: history entry at the same ts as CD feedback. >= is inclusive."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                creative_feedback=[
                    make_creative_feedback(ts="2026-04-22T12:00:00Z"),
                ],
                history=[
                    make_history_entry(
                        "artistic_direction_updated",
                        ts="2026-04-22T12:00:00Z",
                    ),
                ],
                artistic_direction="noir lighting, handheld",
            )
        ]
    )
    warns = validate_before_dispatch(
        "renderer",
        "sh_001",
        manifest,
        {"creative_driven": True},
    )
    assert warns == []


# -- blocking paths (silent breakage) --------------------------------------


def test_no_history_update_blocks() -> None:
    """The silent-breakage case."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                creative_feedback=[make_creative_feedback(ts="2026-04-22T12:00:00Z")],
                history=[],  # <-- no artistic_direction_updated event
                artistic_direction="some old direction",
            )
        ]
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "renderer",
            "sh_001",
            manifest,
            {"creative_driven": True},
        )
    v = excinfo.value.violations[0]
    assert v.contract == ContractName.CD_TO_PROMPT_SMITH
    assert v.severity == Severity.BLOCK
    assert "no 'artistic_direction_updated'" in v.reason


def test_history_update_predates_cd_feedback_blocks() -> None:
    """Producer updated artistic_direction earlier; then CD wrote new feedback.
    The old update doesn't satisfy the new feedback."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                creative_feedback=[make_creative_feedback(ts="2026-04-22T12:00:00Z")],
                history=[
                    make_history_entry(
                        "artistic_direction_updated",
                        ts="2026-04-22T10:00:00Z",  # earlier than CD feedback
                    )
                ],
                artistic_direction="stale direction from earlier round",
            )
        ]
    )
    with pytest.raises(ContractViolation):
        validate_before_dispatch(
            "renderer",
            "sh_001",
            manifest,
            {"creative_driven": True},
        )


def test_empty_artistic_direction_blocks() -> None:
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                creative_feedback=[make_creative_feedback()],
                history=[
                    make_history_entry(
                        "artistic_direction_updated",
                        ts="2026-04-22T12:05:00Z",
                    )
                ],
                artistic_direction="   ",  # whitespace-only
            )
        ]
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "renderer",
            "sh_001",
            manifest,
            {"creative_driven": True},
        )
    assert any(
        "artistic_direction is empty" in v.reason for v in excinfo.value.violations
    )


# -- warn & scope paths ----------------------------------------------------


def test_no_unaddressed_cd_feedback_emits_warn_not_block() -> None:
    """Caller labeled dispatch creative_driven but all CD feedback is addressed."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                creative_feedback=[
                    make_creative_feedback(
                        addressed=True,
                        addressed_at="2026-04-22T11:00:00Z",
                        addressed_by="producer",
                    )
                ],
                artistic_direction="addressed direction",
            )
        ]
    )
    warns = validate_before_dispatch(
        "renderer",
        "sh_001",
        manifest,
        {"creative_driven": True},
    )
    assert len(warns) == 1
    assert warns[0].severity == Severity.WARN
    assert warns[0].contract == ContractName.CD_TO_PROMPT_SMITH


def test_multiple_cd_feedback_latest_ts_is_the_bar() -> None:
    """With two unaddressed CD entries, the history update must be after the latest."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                creative_feedback=[
                    make_creative_feedback(ts="2026-04-22T10:00:00Z"),
                    make_creative_feedback(ts="2026-04-22T14:00:00Z"),  # latest
                ],
                history=[
                    make_history_entry(
                        "artistic_direction_updated",
                        ts="2026-04-22T12:00:00Z",  # between the two
                    ),
                ],
                artistic_direction="direction from the 12:00 update",
            )
        ]
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "renderer",
            "sh_001",
            manifest,
            {"creative_driven": True},
        )
    # Find the CD violation (only one fires for renderer+creative_driven).
    cd_violations = [
        v
        for v in excinfo.value.violations
        if v.contract == ContractName.CD_TO_PROMPT_SMITH
    ]
    assert len(cd_violations) == 1
    assert cd_violations[0].detail.get("latest_cd_ts") == "2026-04-22T14:00:00Z"


def test_non_creative_driven_dispatch_does_not_fire_contract() -> None:
    """Renderer dispatch without creative_driven flag skips this contract."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                creative_feedback=[make_creative_feedback()],  # would block if fired
            )
        ]
    )
    warns = validate_before_dispatch("renderer", "sh_001", manifest, {})
    assert warns == []


def test_prompt_smith_creative_driven_revision_fires_both_contracts() -> None:
    """PromptSmith with both revision+creative_driven: judge AND CD contracts fire.

    Shot has a rejected attempt with judge_notes (judge contract passes), and
    properly updated artistic_direction (CD contract passes). Clean dispatch.
    """
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                attempts=[
                    make_attempt(
                        1,
                        outcome="rejected",
                        rejection_reason="auto_judge",
                        judge_notes="too fast, breaks tonal quiet",
                    ),
                ],
                creative_feedback=[make_creative_feedback(ts="2026-04-22T12:00:00Z")],
                history=[
                    make_history_entry(
                        "artistic_direction_updated",
                        ts="2026-04-22T12:05:00Z",
                    )
                ],
                artistic_direction="slow deliberate handheld",
            )
        ]
    )
    warns = validate_before_dispatch(
        "prompt_smith",
        "sh_001",
        manifest,
        {"revision": True, "creative_driven": True},
    )
    assert warns == []


def test_missing_shot_id_blocks() -> None:
    manifest = make_manifest(shots=[make_shot("sh_001")])
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "renderer",
            None,
            manifest,
            {"creative_driven": True},
        )
    assert any(
        v.contract == ContractName.CD_TO_PROMPT_SMITH
        and "requires a shot_id" in v.reason
        for v in excinfo.value.violations
    )


def test_unknown_shot_id_blocks() -> None:
    manifest = make_manifest(shots=[make_shot("sh_001")])
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "renderer",
            "sh_999",
            manifest,
            {"creative_driven": True},
        )
    assert any("not found" in v.reason for v in excinfo.value.violations)
