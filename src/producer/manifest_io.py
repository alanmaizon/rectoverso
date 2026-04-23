"""Atomic manifest I/O with schema validation and resumable-flag discipline.

`state/manifest.json` is the single source of truth for the current pipeline
projection. Per CLAUDE.md § The shot manifest, every write MUST:

    1. Validate against schemas/manifest.schema.json before write.
    2. Be atomic — readers must never see a torn file.
    3. Honor the resumable flag: false during the write, true when consistent.

This module enforces (1) and (2). Callers are responsible for the event-log
write that brackets each save (see src/producer/dispatch.py for the wrapper).

Edge cases:
- Schema validation failure → raises `ManifestValidationError`; disk unchanged.
- Filesystem write fails mid-rename → POSIX rename is atomic within a
  filesystem; a partial write on the tmpfile is cleaned up on next save.
- Resumable=false found at load time → returns the manifest with a flag the
  caller can inspect (`was_dirty`); reconciliation is out of scope here.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


_DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "manifest.schema.json"


class ManifestValidationError(ValueError):
    """Raised when a manifest fails schema validation on save.

    Carries the underlying ValidationError so the caller can log the path and
    message without re-running the validator.
    """

    def __init__(self, cause: ValidationError) -> None:
        self.cause = cause
        super().__init__(
            f"manifest validation failed at {list(cause.absolute_path)}: {cause.message}"
        )


@dataclass(frozen=True)
class LoadResult:
    """Outcome of loading the manifest.

    `was_dirty` is True when `run_state.resumable == false` on disk, meaning
    the last write may have been interrupted. Caller reconciles before
    accepting new work.
    """

    manifest: dict[str, Any]
    was_dirty: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def load_schema(schema_path: Path | None = None) -> dict[str, Any]:
    path = Path(schema_path) if schema_path else _DEFAULT_SCHEMA_PATH
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_manifest(
    manifest: Mapping[str, Any], schema: Mapping[str, Any] | None = None
) -> None:
    """Raise ManifestValidationError if the manifest doesn't match the schema."""
    s = schema or load_schema()
    validator = Draft202012Validator(s)
    errors = sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path))
    if errors:
        raise ManifestValidationError(errors[0])


def load_manifest(path: Path | str) -> LoadResult:
    """Read the manifest from disk. Returns a fresh dict; the on-disk file is
    not held open. Performs schema validation — on failure, raises
    ManifestValidationError to catch corruption early.

    Migrations (in-memory, not persisted on load):
        - `film_status` missing -> injected as "pending". Legacy manifests
          from before the compose state machine landed are transparently
          upgraded. The next atomic save writes it through.
    """
    p = Path(path)
    with open(p, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    validate_manifest(manifest)
    # Lazy import to avoid a circular reference (film_status imports EventLog
    # which imports from this package). Stdlib pattern; keeps the migration
    # lightweight.
    from .film_status import migrate as _migrate_film_status
    _migrate_film_status(manifest)
    was_dirty = not bool(manifest.get("run_state", {}).get("resumable", True))
    return LoadResult(manifest=manifest, was_dirty=was_dirty)


def save_manifest_atomic(
    path: Path | str,
    manifest: dict[str, Any],
    last_event_id: int,
    *,
    schema: Mapping[str, Any] | None = None,
) -> None:
    """Write the manifest atomically, after setting resumable=true, updating
    last_event_id, and bumping updated_at.

    Order of operations — chosen so that a crash at any point leaves the
    filesystem in a consistent state:

        1. Set run_state.resumable = true, last_event_id, updated_at on the
           in-memory dict. (The caller is responsible for having set
           resumable = false at the start of its transaction via the event
           log; the on-disk manifest from a successful prior save already
           reads resumable = true.)
        2. Validate the manifest against the schema. On failure, raise; disk
           is unchanged.
        3. Write to a sibling tmpfile in the target directory (same
           filesystem → atomic rename).
        4. fsync the tmpfile.
        5. os.replace(tmp, path) — atomic on POSIX.

    If the process crashes between (1) and (5), the on-disk manifest is from
    the PREVIOUS successful save — still consistent. The events.db entry the
    caller wrote as `dispatch_intent` is the recovery pointer.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Step 1 — mark consistent, stamp event id and updated_at.
    run_state = manifest.setdefault("run_state", {})
    run_state["resumable"] = True
    run_state["last_event_id"] = last_event_id
    manifest["updated_at"] = _now_iso()

    # Step 2 — validate before touching disk.
    validate_manifest(manifest, schema)

    # Steps 3–5 — write tmpfile, fsync, atomic rename.
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".manifest_", suffix=".json.tmp", dir=p.parent
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp:
            json.dump(manifest, tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, p)
    except Exception:
        # Best-effort cleanup; if we can't remove, a future save will overwrite.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
