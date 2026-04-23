"""`rectoverso judge --shot <id>` — run Shot Judge on an attempt's rendered MP4.

Flow per invocation:

    1. Load manifest + sanity-check: shot.status == "judging", attempts[-1]
       exists with a render_path on disk.
    2. Gather continuity reference frames from shots[].final for any shot_id
       in this shot's continuity_refs list (best-effort — missing refs are
       simply skipped, not errors).
    3. Dispatch through src.producer.dispatch (contract registry returns []
       for shot_judge, so this is just events + tool call).
    4. Project the tool's result into attempts[-1] (judge_score, judge_notes,
       outcome, rejection_reason, approved_by) and shots[].judge_feedback[].
    5. Transition shot.status per the verdict:
         outcome=approved  -> status=approved + final={render_path, attempt_id}
         outcome=rejected  -> status=rejected
         outcome=escalated -> status=escalated
    6. Save manifest atomically.

Explicit non-goals for this first pass:
    - Not invoking PromptSmith for a revision on rejected outcomes. The shot
      lands in `rejected` and waits; operator re-runs `rectoverso render`
      (which refuses, because status != prompted) or the yet-to-be-built
      auto-iterate loop.
    - Not auto-extracting keyframes from continuity_refs' final renders when
      those refs are themselves still in flight.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.contracts import ContractViolation
from src.producer import (
    DispatchFailure,
    ShotJudgeTool,
    dispatch,
    load_manifest,
    open_event_log,
    save_manifest_atomic,
)


def add_subparser(subparsers: "argparse._SubParsersAction[Any]") -> None:
    p = subparsers.add_parser(
        "judge",
        help="Run Shot Judge on an attempt's rendered MP4",
        description=(
            "Extract keyframes from the shot's latest render, send them + the "
            "prompt context to Claude Opus 4.7, and project the verdict back "
            "into the manifest (approved / rejected / escalated)."
        ),
    )
    p.add_argument("--shot", required=True, help="shot_id, e.g. sh_003")
    p.add_argument(
        "manifest_path",
        type=Path,
        nargs="?",
        default=Path("state/manifest.json"),
        help="manifest to read/update (default: state/manifest.json)",
    )
    p.add_argument(
        "--events-db",
        type=Path,
        default=Path("state/events.db"),
        help="events log path (default: state/events.db)",
    )
    p.add_argument(
        "--keyframes",
        type=int,
        default=3,
        help="number of keyframes to extract per render (default: 3)",
    )
    p.add_argument("--json", action="store_true", help="emit JSON summary instead of pretty")
    p.set_defaults(func=cmd_judge)


def cmd_judge(args: argparse.Namespace) -> int:
    try:
        load = load_manifest(args.manifest_path)
    except FileNotFoundError:
        print(f"error: manifest not found at {args.manifest_path}", file=sys.stderr)
        return 2

    manifest = load.manifest
    shot = next((s for s in manifest["shots"] if s["shot_id"] == args.shot), None)
    if shot is None:
        print(f"error: shot {args.shot!r} not found in manifest", file=sys.stderr)
        return 2

    if shot["status"] != "judging":
        print(
            f"error: shot {args.shot} is in status {shot['status']!r}; "
            "judge expects status == 'judging'. Run `rectoverso render` first.",
            file=sys.stderr,
        )
        return 9

    attempts = shot.get("attempts") or []
    if not attempts:
        print(f"error: shot {args.shot} has no attempts to judge", file=sys.stderr)
        return 9

    attempt = attempts[-1]
    render_path_raw = attempt.get("render_path")
    if not render_path_raw:
        print(
            f"error: attempt #{attempt.get('attempt_id')} has no render_path",
            file=sys.stderr,
        )
        return 9

    render_path = Path(render_path_raw)
    if not render_path.is_absolute():
        # Manifest paths are relative to the cwd the Producer runs in.
        render_path = (Path.cwd() / render_path_raw).resolve()
    if not render_path.exists():
        print(f"error: render not found at {render_path}", file=sys.stderr)
        return 2

    # Continuity references: pull the approved render from each referenced
    # shot's `final.render_path` and extract one keyframe as a comparison.
    reference_frames = _collect_reference_frames(
        manifest=manifest,
        continuity_refs=shot.get("continuity_refs") or [],
        cwd=Path.cwd(),
    )

    if not args.json:
        print(f"[judge] shot {args.shot}  attempt #{attempt['attempt_id']}")
        print(f"[judge] render: {render_path}")
        if reference_frames:
            print(f"[judge] continuity refs: {len(reference_frames)} keyframe(s) attached")
        print(f"[judge] dispatching Shot Judge (vision); 10-20s typical")

    tool = ShotJudgeTool(keyframes=args.keyframes)
    ctx: dict[str, Any] = {
        "shot": shot,
        "render_path": str(render_path),
        "brief": manifest.get("brief") or {},
        "reference_frames": reference_frames,
    }

    args.events_db.parent.mkdir(parents=True, exist_ok=True)

    with open_event_log(args.events_db) as events:
        try:
            result = dispatch(
                agent="shot_judge",
                shot_id=args.shot,
                manifest=manifest,
                ctx=ctx,
                tool=tool,
                events=events,
            )
        except ContractViolation as exc:
            print(f"contract block: {exc}", file=sys.stderr)
            return 5
        except DispatchFailure as exc:
            print(f"dispatch failed: {exc}", file=sys.stderr)
            return 5
        final_event_id = result.result_event_id

    # Clean up any temp reference frames we extracted.
    _cleanup_reference_frames(reference_frames)

    tool_out = dict(result.result)
    outcome = tool_out["outcome"]

    # Project into attempt
    attempt["judge_score"] = float(tool_out["judge_score"])
    attempt["judge_notes"] = tool_out["judge_notes"]
    attempt["outcome"] = outcome if outcome != "escalated" else "rejected"
    # Schema requires completed_at on every outcome; stamp it now that the
    # judge has terminated this attempt.
    attempt.setdefault("completed_at", _now_iso())
    if outcome == "approved":
        attempt["approved_by"] = "shot_judge"
    elif outcome in ("rejected", "escalated"):
        attempt["rejection_reason"] = tool_out.get("rejection_reason") or "auto_judge"

    # Per-shot feedback array
    ts = _now_iso()
    feedback_entries = tool_out.get("feedback") or []
    for fb in feedback_entries:
        shot.setdefault("judge_feedback", []).append(
            {
                "ts": ts,
                "feedback_type": str(fb.get("feedback_type", "composition")),
                "severity": str(fb.get("severity", "note")),
                "observation": str(fb.get("observation", ""))[:500],
                **(
                    {"suggestion": str(fb.get("suggestion", ""))[:500]}
                    if fb.get("suggestion")
                    else {}
                ),
                "from_agent": "shot_judge",
            }
        )

    # Transition shot status
    if outcome == "approved":
        shot["status"] = "approved"
        shot["final"] = {
            "render_path": attempt["render_path"],
            "attempt_id": int(attempt["attempt_id"]),
        }
    elif outcome == "rejected":
        shot["status"] = "rejected"
    elif outcome == "escalated":
        shot["status"] = "escalated"

    shot.setdefault("history", []).append(
        {
            "ts": ts,
            "event": f"judged_{outcome}",
            "by": "shot_judge",
            "detail": f"score={attempt['judge_score']:.3f} mode={tool_out.get('mode')}",
        }
    )

    save_manifest_atomic(args.manifest_path, manifest, last_event_id=final_event_id)

    summary = {
        "shot_id": args.shot,
        "attempt_id": attempt["attempt_id"],
        "outcome": outcome,
        "judge_score": tool_out["judge_score"],
        "composition": tool_out["composition"],
        "prompt_adherence": tool_out["prompt_adherence"],
        "continuity": tool_out["continuity"],
        "artifact_flag": tool_out["artifact_flag"],
        "mode": tool_out["mode"],
        "rejection_reason": tool_out.get("rejection_reason"),
        "shot_status": shot["status"],
        "usage": tool_out.get("usage") or {},
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print()
        verdict_tag = {
            "approved": "APPROVED",
            "rejected": "REJECTED",
            "escalated": "ESCALATED",
        }.get(outcome, outcome.upper())
        print(
            f"[judge] {verdict_tag}  score={summary['judge_score']:.3f} "
            f"(comp={summary['composition']:.2f} "
            f"adherence={summary['prompt_adherence']:.2f} "
            f"continuity={summary['continuity']:.2f}"
            f"{' artifact!' if summary['artifact_flag'] else ''})"
        )
        print(f"[judge] mode: {summary['mode']}")
        if tool_out.get("judge_notes"):
            notes = tool_out["judge_notes"]
            print(f"[judge] notes: {notes[:300]}{'...' if len(notes) > 300 else ''}")
        print(f"[judge] shot status -> {summary['shot_status']}")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _collect_reference_frames(
    *,
    manifest: dict,
    continuity_refs: list[str],
    cwd: Path,
) -> list[str]:
    """For each continuity_ref, pull a midpoint keyframe from its approved
    render (if any) and write it to a tmp file. Returns absolute paths.

    Best-effort — missing refs, unapproved refs, or ffmpeg failures all silently
    skip. Judge can still form a verdict without every reference.
    """
    if not continuity_refs:
        return []
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return []

    out: list[str] = []
    for ref_id in continuity_refs:
        ref_shot = next((s for s in manifest["shots"] if s["shot_id"] == ref_id), None)
        if ref_shot is None:
            continue
        final = ref_shot.get("final") or {}
        ref_path = final.get("render_path")
        if not ref_path:
            continue
        abs_ref = Path(ref_path)
        if not abs_ref.is_absolute():
            abs_ref = (cwd / ref_path).resolve()
        if not abs_ref.exists():
            continue
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"rv_ref_{ref_id}_", suffix=".jpg", delete=False
        )
        tmp.close()
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(abs_ref),
            "-ss",
            "00:00:00.5",
            "-frames:v",
            "1",
            "-vf",
            "scale='min(1024,iw)':-2",
            "-q:v",
            "3",
            "-y",
            tmp.name,
        ]
        proc = subprocess.run(cmd, capture_output=True, check=False)
        if proc.returncode == 0 and os.path.getsize(tmp.name) > 0:
            out.append(tmp.name)
        else:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
    return out


def _cleanup_reference_frames(paths: list[str]) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass
