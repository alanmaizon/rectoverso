"""FilmOrchestrator normalize-phase tests.

The normalize pre-pass runs AFTER a shot lands approved and BEFORE the
audio phase. It homogenizes the render's codec/resolution/fps/profile so
the downstream Editor's Hyperframes composition sees identical inputs.

Coverage parallels the audio suite since the design is intentionally
symmetric: independent phase, at-most-once per shot per run, failures
do NOT demote the shot.

    - happy path: approved shot -> normalize fires -> final.normalized_*
      populated, shot.status stays approved
    - no-op when ToolSet.normalize is None (backwards-compat with pre-wiring)
    - resume: approved shot with no normalized_path gets backfilled
    - resume guard: approved shot with normalized_path already set is NOT
      re-normalized (at-most-once, respects operator-set values)
    - failure keeps the shot approved + leaves normalized_* absent + adds
      history row
    - dispatch exception (tool raises) keeps shot approved
    - ShotSummary surfaces normalized_render_path
    - audio phase still runs when normalize fires first
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.producer import (
    FilmOrchestrator,
    ToolSet,
    open_event_log,
    save_manifest_atomic,
)
from tests.producer.conftest import add_minimal_shot, minimal_manifest


# ---------------------------------------------------------------------------
# Stubs — match the audio suite pattern (tool fns also persist to disk so
# the orchestrator's _reload_shot_from_disk doesn't clobber state).
# ---------------------------------------------------------------------------


def _render_stub_ok(manifest: dict, manifest_path: Path):
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


def _revise_stub_never(**_kw):
    raise AssertionError("revise should not fire (every shot approves first try)")


def _ok_normalize_result(shot_id: str, attempt_id: int, tmp_path: Path) -> dict:
    out = tmp_path / "renders" / shot_id / f"{shot_id}_norm_v{attempt_id}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"\x00" * 2048)
    return {
        "status": "ok",
        "provider": "ffmpeg_normalize",
        "mode": "normalize",
        "shot_id": shot_id,
        "src_path": f"artifacts/renders/{shot_id}/v{attempt_id}.mp4",
        "src_md5": "a" * 32,
        "output_path": str(out),
        "output_md5": "0123456789abcdef0123456789abcdef",
        "output_size_bytes": 2048,
        "duration_s": 5.0,
        "target_spec": {"width": 1280, "height": 720, "fps": 24},
        "ffmpeg_command": ["/fake/ffmpeg", "-i", "src.mp4"],
        "cost_usd": 0.0,
        "quota_cost": 0,
        "latency_s": 3.0,
        "stdout_tail": "",
        "stderr_tail": "",
    }


def _failed_normalize_result(shot_id: str, stage: str = "encode_failed") -> dict:
    return {
        "status": "failed",
        "failure_stage": stage,
        "provider": "ffmpeg_normalize",
        "mode": "normalize",
        "shot_id": shot_id,
        "src_path": f"artifacts/renders/{shot_id}/v1.mp4",
        "src_md5": "a" * 32,
        "output_path": "",
        "output_md5": None,
        "output_size_bytes": 0,
        "cost_usd": 0.0,
        "quota_cost": 0,
        "latency_s": 0.4,
        "stdout_tail": "",
        "stderr_tail": f"simulated {stage}",
    }


def _make_normalize_stub(results: list[dict[str, Any]] | list[Any]):
    """Return a normalize fn that plays `results` back in order. Each entry
    is either a result dict OR an Exception instance to raise on that call."""
    calls: list[dict[str, Any]] = []

    def _n(*, shot: dict, attempt_id: int) -> dict:
        idx = len(calls)
        calls.append({"shot_id": shot["shot_id"], "attempt_id": attempt_id})
        if idx >= len(results):
            raise AssertionError(
                f"normalize stub called {idx + 1} times; only {len(results)} results queued"
            )
        r = results[idx]
        if isinstance(r, Exception):
            raise r
        return dict(r)

    _n.calls = calls  # type: ignore[attr-defined]
    return _n


def _build_ready_shot(manifest: dict, shot_id: str) -> dict:
    shot = add_minimal_shot(manifest, shot_id)
    shot["status"] = "prompted"
    shot["routing"]["chosen_provider"] = "alibaba_wan_2_7_plus"
    shot["routing"]["chosen_model"] = "wan2.7-t2v"
    return shot


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_normalize_fires_after_approval_and_populates_final(tmp_path: Path) -> None:
    manifest = minimal_manifest()
    shot = _build_ready_shot(manifest, "sh_001")
    normalize = _make_normalize_stub([_ok_normalize_result("sh_001", 1, tmp_path)])
    manifest_path = tmp_path / "manifest.json"

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=manifest_path,
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, manifest_path),
                judge=_judge_stub_approves(manifest, manifest_path),
                revise=_revise_stub_never,
                normalize=normalize,
            ),
        )
        result = orch.run()

    assert len(normalize.calls) == 1
    assert normalize.calls[0]["shot_id"] == "sh_001"
    assert normalize.calls[0]["attempt_id"] == 1
    assert shot["status"] == "approved"
    # final.normalized_path + normalized_md5 populated
    final = shot["final"]
    assert final["normalized_path"].endswith("sh_001_norm_v1.mp4")
    assert final["normalized_md5"] == "0123456789abcdef0123456789abcdef"
    # History records the success
    events_list = [h["event"] for h in shot["history"]]
    assert "normalized" in events_list
    # Summary surfaces the normalized path
    s = result.shots[0]
    assert s.final_status == "approved"
    assert s.normalized_render_path is not None
    assert s.normalized_render_path.endswith("sh_001_norm_v1.mp4")


# ---------------------------------------------------------------------------
# Opt-out: no tool wired
# ---------------------------------------------------------------------------


def test_no_normalize_tool_leaves_final_untouched(tmp_path: Path) -> None:
    """Backwards-compat: ToolSet.normalize=None → shot.final has only
    render_path + attempt_id (no normalized_* fields)."""
    manifest = minimal_manifest()
    shot = _build_ready_shot(manifest, "sh_001")
    manifest_path = tmp_path / "manifest.json"

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=manifest_path,
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, manifest_path),
                judge=_judge_stub_approves(manifest, manifest_path),
                revise=_revise_stub_never,
                # normalize deliberately omitted
            ),
        )
        result = orch.run()

    assert shot["status"] == "approved"
    assert "normalized_path" not in shot["final"]
    assert "normalized_md5" not in shot["final"]
    assert result.shots[0].normalized_render_path is None


# ---------------------------------------------------------------------------
# Resume semantics
# ---------------------------------------------------------------------------


def test_resume_backfills_normalize_on_already_approved_shot(tmp_path: Path) -> None:
    """A manifest with an approved shot but no normalized_path should get
    normalized on resume (operator can reset by unsetting normalized_path)."""
    manifest = minimal_manifest()
    shot = _build_ready_shot(manifest, "sh_001")
    # Pre-set shot to the approved terminal state — simulates resume on a
    # manifest produced before the normalize pre-pass was wired.
    shot["status"] = "approved"
    shot["attempts"] = [{
        "attempt_id": 1,
        "provider": "alibaba_wan_2_7_plus",
        "started_at": "2026-04-23T19:00:00.000000Z",
        "completed_at": "2026-04-23T19:00:10.000000Z",
        "outcome": "approved",
        "render_path": "artifacts/renders/sh_001/v1.mp4",
        "cost_usd": 0.0,
        "judge_score": 0.85,
        "approved_by": "shot_judge",
    }]
    shot["final"] = {
        "render_path": "artifacts/renders/sh_001/v1.mp4",
        "attempt_id": 1,
    }
    normalize = _make_normalize_stub([_ok_normalize_result("sh_001", 1, tmp_path)])
    manifest_path = tmp_path / "manifest.json"

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=manifest_path,
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, manifest_path),
                judge=_judge_stub_approves(manifest, manifest_path),
                revise=_revise_stub_never,
                normalize=normalize,
            ),
        )
        orch.run()

    assert len(normalize.calls) == 1
    assert shot["final"]["normalized_path"].endswith("sh_001_norm_v1.mp4")


def test_resume_does_not_renormalize_already_normalized_shot(tmp_path: Path) -> None:
    """Approved shot with final.normalized_path already set → normalize
    tool is NOT invoked (at-most-once, respect operator-set values)."""
    manifest = minimal_manifest()
    shot = _build_ready_shot(manifest, "sh_001")
    shot["status"] = "approved"
    shot["attempts"] = [{
        "attempt_id": 1,
        "provider": "alibaba_wan_2_7_plus",
        "started_at": "2026-04-23T19:00:00.000000Z",
        "completed_at": "2026-04-23T19:00:10.000000Z",
        "outcome": "approved",
        "render_path": "artifacts/renders/sh_001/v1.mp4",
        "cost_usd": 0.0,
        "judge_score": 0.85,
        "approved_by": "shot_judge",
    }]
    shot["final"] = {
        "render_path": "artifacts/renders/sh_001/v1.mp4",
        "attempt_id": 1,
        "normalized_path": "artifacts/renders/sh_001/sh_001_norm_v1.mp4",
        "normalized_md5": "cafebabecafebabecafebabecafebabe",
    }
    normalize = _make_normalize_stub([])   # any call would error
    manifest_path = tmp_path / "manifest.json"

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=manifest_path,
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, manifest_path),
                judge=_judge_stub_approves(manifest, manifest_path),
                revise=_revise_stub_never,
                normalize=normalize,
            ),
        )
        result = orch.run()

    assert len(normalize.calls) == 0
    # Operator-set values preserved
    assert shot["final"]["normalized_md5"] == "cafebabecafebabecafebabecafebabe"
    assert result.shots[0].normalized_render_path.endswith("sh_001_norm_v1.mp4")


# ---------------------------------------------------------------------------
# Failure semantics (the core separation-of-concerns contract)
# ---------------------------------------------------------------------------


def test_normalize_failure_keeps_shot_approved(tmp_path: Path) -> None:
    """Normalize returning status=failed must NOT demote the shot. The
    shot stays approved; final.normalized_* stays absent; a history row
    records the failure. Editor is expected to fall back to render_path."""
    manifest = minimal_manifest()
    shot = _build_ready_shot(manifest, "sh_001")
    normalize = _make_normalize_stub([_failed_normalize_result("sh_001", "encode_failed")])
    manifest_path = tmp_path / "manifest.json"

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=manifest_path,
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, manifest_path),
                judge=_judge_stub_approves(manifest, manifest_path),
                revise=_revise_stub_never,
                normalize=normalize,
            ),
        )
        result = orch.run()

    assert shot["status"] == "approved"
    assert "normalized_path" not in shot["final"]
    assert "normalized_md5" not in shot["final"]
    events_list = [h["event"] for h in shot["history"]]
    assert "normalize_failed" in events_list
    s = result.shots[0]
    assert s.final_status == "approved"
    assert s.normalized_render_path is None


def test_normalize_dispatch_exception_keeps_shot_approved(tmp_path: Path) -> None:
    """A bare exception from the tool (missing ffmpeg, bad payload) is
    caught; shot stays approved; history records normalize_dispatch_error."""
    manifest = minimal_manifest()
    shot = _build_ready_shot(manifest, "sh_001")
    normalize = _make_normalize_stub([RuntimeError("ffprobe segfaulted")])
    manifest_path = tmp_path / "manifest.json"

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=manifest_path,
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, manifest_path),
                judge=_judge_stub_approves(manifest, manifest_path),
                revise=_revise_stub_never,
                normalize=normalize,
            ),
        )
        orch.run()

    assert shot["status"] == "approved"
    assert "normalized_path" not in shot["final"]
    events_list = [h["event"] for h in shot["history"]]
    assert "normalize_dispatch_error" in events_list


# ---------------------------------------------------------------------------
# Ordering with audio
# ---------------------------------------------------------------------------


def test_normalize_logs_events_with_local_ffmpeg_provider(tmp_path: Path) -> None:
    """Every normalize attempt produces a dispatch_intent / dispatch_result
    pair in events.db with agent='normalizer', provider='local_ffmpeg',
    cost_usd=0.0. Keeps the audit trail uniform with other dispatches.
    Verifies both the happy path and the failed-result path."""
    manifest = minimal_manifest()
    shot_ok = _build_ready_shot(manifest, "sh_001")
    shot_fail = _build_ready_shot(manifest, "sh_002")
    normalize = _make_normalize_stub([
        _ok_normalize_result("sh_001", 1, tmp_path),
        _failed_normalize_result("sh_002", "encode_failed"),
    ])
    manifest_path = tmp_path / "manifest.json"

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=manifest_path,
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, manifest_path),
                judge=_judge_stub_approves(manifest, manifest_path),
                revise=_revise_stub_never,
                normalize=normalize,
            ),
        )
        orch.run()

        # Four events: intent+result for sh_001 (ok), intent+result for sh_002 (failed)
        norm_events = [e for e in events.recent(limit=50) if e.agent == "normalizer"]
        assert len(norm_events) == 4
        kinds = sorted(e.kind for e in norm_events)
        assert kinds == ["dispatch_intent", "dispatch_intent", "dispatch_result", "dispatch_result"]
        # Every payload uses the local_ffmpeg provider + zero cost
        for e in norm_events:
            assert e.payload["provider"] == "local_ffmpeg"
            if "cost_usd" in e.payload:
                assert e.payload["cost_usd"] == 0.0
        # Each result references its intent via ref_event_id
        results = [e for e in norm_events if e.kind == "dispatch_result"]
        for r in results:
            assert r.ref_event_id is not None
        # Success result carries output_md5; failure result carries failure_stage
        by_status = {r.payload.get("status"): r for r in results}
        assert "ok" in by_status
        assert by_status["ok"].payload["output_md5"] == "0123456789abcdef0123456789abcdef"
        assert "failed" in by_status
        assert by_status["failed"].payload["failure_stage"] == "encode_failed"


def test_normalize_dispatch_exception_writes_dispatch_failure_event(tmp_path: Path) -> None:
    """A bare exception from the tool also writes an event row — as a
    dispatch_failure (not dispatch_result) to distinguish bug from bad input."""
    manifest = minimal_manifest()
    _build_ready_shot(manifest, "sh_001")
    normalize = _make_normalize_stub([RuntimeError("ffprobe segfaulted")])
    manifest_path = tmp_path / "manifest.json"

    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=manifest_path,
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, manifest_path),
                judge=_judge_stub_approves(manifest, manifest_path),
                revise=_revise_stub_never,
                normalize=normalize,
            ),
        )
        orch.run()

        norm_events = [e for e in events.recent(limit=50) if e.agent == "normalizer"]
        kinds = sorted(e.kind for e in norm_events)
        assert kinds == ["dispatch_failure", "dispatch_intent"]
        failure = [e for e in norm_events if e.kind == "dispatch_failure"][0]
        assert failure.payload["error_type"] == "RuntimeError"
        assert "ffprobe segfaulted" in failure.payload["error"]


def test_normalize_runs_before_audio(tmp_path: Path) -> None:
    """When both phases are wired, normalize fires first so the audio
    phase observes a final dict that already carries normalized_*."""
    manifest = minimal_manifest()
    shot = _build_ready_shot(manifest, "sh_001")
    shot["audio_cues"] = [
        {"mode": "sfx", "sfx_id": "thud", "description": "thud", "duration_s": 1.0},
    ]
    shot["audio_status"] = "pending"

    normalize = _make_normalize_stub([_ok_normalize_result("sh_001", 1, tmp_path)])
    # Capture final-dict snapshot at the moment the audio fn is invoked.
    observed: dict[str, Any] = {}

    def _audio_fn(*, shot: dict, cue: dict, attempt_id: int) -> dict:
        observed["final_snapshot"] = dict(shot.get("final") or {})
        audio_path = tmp_path / "audio" / f"{shot['shot_id']}_{cue['sfx_id']}_v1.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"\x00")
        return {
            "status": "ok",
            "provider": "elevenlabs",
            "mode": "sfx",
            "shot_id": shot["shot_id"],
            "sfx_id": cue["sfx_id"],
            "description": cue["description"],
            "audio_path": str(audio_path),
            "audio_md5": "d" * 32,
            "output_size_bytes": 1,
            "duration_s": 1.0,
            "cost_usd": 0.0,
            "quota_cost": 10,
            "latency_s": 0.1,
        }

    manifest_path = tmp_path / "manifest.json"
    with open_event_log(tmp_path / "events.db") as events:
        orch = FilmOrchestrator(
            manifest=manifest,
            manifest_path=manifest_path,
            events=events,
            tools=ToolSet(
                render=_render_stub_ok(manifest, manifest_path),
                judge=_judge_stub_approves(manifest, manifest_path),
                revise=_revise_stub_never,
                audio=_audio_fn,
                normalize=normalize,
            ),
        )
        orch.run()

    assert "normalized_path" in observed["final_snapshot"]
    assert shot["audio_status"] == "ok"
    assert shot["final"]["normalized_md5"] == "0123456789abcdef0123456789abcdef"
