"""Contract 2 — Shot Judge -> PromptSmith.

Silent-breakage case per docs/contracts.md § Contract 2:
PromptSmith is asked to revise a prompt for a shot whose last attempt was
rejected with empty judge_notes. PromptSmith has no signal and produces a
near-identical prompt. Next attempt fails the same way; shot burns attempts.
"""

from __future__ import annotations

import pytest

from src.contracts import (
    ContractName,
    ContractViolation,
    Severity,
    validate_before_dispatch,
)
from tests.contracts.conftest import make_attempt, make_manifest, make_shot


# -- happy path -------------------------------------------------------------


def test_revision_with_populated_judge_notes_passes() -> None:
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                status="rejected",
                attempts=[
                    make_attempt(
                        1,
                        outcome="rejected",
                        rejection_reason="auto_judge",
                        judge_notes="horizon tilt, subject off-center",
                    )
                ],
            )
        ]
    )
    warns = validate_before_dispatch(
        "prompt_smith", "sh_001", manifest, {"revision": True}
    )
    assert warns == []


# -- blocking paths ---------------------------------------------------------


def test_revision_with_empty_judge_notes_blocks() -> None:
    """The silent-breakage case."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                status="rejected",
                attempts=[
                    make_attempt(
                        1,
                        outcome="rejected",
                        rejection_reason="auto_judge",
                        judge_notes="",  # <-- empty
                    )
                ],
            )
        ]
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "prompt_smith", "sh_001", manifest, {"revision": True}
        )
    v = excinfo.value.violations[0]
    assert v.contract == ContractName.SHOT_JUDGE_TO_PROMPT_SMITH
    assert v.severity == Severity.BLOCK
    assert "judge_notes is empty" in v.reason


def test_revision_with_whitespace_only_judge_notes_blocks() -> None:
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                attempts=[
                    make_attempt(
                        1,
                        outcome="rejected",
                        rejection_reason="auto_judge",
                        judge_notes="   \n\t  ",
                    )
                ],
            )
        ]
    )
    with pytest.raises(ContractViolation):
        validate_before_dispatch(
            "prompt_smith", "sh_001", manifest, {"revision": True}
        )


def test_revision_on_approved_shot_blocks() -> None:
    """Shouldn't revise an approved take."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                status="approved",
                final_attempt_id=1,
                attempts=[make_attempt(1, outcome="approved")],
            )
        ]
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "prompt_smith", "sh_001", manifest, {"revision": True}
        )
    assert any(
        "expected 'rejected'" in v.reason for v in excinfo.value.violations
    )


def test_revision_without_attempts_blocks() -> None:
    manifest = make_manifest(shots=[make_shot("sh_001", attempts=[])])
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "prompt_smith", "sh_001", manifest, {"revision": True}
        )
    assert any("no attempts yet" in v.reason for v in excinfo.value.violations)


def test_revision_missing_shot_id_blocks() -> None:
    manifest = make_manifest(shots=[make_shot("sh_001")])
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch("prompt_smith", None, manifest, {"revision": True})
    assert any(
        "without shot_id" in v.reason for v in excinfo.value.violations
    )


def test_revision_unknown_shot_id_blocks() -> None:
    manifest = make_manifest(shots=[make_shot("sh_001")])
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "prompt_smith", "sh_999", manifest, {"revision": True}
        )
    assert any("not found" in v.reason for v in excinfo.value.violations)


# -- warn paths -------------------------------------------------------------


def test_user_rejection_reason_skips_notes_check_with_warn() -> None:
    """A user rejection doesn't require judge_notes — but surface it as a warn."""
    # Note: there's no "user" rejection_reason in the schema enum, so this
    # path is really about "reasons outside the contract's scope". Use 'timeout'
    # which is in the enum.
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                attempts=[
                    make_attempt(
                        1,
                        outcome="rejected",
                        rejection_reason="timeout",
                        judge_notes="",  # empty, but reason is timeout
                    )
                ],
            )
        ]
    )
    warns = validate_before_dispatch(
        "prompt_smith", "sh_001", manifest, {"revision": True}
    )
    assert len(warns) == 1
    assert warns[0].severity == Severity.WARN
    assert "timeout" in warns[0].reason


# -- scope ------------------------------------------------------------------


def test_initial_prompt_call_does_not_check_contract() -> None:
    """revision=False means first-time prompt; contract doesn't fire."""
    manifest = make_manifest(shots=[make_shot("sh_001", attempts=[])])
    # No revision flag -> contract skipped.
    warns = validate_before_dispatch("prompt_smith", "sh_001", manifest, {})
    assert warns == []


def test_contract_does_not_fire_for_editor_or_judge() -> None:
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_001",
                attempts=[
                    make_attempt(
                        1, outcome="rejected", rejection_reason="auto_judge", judge_notes=""
                    )
                ],
            )
        ]
    )
    # These agents don't carry this contract, even with revision flag set.
    for agent in ("editor_agent", "shot_judge", "audio_agent", "creative_director"):
        validate_before_dispatch(agent, "sh_001", manifest, {"revision": True})
