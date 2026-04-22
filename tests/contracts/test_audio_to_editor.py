"""Contract 1 — Audio -> Editor.

Silent-breakage case docs/contracts.md § Contract 1:
Editor proposes "shorten sh_005 by 0.3s" on a shot whose dialogue is 2.8s
with compressibility_s == 0.0. Audio cannot compress; re-render cycles.
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
    make_dialogue,
    make_history_entry,
    make_manifest,
    make_shot,
)


# -- happy paths ------------------------------------------------------------


def test_happy_path_all_dialogue_complete() -> None:
    """Every shot has dialogue with compressibility_s; no violations."""
    manifest = make_manifest(
        shots=[
            make_shot("sh_001"),
            make_shot("sh_002"),
        ],
        dialogue=[
            make_dialogue("sh_001", compressibility_s=0.0),  # floor pace
            make_dialogue("sh_002", compressibility_s=0.4),
        ],
    )
    warns = validate_before_dispatch("editor_agent", None, manifest, {})
    assert warns == []


def test_compressibility_zero_is_acceptable() -> None:
    """compressibility_s == 0.0 means 'already at floor' — not missing data."""
    manifest = make_manifest(
        shots=[make_shot("sh_001")],
        dialogue=[make_dialogue("sh_001", compressibility_s=0.0)],
    )
    warns = validate_before_dispatch("editor_agent", None, manifest, {})
    assert warns == []


def test_silent_shot_implicit_no_violation() -> None:
    """Shot with no dialogue lines passes in default (non-strict) mode."""
    manifest = make_manifest(
        shots=[make_shot("sh_001")],
        dialogue=[],
    )
    warns = validate_before_dispatch("editor_agent", None, manifest, {})
    assert warns == []


# -- violation paths --------------------------------------------------------


def test_missing_compressibility_blocks_editor_dispatch() -> None:
    """The silent-breakage case: dialogue present, compressibility_s missing."""
    manifest = make_manifest(
        shots=[make_shot("sh_005")],
        dialogue=[make_dialogue("sh_005", compressibility_s=None)],  # <- missing
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch("editor_agent", None, manifest, {})
    violations = excinfo.value.violations
    assert len(violations) == 1
    v = violations[0]
    assert v.contract == ContractName.AUDIO_TO_EDITOR
    assert v.severity == Severity.BLOCK
    assert v.shot_id == "sh_005"
    assert "compressibility_s missing" in v.reason


def test_multiple_lines_each_checked_independently() -> None:
    """Two lines on one shot; only one missing compressibility — one violation."""
    manifest = make_manifest(
        shots=[make_shot("sh_001")],
        dialogue=[
            make_dialogue("sh_001", line_id="l1", compressibility_s=0.3),
            make_dialogue("sh_001", line_id="l2", compressibility_s=None),
        ],
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch("editor_agent", None, manifest, {})
    violations = excinfo.value.violations
    assert len(violations) == 1
    assert violations[0].detail.get("line_id") == "l2"


# -- strict-silence mode ----------------------------------------------------


def test_strict_silence_no_marker_blocks() -> None:
    """In strict mode, a shot with no dialogue and no silent marker blocks."""
    manifest = make_manifest(
        shots=[make_shot("sh_003")],
        dialogue=[],
    )
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch(
            "editor_agent",
            None,
            manifest,
            {"require_silent_marker": True},
        )
    assert any(
        "no silent marker" in v.reason for v in excinfo.value.violations
    )


def test_strict_silence_history_marker_passes() -> None:
    """Shot has shot_silent history entry — passes strict mode."""
    manifest = make_manifest(
        shots=[
            make_shot(
                "sh_003",
                history=[make_history_entry("shot_silent")],
            )
        ],
        dialogue=[],
    )
    warns = validate_before_dispatch(
        "editor_agent",
        None,
        manifest,
        {"require_silent_marker": True},
    )
    assert warns == []


def test_strict_silence_ctx_list_passes() -> None:
    """Caller can pass ctx['silent_shots'] instead of a history marker."""
    manifest = make_manifest(
        shots=[make_shot("sh_003")],
        dialogue=[],
    )
    warns = validate_before_dispatch(
        "editor_agent",
        None,
        manifest,
        {"require_silent_marker": True, "silent_shots": ["sh_003"]},
    )
    assert warns == []


# -- per-shot dispatch ------------------------------------------------------


def test_per_shot_dispatch_only_checks_named_shot() -> None:
    """With shot_id set, only that shot is checked — others are skipped."""
    manifest = make_manifest(
        shots=[make_shot("sh_001"), make_shot("sh_002")],
        dialogue=[
            make_dialogue("sh_001", compressibility_s=0.3),  # clean
            make_dialogue("sh_002", compressibility_s=None),  # dirty, but not checked
        ],
    )
    warns = validate_before_dispatch("editor_agent", "sh_001", manifest, {})
    assert warns == []


def test_per_shot_dispatch_unknown_shot_id_blocks() -> None:
    manifest = make_manifest(shots=[make_shot("sh_001")], dialogue=[])
    with pytest.raises(ContractViolation) as excinfo:
        validate_before_dispatch("editor_agent", "sh_999", manifest, {})
    assert any("not found" in v.reason for v in excinfo.value.violations)


# -- scope -------------------------------------------------------------------


def test_contract_does_not_fire_for_non_editor_agents() -> None:
    """creative_director / shot_judge / audio_agent don't trigger this contract."""
    manifest = make_manifest(
        shots=[make_shot("sh_001")],
        dialogue=[make_dialogue("sh_001", compressibility_s=None)],  # would block editor
    )
    # None of these should raise — contract is not in their registry.
    for agent in ("creative_director", "shot_judge", "audio_agent", "screenwriter"):
        validate_before_dispatch(agent, None, manifest, {})
