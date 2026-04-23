"""FilmOrchestrator audio-phase tests.

Audio runs AFTER a shot's render+judge loop reaches approved. Failures in
the audio phase must NOT demote the shot to failed/escalated — `shot.status`
stays approved, and `shot.audio_status` carries the partial/failed signal
separately. This separation is the feature.

Coverage:
    - happy path: 2 cues, both ok → audio_status="ok", both projected
    - partial:    2 cues, 1 ok + 1 failed → audio_status="partial"
    - all failed: 2 cues, 0 ok → audio_status="failed"
    - skipped:    shot with zero cues → audio_status="skipped"
    - shot still approved despite audio failure (independence)
    - resume: approved shot with audio_status=pending fires the phase
    - audio_status terminal prevents re-dispatch (retry cap of 1)
    - TTS projects into audio.dialogue[] with required fields
    - SFX projects into audio.sfx[] with required fields
    - film-level audio rollup aggregates status breakdown + credits
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from src.producer import (
    FilmOrchestrator,
    ToolSet,
    open_event_log,
    save_manifest_atomic,
)
from tests.producer.conftest import add_minimal_shot, minimal_manifest


# ---------------------------------------------------------------------------
# Fixtures + stubs
# ---------------------------------------------------------------------------


def _render_stub_ok(manifest: dict, manifest_path: Path):
    """Render stub that approves every shot on attempt 1 AND writes the
    manifest to disk after mutating. The orchestrator reloads from disk
    after each tool call, so stubs must persist or they'll be clobbered
    once the audio phase starts writing disk state."""
    def _r(*, shot: dict, attempt_id: int, run_mode: str) -> dict:
        now_iso = "2026-04-23T20:00:00.000000Z"
        shot.setdefault("attempts", []).append({
            "attempt_id": attempt_id,
            "provider": "alibaba_wan_2_7_plus",
            "started_at": now_iso,
            "completed_at": now_iso,
            "outcome": "pending",
            "render_path": f"artifacts/renders/{shot['shot_id']}/v1.mp4",
            "cost_usd": 0.0,
        })
        shot["status"] = "judging"
        shot.setdefault("history", []).append({
            "ts": now_iso, "event": "rendering", "by": "renderer",
            "detail": f"attempt {attempt_id}",
        })
        save_manifest_atomic(
            manifest_path, manifest,
            last_event_id=manifest.get("run_state", {}).get("last_event_id", 0),
        )
        return {"status": "ok"}
    return _r


def _judge_stub_approves(manifest: dict, manifest_path: Path):
    def _j(*, shot: dict, attempt_id: int) -> dict:
        attempt = shot["attempts"][-1]
        attempt["outcome"] = "approved"
        attempt["judge_score"] = 0.85
        attempt["approved_by"] = "shot_judge"
        shot["status"] = "approved"
        shot["final"] = {
            "render_path": attempt["render_path"],
            "attempt_id": attempt_id,
        }
        save_manifest_atomic(
            manifest_path, manifest,
            last_event_id=manifest.get("run_state", {}).get("last_event_id", 0),
        )
        return {"outcome": "approved", "judge_score": 0.85}
    return _j


def _make_audio_stub(results_by_cue_index: list[dict[str, Any]]):
    """Return an audio fn that returns results_by_cue_index[i] for the i-th
    call. Records every call for assertions."""
    calls: list[dict[str, Any]] = []

    def _a(*, shot: dict, cue: dict, attempt_id: int) -> dict:
        idx = len(calls)
        calls.append({"shot_id": shot["shot_id"], "cue": dict(cue), "attempt_id": attempt_id})
        if idx >= len(results_by_cue_index):
            raise AssertionError(f"audio stub called {idx + 1} times; only {len(results_by_cue_index)} results queued")
        return dict(results_by_cue_index[idx])

    _a.calls = calls  # type: ignore[attr-defined]
    return _a


def _revise_stub_never(**_kw):
    raise AssertionError("revise should not fire in these tests (every shot approves first try)")


def _ok_sfx_result(shot_id: str, sfx_id: str, tmp_path: Path, *, credits: int = 80) -> dict:
    path = tmp_path / "audio" / f"{shot_id}_{sfx_id}_v1.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake_mp3_bytes")
    return {
        "status": "ok",
        "provider": "elevenlabs",
        "mode": "sfx",
        "shot_id": shot_id,
        "sfx_id": sfx_id,
        "description": f"{sfx_id} description",
        "audio_path": str(path),
        "audio_md5": "d41d8cd98f00b204e9800998ecf8427e",
        "output_size_bytes": 12,
        "duration_s": 2.0,
        "cost_usd": 0.0,
        "quota_cost": credits,
        "latency_s": 1.5,
    }


def _ok_tts_result(shot_id: str, line_id: str, text: str, voice_id: str, tmp_path: Path) -> dict:
    path = tmp_path / "audio" / f"{shot_id}_{line_id}_v1.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake_tts_mp3")
    return {
        "status": "ok",
        "provider": "elevenlabs",
        "mode": "tts",
        "shot_id": shot_id,
        "line_id": line_id,
        "text": text,
        "voice_id": voice_id,
        "model_id": "eleven_v3",
        "audio_path": str(path),
        "audio_md5": "deadbeef",
        "output_size_bytes": 12,
        "output_format": "mp3_44100_128",
        "duration_s": 1.2,
        "cost_usd": 0.0,
        "quota_cost": len(text),
        "latency_s": 0.8,
    }


def _failed_sfx_result(shot_id: str, sfx_id: str, stage: str = "content_policy") -> dict:
    return {
        "status": "failed",
        "failure_stage": stage,
        "provider": "elevenlabs",
        "mode": "sfx",
        "shot_id": shot_id,
        "sfx_id": sfx_id,
        "description": f"{sfx_id} description",
        "audio_path": "",
        "audio_md5": None,
        "output_size_bytes": 0,
        "cost_usd": 0.0,
        "quota_cost": 0,
        "latency_s": 0.5,
        "stderr_tail": f"simulated {stage}",
    }


def _build_shot_with_cues(manifest: dict, shot_id: str, cues: list[dict]) -> dict:
    shot = add_minimal_shot(manifest, shot_id)
    shot["status"] = "prompted"
    shot["routing"]["chosen_provider"] = "alibaba_wan_2_7_plus"
    shot["routing"]["chosen_model"] = "wan2.7-t2v"
    shot["audio_cues"] = cues
    shot["audio_status"] = "pending"
    return shot


# ---------------------------------------------------------------------------
# Happy + negative audio-phase paths
# ---------------------------------------------------------------------------


def test_audio_phase_two_cues_both_ok(tmp_path: Path) -> None:
    """Both cues succeed → shot.audio_status='ok', both projected into
    manifest.audio.sfx[], shot.status stays approved."""
    manifest = minimal_manifest()
    shot = _build_shot_with_cues(manifest, "sh_005", [
        {"mode": "sfx", "sfx_id": "ice_crack", "description": "ice crack", "duration_s": 2.0},
        {"mode": "sfx", "sfx_id": "fox_breath", "description": "fox breath", "duration_s": 2.5},
    ])
    audio = _make_audio_stub([
        _ok_sfx_result("sh_005", "ice_crack", tmp_path, credits=80),
        _ok_sfx_result("sh_005", "fox_breath", tmp_path, credits=100),
    ])

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, tmp_path / "manifest.json"),
                judge=_judge_stub_approves(manifest, tmp_path / "manifest.json"),
                revise=_revise_stub_never,
                audio=audio,
            ),
        )
        result = orch.run()

    assert len(audio.calls) == 2
    assert shot["status"] == "approved"        # unchanged by audio outcome
    assert shot["audio_status"] == "ok"
    # Both SFX cues projected into manifest.audio.sfx[]
    sfx_rows = manifest["audio"]["sfx"]
    assert len(sfx_rows) == 2
    assert {r["sfx_id"] for r in sfx_rows} == {"ice_crack", "fox_breath"}
    # All required schema fields present
    for r in sfx_rows:
        assert set(r.keys()) >= {"shot_id", "sfx_id", "description", "audio_path"}
    # Per-shot summary carries credits
    s = result.shots[0]
    assert s.audio_status == "ok"
    assert s.audio_cues_total == 2
    assert s.audio_cues_ok == 2
    assert s.audio_credits_used == 180
    # Film-level rollup
    assert result.total_audio_credits_used == 180
    assert result.audio_status_breakdown.get("ok") == 1


def test_audio_phase_partial_failure_shot_stays_approved(tmp_path: Path) -> None:
    """1 cue ok + 1 cue fails → audio_status='partial', shot.status
    still 'approved'. This is the core separation-of-concerns contract."""
    manifest = minimal_manifest()
    shot = _build_shot_with_cues(manifest, "sh_005", [
        {"mode": "sfx", "sfx_id": "ice_crack", "description": "ice crack", "duration_s": 2.0},
        {"mode": "sfx", "sfx_id": "fox_breath", "description": "fox breath", "duration_s": 2.5},
    ])
    audio = _make_audio_stub([
        _ok_sfx_result("sh_005", "ice_crack", tmp_path),
        _failed_sfx_result("sh_005", "fox_breath", "content_policy"),
    ])

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, tmp_path / "manifest.json"),
                judge=_judge_stub_approves(manifest, tmp_path / "manifest.json"),
                revise=_revise_stub_never,
                audio=audio,
            ),
        )
        result = orch.run()

    assert shot["status"] == "approved"         # KEY: audio failure doesn't demote
    assert shot["audio_status"] == "partial"
    # Only the successful cue projected
    assert len(manifest["audio"]["sfx"]) == 1
    assert manifest["audio"]["sfx"][0]["sfx_id"] == "ice_crack"
    # History records both outcomes
    events_list = [h["event"] for h in shot["history"]]
    assert "audio_cue_ok" in events_list
    assert "audio_cue_failed" in events_list
    s = result.shots[0]
    assert s.final_status == "approved"
    assert s.audio_status == "partial"
    assert s.audio_cues_ok == 1
    assert s.audio_cues_total == 2


def test_audio_phase_all_failed(tmp_path: Path) -> None:
    """Every cue fails → audio_status='failed'. Shot still approved."""
    manifest = minimal_manifest()
    shot = _build_shot_with_cues(manifest, "sh_005", [
        {"mode": "sfx", "sfx_id": "ice_crack", "description": "ice crack", "duration_s": 2.0},
    ])
    audio = _make_audio_stub([
        _failed_sfx_result("sh_005", "ice_crack", "quota_exhausted"),
    ])

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, tmp_path / "manifest.json"),
                judge=_judge_stub_approves(manifest, tmp_path / "manifest.json"),
                revise=_revise_stub_never,
                audio=audio,
            ),
        )
        result = orch.run()

    assert shot["status"] == "approved"
    assert shot["audio_status"] == "failed"
    assert manifest["audio"]["sfx"] == []
    assert result.shots[0].final_status == "approved"
    assert result.shots[0].audio_status == "failed"


def test_audio_phase_skipped_when_no_cues(tmp_path: Path) -> None:
    """A shot with no audio_cues gets audio_status='skipped' (distinct from
    'failed' — means no audio was requested for this shot)."""
    manifest = minimal_manifest()
    shot = _build_shot_with_cues(manifest, "sh_001", [])
    audio = _make_audio_stub([])  # never called

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, tmp_path / "manifest.json"),
                judge=_judge_stub_approves(manifest, tmp_path / "manifest.json"),
                revise=_revise_stub_never,
                audio=audio,
            ),
        )
        result = orch.run()

    assert len(audio.calls) == 0
    assert shot["audio_status"] == "skipped"
    assert result.shots[0].audio_status == "skipped"


def test_audio_phase_noop_when_tools_audio_is_none(tmp_path: Path) -> None:
    """If the ToolSet has no audio callable, the audio phase is skipped
    entirely and audio_status stays 'pending'."""
    manifest = minimal_manifest()
    shot = _build_shot_with_cues(manifest, "sh_001", [
        {"mode": "sfx", "sfx_id": "x", "description": "y", "duration_s": 1.0},
    ])

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, tmp_path / "manifest.json"),
                judge=_judge_stub_approves(manifest, tmp_path / "manifest.json"),
                revise=_revise_stub_never,
                audio=None,  # KEY: no audio tool
            ),
        )
        orch.run()

    assert shot["audio_status"] == "pending"
    assert manifest["audio"]["sfx"] == []


def test_audio_phase_not_rerun_when_already_terminal(tmp_path: Path) -> None:
    """Retry cap of 1 — if shot.audio_status is already ok/partial/failed,
    the orchestrator does NOT dispatch again on resume. Operator opts back
    in by manually resetting audio_status='pending'."""
    manifest = minimal_manifest()
    shot = _build_shot_with_cues(manifest, "sh_005", [
        {"mode": "sfx", "sfx_id": "ice_crack", "description": "ice crack", "duration_s": 2.0},
    ])
    # Pre-populate as already approved + already-audio-done.
    shot["status"] = "approved"
    shot["final"] = {
        "render_path": "artifacts/renders/sh_005/v1.mp4",
        "attempt_id": 1,
    }
    shot["attempts"] = [{
        "attempt_id": 1,
        "provider": "alibaba_wan_2_7_plus",
        "started_at": "2026-04-23T10:00:00.000000Z",
        "completed_at": "2026-04-23T10:01:00.000000Z",
        "outcome": "approved",
        "render_path": "artifacts/renders/sh_005/v1.mp4",
        "judge_score": 0.85,
        "approved_by": "shot_judge",
    }]
    shot["audio_status"] = "partial"   # KEY: terminal audio status
    audio = _make_audio_stub([])       # must not be called

    def _never_render(**_kw): raise AssertionError("render should not fire")
    def _never_judge(**_kw): raise AssertionError("judge should not fire")

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=ToolSet(
                render=_never_render,
                judge=_never_judge,
                revise=_revise_stub_never,
                audio=audio,
            ),
        )
        orch.run()

    assert len(audio.calls) == 0
    # State preserved unchanged
    assert shot["audio_status"] == "partial"


def test_audio_phase_fires_on_resume_when_audio_pending(tmp_path: Path) -> None:
    """Approved shot with audio_status='pending' picks up the audio phase
    on the next orchestrator pass. Resumability: operator edits audio_cues
    between runs, resumes, audio lands."""
    manifest = minimal_manifest()
    shot = _build_shot_with_cues(manifest, "sh_005", [
        {"mode": "sfx", "sfx_id": "ice_crack", "description": "ice", "duration_s": 2.0},
    ])
    shot["status"] = "approved"
    shot["final"] = {"render_path": "artifacts/renders/sh_005/v1.mp4", "attempt_id": 1}
    shot["attempts"] = [{
        "attempt_id": 1, "provider": "alibaba_wan_2_7_plus",
        "started_at": "2026-04-23T10:00:00.000000Z",
        "completed_at": "2026-04-23T10:01:00.000000Z",
        "outcome": "approved",
        "render_path": "artifacts/renders/sh_005/v1.mp4",
        "judge_score": 0.85, "approved_by": "shot_judge",
    }]
    # audio_status="pending" (set by _build_shot_with_cues) — not yet run
    audio = _make_audio_stub([
        _ok_sfx_result("sh_005", "ice_crack", tmp_path),
    ])

    def _never_render(**_kw): raise AssertionError("render should not fire on resume")

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=ToolSet(
                render=_never_render,
                judge=_judge_stub_approves(manifest, tmp_path / "manifest.json"),
                revise=_revise_stub_never,
                audio=audio,
            ),
        )
        orch.run()

    assert len(audio.calls) == 1
    assert shot["audio_status"] == "ok"


# ---------------------------------------------------------------------------
# Projection: manifest.audio.dialogue[] shape for TTS
# ---------------------------------------------------------------------------


def test_tts_cue_projects_all_required_dialogue_fields(tmp_path: Path) -> None:
    """audio.dialogue[] requires shot_id, line_id, text, voice_id,
    audio_path, duration_s, timing. All seven must be populated."""
    manifest = minimal_manifest()
    shot = _build_shot_with_cues(manifest, "sh_006", [
        {
            "mode": "tts",
            "line_id": "l1",
            "text": "Oh. It was here too.",
            "voice_id": "jBpfuIE2acCO8z3wKNLl",  # Gigi
        },
    ])
    audio = _make_audio_stub([
        _ok_tts_result("sh_006", "l1", "Oh. It was here too.",
                       "jBpfuIE2acCO8z3wKNLl", tmp_path),
    ])

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, tmp_path / "manifest.json"),
                judge=_judge_stub_approves(manifest, tmp_path / "manifest.json"),
                revise=_revise_stub_never,
                audio=audio,
            ),
        )
        orch.run()

    dialogue = manifest["audio"]["dialogue"]
    assert len(dialogue) == 1
    row = dialogue[0]
    for required in ("shot_id", "line_id", "text", "voice_id", "audio_path", "duration_s", "timing"):
        assert required in row, f"dialogue row missing {required}"
    assert row["timing"]["in_s"] == 0.0
    assert row["timing"]["out_s"] > 0
    assert row["duration_s"] == 1.2   # from stub result


# ---------------------------------------------------------------------------
# Film-level rollup
# ---------------------------------------------------------------------------


def test_film_level_audio_rollup_aggregates_status_and_credits(tmp_path: Path) -> None:
    """Three shots: one ok, one partial, one skipped. Film result rolls
    status breakdown + total credits."""
    manifest = minimal_manifest()
    s1 = _build_shot_with_cues(manifest, "sh_001", [
        {"mode": "sfx", "sfx_id": "a", "description": "a", "duration_s": 2.0},
    ])
    s2 = _build_shot_with_cues(manifest, "sh_002", [
        {"mode": "sfx", "sfx_id": "b", "description": "b", "duration_s": 2.0},
        {"mode": "sfx", "sfx_id": "c", "description": "c", "duration_s": 2.0},
    ])
    s3 = _build_shot_with_cues(manifest, "sh_003", [])  # no cues → skipped

    audio = _make_audio_stub([
        _ok_sfx_result("sh_001", "a", tmp_path, credits=80),
        _ok_sfx_result("sh_002", "b", tmp_path, credits=80),
        _failed_sfx_result("sh_002", "c", "content_policy"),
        # sh_003 has no cues, audio never called for it
    ])

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, tmp_path / "manifest.json"),
                judge=_judge_stub_approves(manifest, tmp_path / "manifest.json"),
                revise=_revise_stub_never,
                audio=audio,
            ),
        )
        result = orch.run()

    assert result.audio_status_breakdown == {"ok": 1, "partial": 1, "skipped": 1}
    # sh_001: 80 credits + sh_002: 80 credits (only ok cue counted) = 160
    assert result.total_audio_credits_used == 160
    assert len(audio.calls) == 3
