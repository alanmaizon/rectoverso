"""Checkpoint 2 smoke tests: package imports, registry resolves, stubs return clean."""

from __future__ import annotations

import pytest

from src.contracts import (
    ContractName,
    ContractViolation,
    Severity,
    Violation,
    validate_before_dispatch,
)
from src.contracts.registry import contracts_for_dispatch


EMPTY_MANIFEST = {
    "manifest_version": "1.0",
    "shots": [],
    "audio": {"dialogue": [], "sfx": []},
    "creative_decisions": [],
}


def test_all_contracts_registered() -> None:
    from src.contracts.registry import _CHECKS

    registered = set(_CHECKS.keys())
    expected = {
        ContractName.AUDIO_TO_EDITOR,
        ContractName.SHOT_JUDGE_TO_PROMPT_SMITH,
        ContractName.CD_TO_PROMPT_SMITH,
        ContractName.CD_READS_APPROVED_JUDGE_FEEDBACK,
        ContractName.CD_EDITOR_AUTHORITY,
        ContractName.NORMALIZE_TO_EDITOR,
    }
    assert registered == expected


def test_registry_maps_editor_to_audio_cd_and_normalize() -> None:
    checks = contracts_for_dispatch("editor_agent", {})
    assert ContractName.AUDIO_TO_EDITOR in checks
    assert ContractName.CD_EDITOR_AUTHORITY in checks
    assert ContractName.NORMALIZE_TO_EDITOR in checks


def test_registry_maps_prompt_smith_revision_to_judge_contract() -> None:
    assert contracts_for_dispatch("prompt_smith", {"revision": True}) == [
        ContractName.SHOT_JUDGE_TO_PROMPT_SMITH
    ]


def test_registry_maps_creative_prompt_smith_to_both_contracts() -> None:
    checks = contracts_for_dispatch(
        "prompt_smith", {"revision": True, "creative_driven": True}
    )
    assert ContractName.SHOT_JUDGE_TO_PROMPT_SMITH in checks
    assert ContractName.CD_TO_PROMPT_SMITH in checks


def test_registry_first_time_prompt_smith_call_has_no_contracts() -> None:
    # Initial (non-revision) call has nothing to check — shot hasn't been judged yet.
    assert contracts_for_dispatch("prompt_smith", {}) == []


def test_registry_maps_creative_director_to_sanitization_contract() -> None:
    assert contracts_for_dispatch("creative_director", {}) == [
        ContractName.CD_READS_APPROVED_JUDGE_FEEDBACK
    ]


def test_registry_shot_judge_and_audio_agent_have_no_contracts() -> None:
    assert contracts_for_dispatch("shot_judge", {}) == []
    assert contracts_for_dispatch("audio_agent", {}) == []
    assert contracts_for_dispatch("screenwriter", {}) == []


def test_registry_renderer_creative_driven_invokes_cd_contract() -> None:
    assert contracts_for_dispatch("renderer", {"creative_driven": True}) == [
        ContractName.CD_TO_PROMPT_SMITH
    ]
    assert contracts_for_dispatch("renderer", {}) == []


def test_validate_before_dispatch_clean_on_empty_manifest() -> None:
    # Every agent on an empty manifest should return no warns and not raise.
    for agent in ("editor_agent", "creative_director", "shot_judge", "audio_agent"):
        assert validate_before_dispatch(agent, None, EMPTY_MANIFEST, {}) == []


def test_validate_before_dispatch_prompt_smith_revision_on_unknown_shot_blocks() -> None:
    """Revision dispatch on a shot that isn't in the manifest blocks via the
    shot_judge_to_prompt_smith contract — not a scaffold concern anymore; the
    real contract is wired. Kept here as smoke test that revision ctx routes
    through the registry correctly."""
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "prompt_smith", "sh_001", EMPTY_MANIFEST, {"revision": True}
        )
    assert any(
        v.contract == ContractName.SHOT_JUDGE_TO_PROMPT_SMITH
        for v in excinfo.value.violations
    )


def test_contract_violation_carries_blocking_list() -> None:
    v = Violation(
        contract=ContractName.AUDIO_TO_EDITOR,
        severity=Severity.BLOCK,
        reason="test",
        shot_id="sh_001",
    )
    with pytest.raises(ContractViolation) as excinfo:
        raise ContractViolation([v])
    assert excinfo.value.violations == [v]
    assert "audio_to_editor" in str(excinfo.value)
    assert "sh_001" in str(excinfo.value)


def test_violation_fields_are_frozen() -> None:
    v = Violation(
        contract=ContractName.AUDIO_TO_EDITOR,
        severity=Severity.WARN,
        reason="example",
    )
    with pytest.raises(Exception):
        v.reason = "mutated"  # type: ignore[misc]
