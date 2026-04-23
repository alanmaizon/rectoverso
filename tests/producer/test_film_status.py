"""Film-level compose state-machine tests.

Coverage:
    - schema: enum accepts all four states; unknown values rejected
    - loader migration: absent field -> pending
    - current_status: returns None when absent, str when present
    - transition: happy paths for every edge in VALID_TRANSITIONS
    - transition: invalid edges raise InvalidTransition
    - transition: unknown target raises ValueError
    - composed is terminal: no transitions out
    - clear_edit_artifacts: removes top-level entries, idempotent on empty
    - option-a invariant: every transition into pending clears edit
    - other transitions (assembling, composed, compose_failed) do NOT clear
    - recover_on_startup: assembling -> pending + clear + event emitted
    - recover_on_startup: idempotent on pending, composed, compose_failed
    - orchestrator recovery integration: construction persists the transition
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.producer import (
    ASSEMBLING,
    COMPOSED,
    COMPOSE_FAILED,
    FILM_STATUSES,
    FilmOrchestrator,
    InvalidTransition,
    PENDING,
    RecoveryReport,
    ToolSet,
    VALID_TRANSITIONS,
    clear_edit_artifacts,
    current_status,
    load_manifest,
    open_event_log,
    recover_on_startup,
    save_manifest_atomic,
    transition,
    validate_manifest,
)
from tests.producer.conftest import minimal_manifest


# ---------------------------------------------------------------------------
# Helpers — build a project-root layout so clear_edit_artifacts has real
# stuff to remove.
# ---------------------------------------------------------------------------


def _seed_edit_workspace(project_root: Path, *, files: int = 3) -> list[Path]:
    """Create a populated artifacts/edit/ directory so tests can observe
    the clear behavior. Returns the paths created (for assertions)."""
    edit = project_root / "artifacts" / "edit"
    edit.mkdir(parents=True, exist_ok=True)
    created = []
    for i in range(files):
        p = edit / f"file_{i}.txt"
        p.write_text(f"content {i}")
        created.append(p)
    # Nested asset dir to exercise the is_dir branch in rmtree
    assets = edit / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "shots.txt").write_text("x")
    created.append(assets)
    return created


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_accepts_every_valid_film_status() -> None:
    for status in FILM_STATUSES:
        m = minimal_manifest()
        m["film_status"] = status
        validate_manifest(m)   # must not raise


def test_schema_rejects_unknown_film_status() -> None:
    from src.producer import ManifestValidationError
    m = minimal_manifest()
    m["film_status"] = "rendering"   # not in the enum
    with pytest.raises(ManifestValidationError):
        validate_manifest(m)


# ---------------------------------------------------------------------------
# Loader migration
# ---------------------------------------------------------------------------


def test_loader_injects_film_status_on_legacy_manifest(tmp_path: Path) -> None:
    """Pre-existing manifests (no film_status field) come back from
    load_manifest with film_status='pending' injected."""
    manifest = minimal_manifest()   # no film_status
    manifest_path = tmp_path / "manifest.json"
    save_manifest_atomic(manifest_path, manifest, last_event_id=0)
    # Drop the field to simulate legacy on-disk state
    import json
    raw = json.loads(manifest_path.read_text())
    raw.pop("film_status", None)
    manifest_path.write_text(json.dumps(raw))

    load = load_manifest(manifest_path)
    assert current_status(load.manifest) == PENDING


def test_loader_preserves_existing_film_status(tmp_path: Path) -> None:
    manifest = minimal_manifest()
    manifest["film_status"] = ASSEMBLING
    manifest_path = tmp_path / "manifest.json"
    save_manifest_atomic(manifest_path, manifest, last_event_id=0)

    load = load_manifest(manifest_path)
    assert current_status(load.manifest) == ASSEMBLING


def test_current_status_returns_none_on_legacy_dict() -> None:
    manifest = minimal_manifest()
    manifest.pop("film_status", None)
    assert current_status(manifest) is None


# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------


def test_pending_to_assembling(tmp_path: Path) -> None:
    m = minimal_manifest()
    m["film_status"] = PENDING
    prior, cleared, _ = transition(m, ASSEMBLING, project_root=tmp_path)
    assert prior == PENDING
    assert m["film_status"] == ASSEMBLING
    assert cleared == 0   # no clear on non-pending transitions


def test_assembling_to_composed(tmp_path: Path) -> None:
    m = minimal_manifest()
    m["film_status"] = ASSEMBLING
    transition(m, COMPOSED, project_root=tmp_path)
    assert m["film_status"] == COMPOSED


def test_assembling_to_compose_failed(tmp_path: Path) -> None:
    m = minimal_manifest()
    m["film_status"] = ASSEMBLING
    transition(m, COMPOSE_FAILED, project_root=tmp_path)
    assert m["film_status"] == COMPOSE_FAILED


def test_assembling_to_pending_clears_edit(tmp_path: Path) -> None:
    """Dead-session recovery path via explicit transition."""
    seeded = _seed_edit_workspace(tmp_path, files=2)
    # Sanity — files are on disk
    for p in seeded:
        assert p.exists()

    m = minimal_manifest()
    m["film_status"] = ASSEMBLING
    prior, cleared_count, cleared_paths = transition(m, PENDING, project_root=tmp_path)
    assert prior == ASSEMBLING
    assert m["film_status"] == PENDING
    assert cleared_count >= 2
    # All seeded top-level entries are gone
    for p in seeded:
        assert not p.exists()


def test_compose_failed_to_pending_clears_edit(tmp_path: Path) -> None:
    """Option-a invariant: every transition INTO pending clears edit,
    not just from assembling."""
    seeded = _seed_edit_workspace(tmp_path, files=1)
    m = minimal_manifest()
    m["film_status"] = COMPOSE_FAILED
    _, cleared_count, _ = transition(m, PENDING, project_root=tmp_path)
    assert cleared_count >= 1
    for p in seeded:
        assert not p.exists()


def test_migration_none_to_pending(tmp_path: Path) -> None:
    m = minimal_manifest()
    m.pop("film_status", None)
    prior, _, _ = transition(m, PENDING, project_root=tmp_path)
    assert prior is None
    assert m["film_status"] == PENDING


# ---------------------------------------------------------------------------
# Invalid transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current,target",
    [
        (PENDING, COMPOSED),
        (PENDING, COMPOSE_FAILED),
        (PENDING, PENDING),                     # self-loop not allowed
        (ASSEMBLING, ASSEMBLING),
        (COMPOSED, ASSEMBLING),                 # terminal out
        (COMPOSED, PENDING),
        (COMPOSED, COMPOSE_FAILED),
        (COMPOSE_FAILED, ASSEMBLING),
        (COMPOSE_FAILED, COMPOSED),
    ],
)
def test_invalid_transitions_raise(
    current: str, target: str, tmp_path: Path
) -> None:
    m = minimal_manifest()
    m["film_status"] = current
    with pytest.raises(InvalidTransition) as excinfo:
        transition(m, target, project_root=tmp_path)
    assert excinfo.value.current == current
    assert excinfo.value.target == target
    # Manifest is NOT mutated on failed transition
    assert m["film_status"] == current


def test_unknown_target_raises_value_error(tmp_path: Path) -> None:
    m = minimal_manifest()
    m["film_status"] = PENDING
    with pytest.raises(ValueError):
        transition(m, "rendering", project_root=tmp_path)
    assert m["film_status"] == PENDING


def test_composed_is_terminal() -> None:
    """Registry table spot-check — composed has an empty allowed-next set."""
    assert VALID_TRANSITIONS[COMPOSED] == frozenset()


# ---------------------------------------------------------------------------
# clear_edit_artifacts
# ---------------------------------------------------------------------------


def test_clear_edit_artifacts_removes_top_level(tmp_path: Path) -> None:
    seeded = _seed_edit_workspace(tmp_path, files=2)
    count, paths = clear_edit_artifacts(tmp_path)
    assert count >= 2
    assert len(paths) == count
    # Edit dir itself remains (rmtree applied to children, not the dir)
    assert (tmp_path / "artifacts" / "edit").exists()
    # But all seeded entries are gone
    for p in seeded:
        assert not p.exists()


def test_clear_edit_artifacts_idempotent_when_dir_absent(tmp_path: Path) -> None:
    count, paths = clear_edit_artifacts(tmp_path)
    assert count == 0
    assert paths == ()


def test_clear_edit_artifacts_idempotent_on_empty_dir(tmp_path: Path) -> None:
    (tmp_path / "artifacts" / "edit").mkdir(parents=True)
    count, _ = clear_edit_artifacts(tmp_path)
    assert count == 0


# ---------------------------------------------------------------------------
# recover_on_startup
# ---------------------------------------------------------------------------


def test_recover_on_assembling_transitions_and_clears(tmp_path: Path) -> None:
    seeded = _seed_edit_workspace(tmp_path, files=3)
    m = minimal_manifest()
    m["film_status"] = ASSEMBLING

    with open_event_log(tmp_path / "events.db") as events:
        report = recover_on_startup(m, project_root=tmp_path, events=events)

        assert report.ran is True
        assert report.prior_status == ASSEMBLING
        assert report.new_status == PENDING
        assert report.cleared_count >= 3
        # Edit dir is empty of the seeded files
        for p in seeded:
            assert not p.exists()
        # Manifest flipped to pending
        assert m["film_status"] == PENDING
        # Event emitted with recovery payload
        rows = [e for e in events.recent(limit=10) if e.kind == "film_status_transition"]
        assert len(rows) == 1
        assert rows[0].agent == "orchestrator"
        assert rows[0].payload["from"] == ASSEMBLING
        assert rows[0].payload["to"] == PENDING
        assert rows[0].payload["reason"] == "dead_session_recovery"
        assert rows[0].payload["cleared_count"] >= 3


@pytest.mark.parametrize("status", [PENDING, COMPOSED, COMPOSE_FAILED])
def test_recover_is_noop_for_non_assembling(status: str, tmp_path: Path) -> None:
    """Idempotency: recovery on any non-assembling state is a no-op."""
    seeded = _seed_edit_workspace(tmp_path, files=2)
    m = minimal_manifest()
    m["film_status"] = status

    with open_event_log(tmp_path / "events.db") as events:
        report = recover_on_startup(m, project_root=tmp_path, events=events)
        assert report.ran is False
        assert m["film_status"] == status
        # Edit artifacts untouched — recovery didn't fire, so no clear
        for p in seeded:
            assert p.exists()
        # No event emitted
        rows = [e for e in events.recent(limit=10) if e.kind == "film_status_transition"]
        assert len(rows) == 0


def test_recover_is_idempotent_on_legacy_manifest(tmp_path: Path) -> None:
    """No film_status field (legacy) → recovery no-op, doesn't raise."""
    m = minimal_manifest()
    m.pop("film_status", None)
    report = recover_on_startup(m, project_root=tmp_path, events=None)
    assert report.ran is False


def test_recover_second_call_on_pending_is_noop(tmp_path: Path) -> None:
    """Calling recover twice in a row — second call must be clean no-op.
    This is the resume-safety invariant: repeated startup never re-clears."""
    seeded = _seed_edit_workspace(tmp_path, files=2)
    m = minimal_manifest()
    m["film_status"] = ASSEMBLING

    # First call does the work
    with open_event_log(tmp_path / "events.db") as events:
        first = recover_on_startup(m, project_root=tmp_path, events=events)
        assert first.ran is True

        # Re-seed to prove the second call doesn't clear
        reseeded = _seed_edit_workspace(tmp_path, files=2)

        # Second call — state is now pending, no-op expected
        second = recover_on_startup(m, project_root=tmp_path, events=events)
        assert second.ran is False
        for p in reseeded:
            assert p.exists()


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


def test_orchestrator_construction_runs_recovery(tmp_path: Path) -> None:
    """FilmOrchestrator.__init__ calls recover_on_startup. If the on-disk
    manifest is in 'assembling', construction flips it to 'pending' and
    persists that back to disk."""
    seeded = _seed_edit_workspace(tmp_path, files=2)
    manifest = minimal_manifest()
    manifest["film_status"] = ASSEMBLING
    manifest_path = tmp_path / "state" / "manifest.json"
    save_manifest_atomic(manifest_path, manifest, last_event_id=0)

    # Reload so we're testing the flow from a fresh dict
    load = load_manifest(manifest_path)
    assert current_status(load.manifest) == ASSEMBLING   # still assembling pre-construction

    with open_event_log(tmp_path / "state" / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=load.manifest,
            manifest_path=manifest_path,
            events=events,
            tools=ToolSet(
                render=lambda **_kw: {"status": "ok"},
                judge=lambda **_kw: {},
                revise=lambda **_kw: {},
            ),
            project_root=tmp_path,
        )
        # Recovery fired
        assert orch._recovery_report.ran is True
        assert orch._recovery_report.prior_status == ASSEMBLING
        assert current_status(load.manifest) == PENDING
        # Edit artifacts were cleared
        for p in seeded:
            assert not p.exists()
        # Event was emitted
        rows = [e for e in events.recent(limit=10) if e.kind == "film_status_transition"]
        assert len(rows) == 1

    # On-disk manifest reflects the transition (persisted in __init__)
    reloaded = load_manifest(manifest_path)
    assert current_status(reloaded.manifest) == PENDING


def test_orchestrator_construction_noop_on_pending(tmp_path: Path) -> None:
    """Construction on a healthy pending manifest: recovery is a no-op, no
    event emitted, edit artifacts untouched."""
    seeded = _seed_edit_workspace(tmp_path, files=1)
    manifest = minimal_manifest()
    manifest["film_status"] = PENDING
    manifest_path = tmp_path / "state" / "manifest.json"
    save_manifest_atomic(manifest_path, manifest, last_event_id=0)

    load = load_manifest(manifest_path)
    with open_event_log(tmp_path / "state" / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=load.manifest,
            manifest_path=manifest_path,
            events=events,
            tools=ToolSet(
                render=lambda **_kw: {"status": "ok"},
                judge=lambda **_kw: {},
                revise=lambda **_kw: {},
            ),
            project_root=tmp_path,
        )
        assert orch._recovery_report.ran is False
        for p in seeded:
            assert p.exists()
