"""CLI-boundary tests for `rectoverso judge`.

Hermetic: the ShotJudgeTool instantiation inside judge_cmd is replaced with a
fake-client-backed tool that returns canned JSON. No network, no ffmpeg, no
real MP4 needed beyond a non-empty file on disk.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from src.rectoverso.cli import main
from tests.producer.conftest import minimal_manifest, add_minimal_shot


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_manifest(path: Path, manifest: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest))
    return path


def _fake_mp4(path: Path) -> Path:
    """Write a tiny placeholder MP4 — content doesn't matter because we stub
    the Tool before any ffmpeg call would happen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x20ftypisom" + bytes(1024))
    return path


def _manifest_with_rendered_shot(tmp_path: Path) -> tuple[Path, Path]:
    """Manifest with one shot in status=judging with a valid render_path.

    Schema requires relative paths (^(?!/|~)[^\\0]+$). Even if tmp_path is
    outside cwd, os.path.relpath produces a valid relative path (possibly with
    ../ prefixes) that cmd_judge then resolves back against cwd.
    """
    m = minimal_manifest()
    shot = add_minimal_shot(m, "sh_001")
    render_path = _fake_mp4(tmp_path / "artifacts/renders/sh_001/v1.mp4")
    rel = os.path.relpath(str(render_path.resolve()), start=str(Path.cwd()))
    shot["status"] = "judging"
    shot["attempts"] = [
        {
            "attempt_id": 1,
            "provider": "alibaba_wan_2_7_plus",
            "started_at": "2026-04-23T10:00:00.000000Z",
            "outcome": "pending",
            "render_path": rel,
        }
    ]
    manifest_path = _write_manifest(tmp_path / "state/manifest.json", m)
    return manifest_path, render_path


# ---------------------------------------------------------------------------
# Inject a fake ShotJudgeTool at the callsite
# ---------------------------------------------------------------------------


class _FakeJudgeTool:
    name = "shot_judge"

    def __init__(self, *, result: dict, **_: Any) -> None:
        self._result = result

    def __call__(self, shot_id, payload):
        # Return whatever the test specified
        return dict(self._result)


def _patch_judge_tool(monkeypatch: pytest.MonkeyPatch, result: dict) -> None:
    """Swap ShotJudgeTool inside judge_cmd with a fake returning `result`."""

    def _factory(*args, **kwargs):
        return _FakeJudgeTool(result=result)

    monkeypatch.setattr("src.rectoverso.judge_cmd.ShotJudgeTool", _factory)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def _approved_result() -> dict:
    return {
        "judge_score": 0.82,
        "composition": 0.80,
        "prompt_adherence": 0.85,
        "continuity": 0.81,
        "artifact_flag": False,
        "judge_notes": "Clean composition; subject well-placed.",
        "feedback": [
            {"feedback_type": "composition", "severity": "note", "observation": "rule of thirds hit"}
        ],
        "outcome": "approved",
        "rejection_reason": None,
        "mode": "vision",
        "model": "claude-opus-4-7",
        "usage": {"input_tokens": 1000, "output_tokens": 120, "cache_read_input_tokens": 5000},
    }


def _rejected_result() -> dict:
    return {
        "judge_score": 0.60,
        "composition": 0.55,
        "prompt_adherence": 0.60,
        "continuity": 0.65,
        "artifact_flag": False,
        "judge_notes": "Subject over-centered; reads flat.",
        "feedback": [],
        "outcome": "rejected",
        "rejection_reason": "auto_judge",
        "mode": "vision",
        "model": "claude-opus-4-7",
        "usage": {"input_tokens": 1000, "output_tokens": 80},
    }


def test_judge_approved_projects_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path, _ = _manifest_with_rendered_shot(tmp_path)
    _patch_judge_tool(monkeypatch, _approved_result())

    code = main(
        [
            "judge",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            "--json",
            str(manifest_path),
        ]
    )
    assert code == 0

    m = json.loads(manifest_path.read_text())
    shot = m["shots"][0]
    assert shot["status"] == "approved"
    assert shot["final"]["attempt_id"] == 1
    attempt = shot["attempts"][0]
    assert attempt["outcome"] == "approved"
    assert attempt["approved_by"] == "shot_judge"
    assert attempt["judge_score"] == 0.82
    assert "Clean composition" in attempt["judge_notes"]
    # Feedback projected into shot.judge_feedback[]
    assert shot["judge_feedback"]
    assert shot["judge_feedback"][0]["from_agent"] == "shot_judge"
    # History entry added
    assert any(h["event"] == "judged_approved" for h in shot["history"])


def test_judge_rejected_sets_rejection_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path, _ = _manifest_with_rendered_shot(tmp_path)
    _patch_judge_tool(monkeypatch, _rejected_result())

    code = main(
        [
            "judge",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            "--json",
            str(manifest_path),
        ]
    )
    assert code == 0

    m = json.loads(manifest_path.read_text())
    shot = m["shots"][0]
    assert shot["status"] == "rejected"
    assert "final" not in shot
    attempt = shot["attempts"][0]
    assert attempt["outcome"] == "rejected"
    assert attempt["rejection_reason"] == "auto_judge"
    assert attempt["judge_notes"]


def test_judge_escalated_sets_rejected_outcome_at_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """outcome=escalated → shot.status=escalated, but attempt.outcome='rejected'
    so the schema's rejected-requires-rejection_reason rule is satisfied."""
    manifest_path, _ = _manifest_with_rendered_shot(tmp_path)
    result = _rejected_result()
    result.update({"outcome": "escalated", "judge_score": 0.25})
    _patch_judge_tool(monkeypatch, result)

    code = main(
        [
            "judge",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            "--json",
            str(manifest_path),
        ]
    )
    assert code == 0

    m = json.loads(manifest_path.read_text())
    shot = m["shots"][0]
    assert shot["status"] == "escalated"
    assert shot["attempts"][0]["outcome"] == "rejected"  # schema-compat
    assert shot["attempts"][0]["rejection_reason"]


def test_judge_writes_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.producer import open_event_log

    manifest_path, _ = _manifest_with_rendered_shot(tmp_path)
    _patch_judge_tool(monkeypatch, _approved_result())
    events_db = tmp_path / "state/events.db"

    main(
        [
            "judge",
            "--shot", "sh_001",
            "--events-db", str(events_db),
            "--json",
            str(manifest_path),
        ]
    )

    with open_event_log(events_db) as log:
        events = log.for_shot("sh_001")
    kinds = [e.kind for e in events]
    assert "dispatch_intent" in kinds
    assert "dispatch_result" in kinds
    # Confirm the agent tag
    assert all(e.agent == "shot_judge" for e in events if e.kind in ("dispatch_intent", "dispatch_result"))


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


def test_judge_missing_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_judge_tool(monkeypatch, _approved_result())
    code = main(
        [
            "judge",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "events.db"),
            str(tmp_path / "nope.json"),
        ]
    )
    assert code == 2


def test_judge_shot_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path, _ = _manifest_with_rendered_shot(tmp_path)
    _patch_judge_tool(monkeypatch, _approved_result())
    code = main(
        [
            "judge",
            "--shot", "sh_999",
            "--events-db", str(tmp_path / "state/events.db"),
            str(manifest_path),
        ]
    )
    assert code == 2


def test_judge_refuses_non_judging_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = minimal_manifest()
    add_minimal_shot(m, "sh_001")  # status=created
    manifest_path = _write_manifest(tmp_path / "state/manifest.json", m)
    _patch_judge_tool(monkeypatch, _approved_result())
    code = main(
        [
            "judge",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            str(manifest_path),
        ]
    )
    assert code == 9


def test_judge_missing_render_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = minimal_manifest()
    shot = add_minimal_shot(m, "sh_001")
    shot["status"] = "judging"
    shot["attempts"] = [
        {
            "attempt_id": 1,
            "provider": "x",
            "started_at": "2026-04-23T10:00:00.000000Z",
            "outcome": "pending",
            "render_path": "does/not/exist.mp4",
        }
    ]
    manifest_path = _write_manifest(tmp_path / "state/manifest.json", m)
    _patch_judge_tool(monkeypatch, _approved_result())
    code = main(
        [
            "judge",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            str(manifest_path),
        ]
    )
    assert code == 2
