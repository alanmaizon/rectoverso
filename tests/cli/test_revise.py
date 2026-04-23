"""CLI-boundary tests for `rectoverso revise`.

Hermetic: the PromptSmithTool instantiation inside revise_cmd is replaced with
a fake returning canned JSON. No network, no API key needed.
"""

from __future__ import annotations

import json
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


def _manifest_with_rejected_shot(
    tmp_path: Path,
    *,
    judge_notes: str = "Subject over-centered; reads flat. Rework framing.",
    rejection_reason: str = "auto_judge",
) -> Path:
    """Manifest with one shot in status=rejected + last attempt rejected+judge_notes."""
    m = minimal_manifest()
    shot = add_minimal_shot(m, "sh_001")
    shot["status"] = "rejected"
    # A realistic prompt — revise-cmd reads this to stamp prompt_revision.
    shot["prompt"] = {
        "authored_by": "prompt_smith",
        "primary": "Wide shot of a lone figure on a windswept cliff at golden hour.",
    }
    shot["routing"] = {
        "chosen_provider": "alibaba_wan_2_7_plus",
        "chosen_model": "wan2.6-t2v",
        "rationale": "non-hero non-human",
        "decided_by": "router",
        "decided_at": "2026-04-23T10:00:00.000000Z",
        "alternates": [],
    }
    shot["attempts"] = [
        {
            "attempt_id": 1,
            "provider": "alibaba_wan_2_7_plus",
            "started_at": "2026-04-23T10:00:00.000000Z",
            "completed_at": "2026-04-23T10:01:00.000000Z",
            "outcome": "rejected",
            "rejection_reason": rejection_reason,
            "judge_notes": judge_notes,
            "judge_score": 0.55,
            "render_path": "artifacts/renders/sh_001/v1.mp4",
        }
    ]
    return _write_manifest(tmp_path / "state/manifest.json", m)


# ---------------------------------------------------------------------------
# Fake PromptSmithTool
# ---------------------------------------------------------------------------


class _FakePromptSmithTool:
    name = "prompt_smith"

    def __init__(self, *, result: dict, **_: Any) -> None:
        self._result = result

    def __call__(self, shot_id, payload):
        # Capture the payload so tests can assert revision context was passed.
        self.last_payload = dict(payload)
        self.last_shot_id = shot_id
        return dict(self._result)


def _patch_prompt_smith(monkeypatch: pytest.MonkeyPatch, result: dict) -> list:
    """Swap PromptSmithTool inside revise_cmd with a fake returning `result`.
    Returns a list holding the single fake instance for payload assertions."""
    captured: list = []

    def _factory(*args, **kwargs):
        fake = _FakePromptSmithTool(result=result)
        captured.append(fake)
        return fake

    monkeypatch.setattr("src.rectoverso.revise_cmd.PromptSmithTool", _factory)
    return captured


def _revision_result() -> dict:
    return {
        "primary": (
            "Medium shot of a lone figure on a windswept cliff, cool blue hour lighting, "
            "rule-of-thirds composition with subject at left third."
        ),
        "negative": "centered framing, warm color palette",
        "reference_image_paths": [],
        "model": "claude-opus-4-7",
        "usage": {"input_tokens": 800, "output_tokens": 140},
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_revise_projects_new_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _manifest_with_rejected_shot(tmp_path)
    captured = _patch_prompt_smith(monkeypatch, _revision_result())

    code = main(
        [
            "revise",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            "--json",
            str(manifest_path),
        ]
    )
    assert code == 0

    m = json.loads(manifest_path.read_text())
    shot = m["shots"][0]
    # New prompt projected, authored_by stamped to prompt_smith
    assert shot["prompt"]["authored_by"] == "prompt_smith"
    assert "Medium shot" in shot["prompt"]["primary"]
    assert shot["prompt"]["negative"] == "centered framing, warm color palette"
    # Status stays rejected (render_cmd accepts rejected as retry state)
    assert shot["status"] == "rejected"
    # History entry added
    assert any(h["event"] == "prompt_revised" for h in shot["history"])


def test_revise_passes_revision_flag_and_prior_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_with_rejected_shot(tmp_path)
    captured = _patch_prompt_smith(monkeypatch, _revision_result())

    main(
        [
            "revise",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            "--json",
            str(manifest_path),
        ]
    )

    # The fake captured exactly one call — assert the context was right.
    assert len(captured) == 1
    fake = captured[0]
    assert fake.last_shot_id == "sh_001"
    assert fake.last_payload["revision"] is True
    assert fake.last_payload["creative_driven"] is False
    # Brief + routing passed through
    assert fake.last_payload["brief"]["genre"] == "drama"
    assert fake.last_payload["routing"]["chosen_provider"] == "alibaba_wan_2_7_plus"
    # The shot passed in should carry the stamped prompt_revision on its
    # last attempt so PromptSmith's user payload can include "prior primary".
    last_attempt = fake.last_payload["shot"]["attempts"][-1]
    assert last_attempt["prompt_revision"].startswith("Wide shot")


def test_revise_stamps_prompt_revision_on_last_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_with_rejected_shot(tmp_path)
    _patch_prompt_smith(monkeypatch, _revision_result())

    main(
        [
            "revise",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            "--json",
            str(manifest_path),
        ]
    )

    m = json.loads(manifest_path.read_text())
    shot = m["shots"][0]
    # The original prompt got copied into attempts[-1].prompt_revision for
    # provenance — even after the shot.prompt was overwritten.
    assert shot["attempts"][0]["prompt_revision"].startswith("Wide shot")
    assert shot["prompt"]["primary"].startswith("Medium shot")


def test_revise_preserves_existing_prompt_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If prompt_revision is already set on the last attempt, don't clobber it."""
    m = minimal_manifest()
    shot = add_minimal_shot(m, "sh_001")
    shot["status"] = "rejected"
    shot["prompt"] = {"authored_by": "prompt_smith", "primary": "current prompt"}
    shot["routing"]["chosen_provider"] = "alibaba_wan_2_7_plus"
    shot["routing"]["chosen_model"] = "wan2.6-t2v"
    shot["attempts"] = [
        {
            "attempt_id": 1,
            "provider": "alibaba_wan_2_7_plus",
            "started_at": "2026-04-23T10:00:00.000000Z",
            "completed_at": "2026-04-23T10:01:00.000000Z",
            "outcome": "rejected",
            "rejection_reason": "auto_judge",
            "judge_notes": "some useful notes",
            "prompt_revision": "prior stamp that must not be overwritten",
        }
    ]
    manifest_path = _write_manifest(tmp_path / "state/manifest.json", m)
    _patch_prompt_smith(monkeypatch, _revision_result())

    main(
        [
            "revise",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            "--json",
            str(manifest_path),
        ]
    )

    saved = json.loads(manifest_path.read_text())
    assert saved["shots"][0]["attempts"][0]["prompt_revision"] == "prior stamp that must not be overwritten"


def test_revise_writes_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.producer import open_event_log

    manifest_path = _manifest_with_rejected_shot(tmp_path)
    _patch_prompt_smith(monkeypatch, _revision_result())
    events_db = tmp_path / "state/events.db"

    main(
        [
            "revise",
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
    assert all(
        e.agent == "prompt_smith"
        for e in events
        if e.kind in ("dispatch_intent", "dispatch_result")
    )


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


def test_revise_missing_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_prompt_smith(monkeypatch, _revision_result())
    code = main(
        [
            "revise",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "events.db"),
            str(tmp_path / "nope.json"),
        ]
    )
    assert code == 2


def test_revise_shot_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _manifest_with_rejected_shot(tmp_path)
    _patch_prompt_smith(monkeypatch, _revision_result())
    code = main(
        [
            "revise",
            "--shot", "sh_999",
            "--events-db", str(tmp_path / "state/events.db"),
            str(manifest_path),
        ]
    )
    assert code == 2


def test_revise_refuses_non_rejected_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = minimal_manifest()
    add_minimal_shot(m, "sh_001")  # status=created
    manifest_path = _write_manifest(tmp_path / "state/manifest.json", m)
    _patch_prompt_smith(monkeypatch, _revision_result())
    code = main(
        [
            "revise",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            str(manifest_path),
        ]
    )
    assert code == 9


def test_revise_refuses_missing_judge_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_with_rejected_shot(tmp_path, judge_notes="")
    _patch_prompt_smith(monkeypatch, _revision_result())
    code = main(
        [
            "revise",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            str(manifest_path),
        ]
    )
    assert code == 9


def test_revise_contract_block_when_last_attempt_not_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shot status is 'rejected' but a malformed attempt has outcome=pending —
    revise_cmd's explicit check catches this before contracts run."""
    m = minimal_manifest()
    shot = add_minimal_shot(m, "sh_001")
    shot["status"] = "rejected"
    shot["attempts"] = [
        {
            "attempt_id": 1,
            "provider": "alibaba_wan_2_7_plus",
            "started_at": "2026-04-23T10:00:00.000000Z",
            "outcome": "pending",
        }
    ]
    manifest_path = _write_manifest(tmp_path / "state/manifest.json", m)
    _patch_prompt_smith(monkeypatch, _revision_result())
    code = main(
        [
            "revise",
            "--shot", "sh_001",
            "--events-db", str(tmp_path / "state/events.db"),
            str(manifest_path),
        ]
    )
    assert code == 9
