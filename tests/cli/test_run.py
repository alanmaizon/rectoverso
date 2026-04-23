"""End-to-end tests for `rectoverso run` — the Day-3 pipeline driver.

Intent:       brief.json → manifest.json fully prompted + fully routed, via
              the dry-run stub client (no network).
Architecture: drive main() with the real CLI parser; inspect the manifest
              + events.db artifacts to verify the full loop.
Edge cases:   missing brief, malformed brief, duration summary attached.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.producer import open_event_log
from src.rectoverso.cli import main


REPO_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = REPO_ROOT / "router" / "capabilities.yaml"


BRIEF_SAMPLE = {
    "project_id": "test_e2e",
    "logline": "A test film about testing.",
    "target_duration_s": 30.0,
    "tone": ["terse"],
    "genre": "drama",
    "artistic_style": "minimal, locked-off, natural light",
}


def _write_brief(path: Path, overrides: dict | None = None) -> Path:
    data = dict(BRIEF_SAMPLE)
    if overrides:
        data.update(overrides)
    path.write_text(json.dumps(data))
    return path


# -- happy path ---------------------------------------------------------


def test_dry_run_emits_prompted_manifest(tmp_path: Path, capsys) -> None:
    brief = _write_brief(tmp_path / "brief.json")
    manifest_path = tmp_path / "manifest.json"
    events_db = tmp_path / "events.db"

    code = main(
        [
            "run",
            str(brief),
            "--out",
            str(manifest_path),
            "--events-db",
            str(events_db),
            "--capabilities",
            str(CAPABILITIES),
            "--dry-run",
            "--json",
        ]
    )
    assert code == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["project_id"] == "proj_test_e2e"
    assert summary["shot_count"] == 8
    assert summary["within_duration_bound"] is True
    assert summary["hero_count"] >= 1
    assert sum(summary["providers"].values()) == 8

    m = json.loads(manifest_path.read_text())
    assert m["project_id"] == "proj_test_e2e"
    assert len(m["shots"]) == 8
    for s in m["shots"]:
        assert s["status"] == "prompted"
        assert s["prompt"]["authored_by"] == "prompt_smith"
        assert s["prompt"]["primary"]
        assert s["routing"]["chosen_provider"] != "pending"
        assert s["routing"]["decided_by"] == "router"
        # history should carry shot_created + routed + prompt_authored
        events = [h["event"] for h in s["history"]]
        assert "shot_created" in events
        assert "routed" in events
        assert "prompt_authored" in events


def test_events_db_captures_screenwriter_and_per_shot_prompt(
    tmp_path: Path, capsys
) -> None:
    brief = _write_brief(tmp_path / "brief.json")
    events_db = tmp_path / "events.db"

    main(
        [
            "run",
            str(brief),
            "--out",
            str(tmp_path / "manifest.json"),
            "--events-db",
            str(events_db),
            "--capabilities",
            str(CAPABILITIES),
            "--dry-run",
            "--json",
        ]
    )
    capsys.readouterr()  # discard stdout

    with open_event_log(events_db) as log:
        events = log.recent(limit=200)
    kinds = [e.kind for e in events]
    agents = {e.agent for e in events if e.agent}

    # Screenwriter once film-level, PromptSmith 8 times shot-level, router 8 times
    assert kinds.count("dispatch_result") == 9  # 1 screenwriter + 8 prompt_smith
    assert kinds.count("dispatch_intent") == 9
    assert kinds.count("router_decision") == 8
    assert agents == {"screenwriter", "prompt_smith", "router"}


def test_humans_and_heroes_route_correctly(tmp_path: Path, capsys) -> None:
    """The stub Screenwriter produces alternating humans (even orders) and
    heroes at orders 1/4/7. Verify the router honors humans-never-veo.
    --run-mode submission is needed because Veo is submission-tier only."""
    brief = _write_brief(tmp_path / "brief.json")
    main(
        [
            "run",
            str(brief),
            "--out",
            str(tmp_path / "manifest.json"),
            "--events-db",
            str(tmp_path / "events.db"),
            "--capabilities",
            str(CAPABILITIES),
            "--dry-run",
            "--run-mode", "submission",
            "--json",
        ]
    )
    capsys.readouterr()
    m = json.loads((tmp_path / "manifest.json").read_text())
    for s in m["shots"]:
        if s["has_humans"]:
            assert "veo" not in s["routing"]["chosen_provider"].lower()
        if s["is_hero"] and not s["has_humans"]:
            # heroes without humans should prefer the specialty tier
            assert s["routing"]["chosen_provider"] == "vertex_veo_3_1_fast"


# -- failure modes ------------------------------------------------------


def test_missing_brief_exits_2(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "run",
            str(tmp_path / "nope.json"),
            "--out",
            str(tmp_path / "manifest.json"),
            "--events-db",
            str(tmp_path / "events.db"),
            "--capabilities",
            str(CAPABILITIES),
            "--dry-run",
        ]
    )
    assert code == 2
    assert "not found" in capsys.readouterr().err


def test_malformed_brief_exits_3(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "brief.json"
    bad.write_text("not json {{{")
    code = main(
        [
            "run",
            str(bad),
            "--out",
            str(tmp_path / "manifest.json"),
            "--events-db",
            str(tmp_path / "events.db"),
            "--capabilities",
            str(CAPABILITIES),
            "--dry-run",
        ]
    )
    assert code == 3


def test_brief_missing_required_field_exits_3(tmp_path: Path, capsys) -> None:
    bad = _write_brief(tmp_path / "brief.json", overrides={"genre": None})
    # Remove genre entirely
    data = json.loads(bad.read_text())
    del data["genre"]
    bad.write_text(json.dumps(data))

    code = main(
        [
            "run",
            str(bad),
            "--out",
            str(tmp_path / "manifest.json"),
            "--events-db",
            str(tmp_path / "events.db"),
            "--capabilities",
            str(CAPABILITIES),
            "--dry-run",
        ]
    )
    assert code == 3
    assert "missing required fields" in capsys.readouterr().err
