"""Unit tests for src/producer/manifest_io.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.producer.manifest_io import (
    ManifestValidationError,
    load_manifest,
    save_manifest_atomic,
    validate_manifest,
)
from tests.producer.conftest import minimal_manifest


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    m = minimal_manifest()
    save_manifest_atomic(path, m, last_event_id=42)
    loaded = load_manifest(path)
    assert loaded.was_dirty is False
    assert loaded.manifest["run_state"]["resumable"] is True
    assert loaded.manifest["run_state"]["last_event_id"] == 42


def test_save_marks_resumable_true_and_stamps_event_id(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    m = minimal_manifest()
    m["run_state"]["resumable"] = False  # simulate mid-transaction state
    m["run_state"]["last_event_id"] = 1
    save_manifest_atomic(path, m, last_event_id=99)
    on_disk = json.loads(path.read_text())
    assert on_disk["run_state"]["resumable"] is True
    assert on_disk["run_state"]["last_event_id"] == 99


def test_save_updates_updated_at(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    m = minimal_manifest()
    original = m["updated_at"]
    save_manifest_atomic(path, m, last_event_id=1)
    assert m["updated_at"] != original


def test_schema_validation_fails_loud(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    m = minimal_manifest()
    del m["brief"]["logline"]  # violates schema: required field
    with pytest.raises(ManifestValidationError) as excinfo:
        save_manifest_atomic(path, m, last_event_id=1)
    assert "logline" in str(excinfo.value)
    # Disk unchanged on failure
    assert not path.exists()


def test_save_is_atomic_no_partial_write_visible(tmp_path: Path) -> None:
    """Successful save leaves no tmpfile siblings behind."""
    path = tmp_path / "manifest.json"
    save_manifest_atomic(path, minimal_manifest(), last_event_id=1)
    tmp_siblings = [p for p in path.parent.iterdir() if p.name.startswith(".manifest_")]
    assert tmp_siblings == []


def test_save_failure_cleans_up_tmpfile(tmp_path: Path) -> None:
    """Schema validation failure does not leave a tmpfile behind."""
    path = tmp_path / "manifest.json"
    m = minimal_manifest()
    del m["brief"]["logline"]
    with pytest.raises(ManifestValidationError):
        save_manifest_atomic(path, m, last_event_id=1)
    tmp_siblings = [p for p in path.parent.iterdir() if p.name.startswith(".manifest_")]
    assert tmp_siblings == []


def test_load_detects_dirty_resumable_flag(tmp_path: Path) -> None:
    """A manifest with resumable=false on disk signals interrupted write."""
    path = tmp_path / "manifest.json"
    m = minimal_manifest()
    m["run_state"]["resumable"] = False
    path.write_text(json.dumps(m))
    loaded = load_manifest(path)
    assert loaded.was_dirty is True


def test_load_validates_on_read(tmp_path: Path) -> None:
    """A corrupted on-disk manifest is rejected at load time."""
    path = tmp_path / "manifest.json"
    m = minimal_manifest()
    m["manifest_version"] = "99.0"  # schema says const: "1.0"
    path.write_text(json.dumps(m))
    with pytest.raises(ManifestValidationError):
        load_manifest(path)


def test_validate_manifest_accepts_current_schema() -> None:
    """Baseline: minimal_manifest() is schema-valid (guards the conftest)."""
    validate_manifest(minimal_manifest())  # no raise


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "manifest.json"
    save_manifest_atomic(path, minimal_manifest(), last_event_id=1)
    assert path.exists()
