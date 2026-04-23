"""SQLite-backed append-only event log at `state/events.db`.

Per CLAUDE.md § The shot manifest (keystone): events are truth; the manifest
is a projection. If the two disagree, SQLite wins.

Schema is deliberately minimal — the idea is that events.db captures enough
to reconstruct a manifest from scratch, but the reconstruction logic itself
lives elsewhere (out of scope for this skeleton).

    events(
      event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
      ts          TEXT NOT NULL,              -- ISO-8601 UTC
      kind        TEXT NOT NULL,              -- see KINDS below
      agent       TEXT,                       -- nullable; None for system events
      shot_id     TEXT,                       -- nullable; None for film-level events
      ref_event_id INTEGER,                   -- nullable; links result back to intent
      payload     TEXT NOT NULL               -- JSON string; schemaless
    )

KIND values are free-form strings; the skeleton uses a small set (see KINDS
below) but the table accepts any value so new event types don't require a
migration. This matches the scaling_managed_agents.md § Managed Agents ethos of keeping
interfaces stable while implementations evolve.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

# Canonical kinds this skeleton writes; callers are free to add their own.
KINDS = frozenset(
    {
        "dispatch_intent",
        "contract_block",
        "contract_warn",
        "dispatch_failure",
        "dispatch_result",
        "manifest_saved",
        "manifest_save_failed",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    agent        TEXT,
    shot_id      TEXT,
    ref_event_id INTEGER,
    payload      TEXT    NOT NULL,
    FOREIGN KEY(ref_event_id) REFERENCES events(event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_shot_id ON events(shot_id);
CREATE INDEX IF NOT EXISTS idx_events_ref ON events(ref_event_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class Event:
    event_id: int
    ts: str
    kind: str
    agent: str | None
    shot_id: str | None
    ref_event_id: int | None
    payload: Mapping[str, Any]


class EventLog:
    """Thin wrapper around a sqlite3 connection. Not thread-safe for writes.

    Use as a context manager (`with EventLog(path) as log:`) or manage the
    connection lifetime manually via `close()`.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- context manager sugar --------------------------------------------

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # -- writers ----------------------------------------------------------

    def write(
        self,
        kind: str,
        *,
        agent: str | None = None,
        shot_id: str | None = None,
        ref_event_id: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> int:
        """Append an event. Returns the new event_id."""
        ts = _now_iso()
        payload_json = json.dumps(dict(payload or {}), sort_keys=True, default=str)
        cur = self._conn.execute(
            "INSERT INTO events (ts, kind, agent, shot_id, ref_event_id, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, kind, agent, shot_id, ref_event_id, payload_json),
        )
        self._conn.commit()
        event_id = cur.lastrowid
        assert event_id is not None  # sqlite3 guarantees this for INSERT
        return event_id

    # -- readers ----------------------------------------------------------

    def get(self, event_id: int) -> Event | None:
        row = self._conn.execute(
            "SELECT event_id, ts, kind, agent, shot_id, ref_event_id, payload "
            "FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_event(row)

    def recent(self, limit: int = 50) -> list[Event]:
        rows = self._conn.execute(
            "SELECT event_id, ts, kind, agent, shot_id, ref_event_id, payload "
            "FROM events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def for_shot(self, shot_id: str) -> list[Event]:
        rows = self._conn.execute(
            "SELECT event_id, ts, kind, agent, shot_id, ref_event_id, payload "
            "FROM events WHERE shot_id = ? ORDER BY event_id ASC",
            (shot_id,),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def count(self) -> int:
        (n,) = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(n)

    def last_event_id(self) -> int:
        row = self._conn.execute(
            "SELECT event_id FROM events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else 0


def _row_to_event(row: tuple[Any, ...]) -> Event:
    event_id, ts, kind, agent, shot_id, ref_event_id, payload = row
    return Event(
        event_id=event_id,
        ts=ts,
        kind=kind,
        agent=agent,
        shot_id=shot_id,
        ref_event_id=ref_event_id,
        payload=json.loads(payload),
    )


@contextmanager
def open_event_log(db_path: Path | str) -> Iterator[EventLog]:
    """Convenience context manager for tests and one-shot scripts."""
    log = EventLog(db_path)
    try:
        yield log
    finally:
        log.close()
