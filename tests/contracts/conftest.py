"""Factories for building minimal manifest fragments used in contract tests.

Kept narrow: each helper returns the smallest valid dict slice for its purpose,
so tests can compose a manifest inline without boilerplate. These helpers are
NOT a full manifest builder — contract logic doesn't touch every field, so
most tests skip `brief`, `script`, `budget`, etc.
"""

from __future__ import annotations

from typing import Any


def make_manifest(
    shots: list[dict[str, Any]] | None = None,
    dialogue: list[dict[str, Any]] | None = None,
    sfx: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "manifest_version": "1.0",
        "shots": shots or [],
        "audio": {"dialogue": dialogue or [], "sfx": sfx or []},
        "creative_decisions": [],
    }


def make_shot(
    shot_id: str,
    *,
    status: str = "approved",
    attempts: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
    judge_feedback: list[dict[str, Any]] | None = None,
    creative_feedback: list[dict[str, Any]] | None = None,
    artistic_direction: str | None = None,
    final_attempt_id: int | None = None,
    normalized_path: str | None = None,
    normalized_md5: str | None = None,
    unnormalized: bool = False,
) -> dict[str, Any]:
    """Factory for a shot dict used in contract tests.

    When status="approved", the default behavior is to produce an
    Editor-ready shot — the final block is populated with normalized_path
    + normalized_md5. This keeps pre-existing editor_agent dispatch tests
    clean of Contract 6 (normalize_to_editor) noise.

    Opt-outs for tests that exercise specific failure modes:
      unnormalized=True            — approved shot with final.render_path
                                     but no normalized_path/normalized_md5
                                     (the Contract 6 silent-breakage case).
      status=<non-approved>        — out of Contract 6 scope by definition.
    """
    shot: dict[str, Any] = {
        "shot_id": shot_id,
        "status": status,
        "attempts": attempts or [],
        "history": history or [],
        "judge_feedback": judge_feedback or [],
        "creative_feedback": creative_feedback or [],
    }
    if artistic_direction is not None:
        shot["artistic_direction"] = artistic_direction

    # Build up shot["final"] based on the flags. Four cases:
    #  (a) final_attempt_id + unnormalized → final has render_path, no normalized_*
    #  (b) final_attempt_id + normalized_path → both
    #  (c) status="approved" (default, no final_attempt_id) → auto-populate both
    #      unless unnormalized=True (no final at all then — approved-without-final)
    #  (d) non-approved status and no final_attempt_id → no final
    if final_attempt_id is not None:
        shot["final"] = {"render_path": f"artifacts/{shot_id}.mp4", "attempt_id": final_attempt_id}
        if normalized_path is not None and not unnormalized:
            shot["final"]["normalized_path"] = normalized_path
            shot["final"]["normalized_md5"] = normalized_md5 or "f" * 32
    elif normalized_path is not None and not unnormalized:
        shot["final"] = {
            "render_path": f"artifacts/{shot_id}.mp4",
            "attempt_id": 1,
            "normalized_path": normalized_path,
            "normalized_md5": normalized_md5 or "f" * 32,
        }
    elif status == "approved" and not unnormalized:
        # Default Editor-ready shape for approved shots. Tests that want the
        # approved-without-normalized failure case pass unnormalized=True.
        shot["final"] = {
            "render_path": f"artifacts/{shot_id}.mp4",
            "attempt_id": 1,
            "normalized_path": f"artifacts/renders/{shot_id}/{shot_id}_norm_v1.mp4",
            "normalized_md5": normalized_md5 or "f" * 32,
        }
    return shot


def make_dialogue(
    shot_id: str,
    *,
    line_id: str = "l1",
    duration_s: float = 2.5,
    in_s: float = 0.0,
    out_s: float = 2.5,
    compressibility_s: float | None = 0.2,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "shot_id": shot_id,
        "line_id": line_id,
        "text": "example",
        "voice_id": "v1",
        "audio_path": f"artifacts/audio/{shot_id}_{line_id}.wav",
        "duration_s": duration_s,
        "timing": {"in_s": in_s, "out_s": out_s},
    }
    if compressibility_s is not None:
        entry["compressibility_s"] = compressibility_s
    return entry


def make_attempt(
    attempt_id: int,
    *,
    outcome: str = "approved",
    started_at: str = "2026-04-22T10:00:00Z",
    completed_at: str | None = "2026-04-22T10:02:00Z",
    judge_notes: str | None = None,
    rejection_reason: str | None = None,
    provider: str = "fal_kling_25_pro",
    prompt_revision: str | None = None,
) -> dict[str, Any]:
    a: dict[str, Any] = {
        "attempt_id": attempt_id,
        "provider": provider,
        "started_at": started_at,
        "outcome": outcome,
    }
    if completed_at is not None:
        a["completed_at"] = completed_at
    if outcome == "approved":
        a["render_path"] = f"artifacts/renders/v{attempt_id}.mp4"
        a["approved_by"] = "shot_judge"
    if outcome == "rejected":
        a["rejection_reason"] = rejection_reason or "auto_judge"
    if judge_notes is not None:
        a["judge_notes"] = judge_notes
    if prompt_revision is not None:
        a["prompt_revision"] = prompt_revision
    return a


def make_creative_feedback(
    *,
    from_agent: str = "creative_director",
    priority: str = "high",
    suggestion: str = "re-render with slower camera",
    feedback: str = "Too fast for the surrounding scene",
    ts: str = "2026-04-22T12:00:00Z",
    addressed: bool = False,
    addressed_at: str | None = None,
    addressed_by: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ts": ts,
        "from_agent": from_agent,
        "feedback": feedback,
        "suggestion": suggestion,
        "priority": priority,
        "addressed": addressed,
    }
    if addressed_at is not None:
        entry["addressed_at"] = addressed_at
    if addressed_by is not None:
        entry["addressed_by"] = addressed_by
    return entry


def make_history_entry(
    event: str,
    *,
    ts: str = "2026-04-22T12:05:00Z",
    by: str = "producer",
    detail: str | None = None,
) -> dict[str, Any]:
    h: dict[str, Any] = {"ts": ts, "event": event, "by": by}
    if detail is not None:
        h["detail"] = detail
    return h


def make_judge_feedback(
    *,
    ts: str = "2026-04-22T10:01:30Z",
    feedback_type: str = "composition",
    severity: str = "note",
    observation: str = "horizon a bit low",
    suggestion: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ts": ts,
        "feedback_type": feedback_type,
        "severity": severity,
        "observation": observation,
    }
    if suggestion is not None:
        entry["suggestion"] = suggestion
    return entry
