"""Unit tests for src/producer/events.py — EventLog.

Scope:
- write returns a strictly-increasing event_id
- payload is JSON-round-trippable
- shot_id and ref_event_id are queryable
- recent() ordering is newest-first
- last_event_id() tracks the autoincrement tip
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.producer.events import EventLog, KINDS, open_event_log


def test_fresh_log_is_empty(tmp_path: Path) -> None:
    with open_event_log(tmp_path / "events.db") as log:
        assert log.count() == 0
        assert log.last_event_id() == 0
        assert log.recent() == []


def test_write_returns_monotonic_ids(tmp_path: Path) -> None:
    with open_event_log(tmp_path / "events.db") as log:
        a = log.write("dispatch_intent", agent="shot_judge", shot_id="sh_001")
        b = log.write("dispatch_result", agent="shot_judge", shot_id="sh_001", ref_event_id=a)
        c = log.write("dispatch_intent", agent="editor_agent")
        assert a < b < c


def test_payload_round_trips_through_json(tmp_path: Path) -> None:
    with open_event_log(tmp_path / "events.db") as log:
        payload = {
            "ctx": {"revision": True, "editor_priority": "high"},
            "nested": {"list": [1, 2, 3], "bool": False},
        }
        eid = log.write("dispatch_intent", agent="prompt_smith", payload=payload)
        fetched = log.get(eid)
        assert fetched is not None
        assert fetched.payload == payload


def test_get_returns_none_for_unknown_id(tmp_path: Path) -> None:
    with open_event_log(tmp_path / "events.db") as log:
        log.write("dispatch_intent")
        assert log.get(99999) is None


def test_for_shot_filters_and_orders(tmp_path: Path) -> None:
    with open_event_log(tmp_path / "events.db") as log:
        log.write("dispatch_intent", shot_id="sh_001")
        log.write("dispatch_intent", shot_id="sh_002")
        log.write("dispatch_result", shot_id="sh_001")
        log.write("dispatch_intent")  # film-level, no shot_id

        shot1 = log.for_shot("sh_001")
        assert len(shot1) == 2
        assert [e.kind for e in shot1] == ["dispatch_intent", "dispatch_result"]
        # Ascending event_id order (insertion order)
        assert shot1[0].event_id < shot1[1].event_id


def test_recent_is_newest_first(tmp_path: Path) -> None:
    with open_event_log(tmp_path / "events.db") as log:
        ids = [log.write("dispatch_intent") for _ in range(5)]
        recent = log.recent(limit=3)
        assert [e.event_id for e in recent] == list(reversed(ids[-3:]))


def test_last_event_id_tracks_tip(tmp_path: Path) -> None:
    with open_event_log(tmp_path / "events.db") as log:
        assert log.last_event_id() == 0
        a = log.write("dispatch_intent")
        assert log.last_event_id() == a
        b = log.write("dispatch_result")
        assert log.last_event_id() == b


def test_schema_survives_reopen(tmp_path: Path) -> None:
    """Event log is persistent — reopening after close preserves events."""
    db = tmp_path / "events.db"
    with open_event_log(db) as log:
        log.write("dispatch_intent", agent="shot_judge", shot_id="sh_001")
    with open_event_log(db) as log:
        assert log.count() == 1
        events = log.recent()
        assert events[0].agent == "shot_judge"


def test_canonical_kinds_documented() -> None:
    """Tripwire — if KINDS changes, docstring must too."""
    assert "dispatch_intent" in KINDS
    assert "dispatch_result" in KINDS
    assert "dispatch_failure" in KINDS
    assert "contract_block" in KINDS
    assert "contract_warn" in KINDS


def test_foreign_key_constraint_enforced(tmp_path: Path) -> None:
    """ref_event_id=99999 (nonexistent) should be rejected by FK pragma."""
    import sqlite3

    with open_event_log(tmp_path / "events.db") as log:
        with pytest.raises(sqlite3.IntegrityError):
            log.write("dispatch_result", ref_event_id=99999)
