"""Contract 5 — CD <-> Editor authority.

Silent-breakage case per docs/contracts.md § Contract 5:
Editor extends sh_008 by 0.4s to balance audio spill. CD flags "sh_008 now
drags". Producer applies CD's shorten. Editor re-flags "audio spill". Ping-pong.
The creative resolver's convergence gate catches it on the second pass — this
contract pre-empts by ensuring the pre-Editor full-film CD review is resolved
BEFORE Editor dispatches at all.
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
    make_creative_feedback,
    make_manifest,
    make_shot,
)


# -- film-level check (Editor invocation) ----------------------------------


def test_editor_invocation_no_unaddressed_cd_passes() -> None:
    manifest = make_manifest(shots=[make_shot("sh_001"), make_shot("sh_002")])
    warns = validate_before_dispatch("editor_agent", None, manifest, {})
    assert warns == []


def test_editor_invocation_unaddressed_cd_high_blocks() -> None:
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_003",
                creative_feedback=[
                    make_creative_feedback(priority="high", addressed=False),
                ],
            )
        ]
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch("editor_agent", None, manifest, {})
    authority_violations = [
        v
        for v in excinfo.value.violations
        if v.contract == ContractName.CD_EDITOR_AUTHORITY
    ]
    assert len(authority_violations) == 1
    v = authority_violations[0]
    assert v.severity == Severity.BLOCK
    assert "unaddressed CD feedback" in v.reason
    assert v.detail["entries"][0]["shot_id"] == "sh_003"


def test_editor_invocation_unaddressed_cd_critical_blocks() -> None:
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_003",
                creative_feedback=[
                    make_creative_feedback(priority="critical", addressed=False),
                ],
            )
        ]
    )
    with pytest.raises(ContractViolation):
        validate_before_dispatch("editor_agent", None, manifest, {})


def test_editor_invocation_unaddressed_cd_medium_does_not_block() -> None:
    """Medium-priority CD feedback is handled via lighter interventions per
    producer.md; it does not gate Editor invocation."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_003",
                creative_feedback=[
                    make_creative_feedback(priority="medium", addressed=False),
                ],
            )
        ]
    )
    warns = validate_before_dispatch("editor_agent", None, manifest, {})
    assert warns == []


def test_editor_invocation_addressed_cd_does_not_block() -> None:
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_003",
                creative_feedback=[
                    make_creative_feedback(
                        priority="high",
                        addressed=True,
                        addressed_by="producer",
                        addressed_at="2026-04-22T13:00:00Z",
                    ),
                ],
            )
        ]
    )
    warns = validate_before_dispatch("editor_agent", None, manifest, {})
    assert warns == []


def test_editor_invocation_counts_cd_across_shots() -> None:
    """Film-level check aggregates across all shots."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                creative_feedback=[make_creative_feedback(priority="high")],
            ),
            make_shot(
                "sh_005",
                creative_feedback=[make_creative_feedback(priority="critical")],
            ),
        ]
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch("editor_agent", None, manifest, {})
    authority_violations = [
        v
        for v in excinfo.value.violations
        if v.contract == ContractName.CD_EDITOR_AUTHORITY
    ]
    assert len(authority_violations) == 1
    assert len(authority_violations[0].detail["entries"]) == 2


def test_editor_invocation_ignores_non_cd_feedback() -> None:
    """Editor-authored or audio-authored feedback don't trigger this contract."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                creative_feedback=[
                    make_creative_feedback(
                        from_agent="editor_agent", priority="high"
                    ),
                    make_creative_feedback(
                        from_agent="audio_agent", priority="critical"
                    ),
                ],
            )
        ]
    )
    warns = validate_before_dispatch("editor_agent", None, manifest, {})
    assert warns == []


# -- shot-level check (applying Editor feedback on specific shot) ---------


def test_shot_level_equal_priority_cd_wins_warn() -> None:
    """Editor at high, CD at high on same shot — CD wins, Editor deferred (warn)."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_008",
                creative_feedback=[
                    make_creative_feedback(priority="high", addressed=False),
                ],
            )
        ]
    )
    warns = validate_before_dispatch(
        "editor_agent",
        "sh_008",
        manifest,
        {"editor_priority": "high"},
    )
    assert len(warns) == 1
    v = warns[0]
    assert v.contract == ContractName.CD_EDITOR_AUTHORITY
    assert v.severity == Severity.WARN
    assert v.shot_id == "sh_008"
    assert "deferred" in v.reason


def test_shot_level_cd_strictly_higher_blocks() -> None:
    """Editor at high, CD at critical — wrong authority resolution. Block."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_008",
                creative_feedback=[
                    make_creative_feedback(priority="critical", addressed=False),
                ],
            )
        ]
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "editor_agent",
            "sh_008",
            manifest,
            {"editor_priority": "high"},
        )
    authority_violations = [
        v
        for v in excinfo.value.violations
        if v.contract == ContractName.CD_EDITOR_AUTHORITY
    ]
    assert len(authority_violations) == 1
    assert authority_violations[0].severity == Severity.BLOCK
    assert "strictly higher priority" in authority_violations[0].reason


def test_shot_level_cd_strictly_lower_does_not_block_or_warn() -> None:
    """Editor at high, CD at medium — Editor wins; no violation."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_008",
                creative_feedback=[
                    make_creative_feedback(priority="medium", addressed=False),
                ],
            )
        ]
    )
    warns = validate_before_dispatch(
        "editor_agent",
        "sh_008",
        manifest,
        {"editor_priority": "high"},
    )
    assert warns == []


def test_shot_level_no_cd_feedback_no_violation() -> None:
    manifest = make_manifest(shots=[make_shot("sh_008")])
    warns = validate_before_dispatch(
        "editor_agent",
        "sh_008",
        manifest,
        {"editor_priority": "high"},
    )
    assert warns == []


def test_shot_level_missing_editor_priority_ctx_skips_check() -> None:
    """Without editor_priority in ctx we can't compare — contract is a no-op."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_008",
                creative_feedback=[make_creative_feedback(priority="critical")],
            )
        ]
    )
    # shot_id set but no editor_priority -> shot-level check skips.
    # This will fall through to film-level check? No — shot_id is set, so film-level check doesn't run.
    # But the Audio→Editor contract also runs at shot_id level. Make sure it's clean.
    warns = validate_before_dispatch(
        "editor_agent",
        "sh_008",
        manifest,
        {},  # no editor_priority
    )
    # CD_EDITOR_AUTHORITY is skipped (shot_level w/o editor_priority); AUDIO_TO_EDITOR
    # on a shot with no dialogue passes in non-strict mode. Clean result.
    assert warns == []


def test_addressed_cd_does_not_conflict_at_shot_level() -> None:
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_008",
                creative_feedback=[
                    make_creative_feedback(
                        priority="high",
                        addressed=True,
                        addressed_by="producer",
                        addressed_at="2026-04-22T12:30:00Z",
                    ),
                ],
            )
        ]
    )
    warns = validate_before_dispatch(
        "editor_agent",
        "sh_008",
        manifest,
        {"editor_priority": "high"},
    )
    assert warns == []
