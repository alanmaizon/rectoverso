"""Contract 6 — Normalize -> Editor.

Silent-breakage case: the Editor receives a manifest where every shot is
`status == "approved"` but one of them lacks `final.normalized_path`. Without
this contract, the Editor would feed the raw provider render directly into
Hyperframes, reproducing the `non monotonically increasing dts` failure on
the concat muxer (verified 2026-04-23 across Wan / Kling Pro / Seedance).

The contract is block-severity by design — see the module docstring for
the "Editor cannot safely degrade" rationale. No warn path exists.
"""

from __future__ import annotations

import pytest

from src.contracts import (
    ContractName,
    ContractViolation,
    Severity,
    validate_before_dispatch,
)
from tests.contracts.conftest import make_manifest, make_shot


# ---------------------------------------------------------------------------
# Happy paths — no violations
# ---------------------------------------------------------------------------


def test_happy_path_all_approved_shots_normalized() -> None:
    """Every approved shot carries final.normalized_path + normalized_md5
    (auto-populated by make_shot when status='approved')."""
    manifest = make_manifest(
        shots=[
            make_shot("sh_001"),
            make_shot("sh_002"),
        ],
    )
    # No ContractViolation raised; empty warn list returned.
    warns = validate_before_dispatch("editor_agent", None, manifest, {})
    assert warns == []


def test_non_approved_shots_not_checked() -> None:
    """A rejected/escalated/failed shot without normalized_path is out of
    scope — only approved shots feed the Editor."""
    manifest = make_manifest(
        shots=[
            make_shot("sh_001"),
            # rejected shot — not shipping, doesn't need normalization
            make_shot("sh_002", status="rejected"),
            # escalated shot — human review path, not shipping
            make_shot("sh_003", status="escalated"),
            # failed shot — out of the Editor's scope entirely
            make_shot("sh_004", status="failed"),
        ],
    )
    warns = validate_before_dispatch("editor_agent", None, manifest, {})
    assert warns == []


# ---------------------------------------------------------------------------
# Silent-breakage cases — MUST block
# ---------------------------------------------------------------------------


def test_approved_shot_without_normalized_path_blocks_editor_dispatch() -> None:
    """THE silent-breakage case: approved shot, no final.normalized_path.
    Without this contract, Editor dispatches and Hyperframes feeds the raw
    Wan/Kling/Seedance render into concat, producing dts corruption."""
    manifest = make_manifest(
        shots=[
            make_shot("sh_001"),
            make_shot("sh_002", final_attempt_id=1, unnormalized=True),   # approved, render only, NO normalized
        ],
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch("editor_agent", None, manifest, {})

    violations = [
        v for v in excinfo.value.violations
        if v.contract == ContractName.NORMALIZE_TO_EDITOR
    ]
    assert len(violations) == 1
    v = violations[0]
    assert v.severity == Severity.BLOCK
    assert v.shot_id == "sh_002"
    assert "final.normalized_path is missing" in v.reason
    # Operator remediation guidance is in the reason for debuggability.
    assert "rectoverso film --resume" in v.reason


def test_approved_shot_without_final_at_all_still_blocks() -> None:
    """An approved shot that somehow landed without a final block at all
    (schema misconfiguration or manual edit) must also block."""
    manifest = make_manifest(
        shots=[
            make_shot("sh_001", status="approved", unnormalized=True),
        ],
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch("editor_agent", None, manifest, {})

    violations = [
        v for v in excinfo.value.violations
        if v.contract == ContractName.NORMALIZE_TO_EDITOR
    ]
    assert len(violations) == 1
    assert violations[0].shot_id == "sh_001"
    assert violations[0].severity == Severity.BLOCK


def test_empty_string_normalized_path_counts_as_missing() -> None:
    """Presence is not sufficient; the path must be non-empty."""
    manifest = make_manifest(
        shots=[make_shot("sh_001", final_attempt_id=1, unnormalized=True)]
    )
    manifest["shots"][0]["final"]["normalized_path"] = ""
    manifest["shots"][0]["final"]["normalized_md5"] = ""

    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch("editor_agent", None, manifest, {})
    violations = [
        v for v in excinfo.value.violations
        if v.contract == ContractName.NORMALIZE_TO_EDITOR
    ]
    assert len(violations) == 1
    assert violations[0].shot_id == "sh_001"


def test_normalized_path_without_md5_blocks() -> None:
    """Belt-and-braces: if somehow a write slipped through without the md5,
    the audit chain is broken and the contract blocks."""
    manifest = make_manifest(
        shots=[make_shot("sh_001", final_attempt_id=1, unnormalized=True)]
    )
    manifest["shots"][0]["final"]["normalized_path"] = "artifacts/renders/sh_001/norm.mp4"
    # normalized_md5 deliberately absent

    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch("editor_agent", None, manifest, {})
    violations = [
        v for v in excinfo.value.violations
        if v.contract == ContractName.NORMALIZE_TO_EDITOR
    ]
    assert len(violations) == 1
    assert "normalized_md5 is missing" in violations[0].reason


def test_multiple_shots_missing_normalized_produces_one_violation_each() -> None:
    """Contract reports per-shot so the operator sees the full remediation
    list, not just the first failure."""
    manifest = make_manifest(
        shots=[
            make_shot("sh_001"),
            make_shot("sh_002", final_attempt_id=1, unnormalized=True),   # missing
            make_shot("sh_003", final_attempt_id=1, unnormalized=True),   # missing
            make_shot("sh_004"),
        ],
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch("editor_agent", None, manifest, {})
    violations = [
        v for v in excinfo.value.violations
        if v.contract == ContractName.NORMALIZE_TO_EDITOR
    ]
    shot_ids = {v.shot_id for v in violations}
    assert shot_ids == {"sh_002", "sh_003"}
    assert all(v.severity == Severity.BLOCK for v in violations)


# ---------------------------------------------------------------------------
# Scope control
# ---------------------------------------------------------------------------


def test_editor_shot_scope_filters_to_subset() -> None:
    """Future-proofing: when the Editor operates on an explicit subset, only
    those shots are checked. An approved-but-unnormalized shot OUTSIDE the
    subset is not the Editor's problem on this dispatch."""
    manifest = make_manifest(
        shots=[
            make_shot("sh_001"),
            make_shot("sh_002", final_attempt_id=1, unnormalized=True),   # approved, unnormalized, OUT of scope
            make_shot("sh_003"),
        ],
    )
    # Editor dispatch scoped to sh_001 + sh_003 — should pass.
    warns = validate_before_dispatch(
        "editor_agent",
        None,
        manifest,
        {"editor_shot_scope": ["sh_001", "sh_003"]},
    )
    assert warns == []


def test_per_shot_contract_invocation_checks_only_that_shot() -> None:
    """When shot_id is passed, the contract only inspects that one shot."""
    manifest = make_manifest(
        shots=[
            make_shot("sh_001", final_attempt_id=1, unnormalized=True),    # missing — but not checked
            make_shot("sh_002"),
        ],
    )
    # Per-shot call on sh_002 (the healthy one) → clean.
    warns = validate_before_dispatch("editor_agent", "sh_002", manifest, {})
    assert warns == []


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_contract_fires_on_editor_agent_dispatch() -> None:
    """Regression guard for the registry row — Contract 6 only fires when
    agent=editor_agent. A dispatch on a different agent must not run it."""
    manifest = make_manifest(
        shots=[make_shot("sh_001", final_attempt_id=1, unnormalized=True)],
    )
    # prompt_smith dispatch should NOT consult this contract.
    warns = validate_before_dispatch("prompt_smith", None, manifest, {"revision": False})
    assert warns == []
    # screenwriter: ditto.
    warns = validate_before_dispatch("screenwriter", None, manifest, {})
    assert warns == []
