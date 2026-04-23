"""`rectoverso film <brief.json>` — one-command end-to-end driver.

Wires the Producer's FilmOrchestrator to real (render/judge/revise/generate-ref)
tool callables. Given a brief, walks the whole pipeline: seed manifest →
Screenwriter → router + PromptSmith per shot → orchestrator (render → judge
→ revise loop per shot) → summary.

Resumable: if `--resume` is passed with a path to an existing manifest,
skips the brief→Screenwriter seeding step and walks only the orchestrator
loop. Already-approved shots in the manifest are not re-rendered.

Run modes:
    --run-mode testing      (default) — router filters to testing-tier
                            providers (Wan / Kling Std / Qwen).
    --run-mode submission   — router filters to submission-tier providers
                            (Veo / Seedance / Kling Pro / nano-banana).

Exit codes:
    0  all shots approved or cleanly escalated (film landed)
    2  missing / invalid brief or manifest
    4  router could not place a shot (no provider matches)
    5  contract violation or dispatch failure
    10 budget halted mid-run
    11 one or more shots failed (not escalated — hard failures)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from src.contracts import ContractViolation
from src.producer import (
    DispatchFailure,
    ElevenLabsAudioTool,
    FilmOrchestrator,
    FilmResult,
    NormalizeTool,
    PromptSmithTool,
    RetryPolicy,
    ScreenwriterTool,
    ToolSet,
    default_client,
    load_manifest,
    open_event_log,
    save_manifest_atomic,
)

from . import run as _run_driver


def add_subparser(subparsers: "argparse._SubParsersAction[Any]") -> None:
    p = subparsers.add_parser(
        "film",
        help="Render a complete film end-to-end: brief → shots → approved MP4s",
        description=(
            "Orchestrator command. Takes a brief JSON, walks Screenwriter, "
            "router, PromptSmith, then for each shot runs render → judge → "
            "revise loop until every shot is approved or escalated."
        ),
    )
    p.add_argument(
        "brief_path",
        type=Path,
        nargs="?",
        help="path to brief.json (required unless --resume is set)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="skip Screenwriter + initial routing; run orchestrator loop on "
        "an existing manifest (use with `manifest_path`)",
    )
    p.add_argument(
        "--manifest",
        type=Path,
        default=Path("state/manifest.json"),
        help="manifest path (used for both initial seed output and --resume input)",
    )
    p.add_argument(
        "--events-db",
        type=Path,
        default=Path("state/events.db"),
        help="event log path (default: state/events.db)",
    )
    p.add_argument(
        "--capabilities",
        type=Path,
        default=Path("router/capabilities.yaml"),
        help="router capabilities.yaml",
    )
    p.add_argument(
        "--run-mode",
        choices=("submission", "testing"),
        default="testing",
        help="testing (default) filters to cheap tier; submission filters to premium tier",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="per-shot retry cap before escalating (default 3)",
    )
    p.add_argument(
        "--output-root",
        type=Path,
        default=Path("artifacts/renders"),
        help="root directory for rendered MP4s",
    )
    p.add_argument(
        "--ref-output-root",
        type=Path,
        default=Path("artifacts/refs"),
        help="directory for auto-generated reference images",
    )
    p.add_argument(
        "--audio-output-root",
        type=Path,
        default=Path("artifacts/audio"),
        help="directory for Audio Agent output (ElevenLabs SFX + TTS)",
    )
    p.add_argument(
        "--no-audio",
        action="store_true",
        help="skip the audio phase entirely (ToolSet.audio=None)",
    )
    p.add_argument(
        "--no-normalize",
        action="store_true",
        help="skip the normalize pre-pass (ToolSet.normalize=None). Editor will see heterogeneous codec inputs.",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_film)


def cmd_film(args: argparse.Namespace) -> int:
    # 1. Seed phase — either resume or run the brief→manifest driver.
    if not args.resume:
        if args.brief_path is None:
            print("error: brief_path is required unless --resume is set", file=sys.stderr)
            return 2
        client = default_client()
        try:
            _run_driver.run(
                brief=_run_driver._load_brief(args.brief_path),
                brief_path=args.brief_path,
                manifest_path=args.manifest,
                events_db=args.events_db,
                capabilities_path=args.capabilities,
                screenwriter=ScreenwriterTool(client=client),
                prompt_smith=PromptSmithTool(client=client),
                run_mode=args.run_mode,
            )
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except _run_driver.BriefError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # 2. Orchestrator phase — load the (now-seeded) manifest and walk it.
    try:
        load = load_manifest(args.manifest)
    except FileNotFoundError:
        print(f"error: manifest not found at {args.manifest}", file=sys.stderr)
        return 2

    tools = _build_toolset(args)
    retry_policy = RetryPolicy(max_attempts_per_shot=args.max_attempts)

    with open_event_log(args.events_db) as events:
        orchestrator = FilmOrchestrator(
            manifest=load.manifest,
            manifest_path=args.manifest,
            events=events,
            tools=tools,
            retry_policy=retry_policy,
            run_mode=args.run_mode,
        )
        try:
            film_result = orchestrator.run()
        except ContractViolation as exc:
            print(f"contract violation: {exc}", file=sys.stderr)
            return 5
        except DispatchFailure as exc:
            print(f"dispatch failed: {exc}", file=sys.stderr)
            return 5

    # 3. Summary + exit code.
    if args.json:
        print(json.dumps(_result_to_jsonable(film_result), indent=2, sort_keys=True))
    else:
        _print_pretty(film_result)

    if film_result.budget_halted_count > 0:
        return 10
    if film_result.failed_count > 0:
        return 11
    return 0


# ---------------------------------------------------------------------------
# Tool wiring — the real callables backed by existing CLI commands
# ---------------------------------------------------------------------------


def _build_toolset(args: argparse.Namespace) -> ToolSet:
    """Build the ToolSet the orchestrator consumes. Each callable shells to
    the same underlying command the operator would run manually, keeping
    the orchestrator's per-shot behavior identical to a hand-driven run."""

    from . import generate_ref_cmd as _ref
    from . import judge_cmd as _judge
    from . import render_cmd as _render
    from . import revise_cmd as _revise

    def _render_fn(*, shot: dict, attempt_id: int, run_mode: str) -> dict:
        # Build an argparse.Namespace matching render_cmd's expectations.
        ns = argparse.Namespace(
            shot=shot["shot_id"],
            manifest_path=args.manifest,
            events_db=args.events_db,
            output_root=args.output_root,
            resolution="720p",
            json=True,
        )
        # cmd_render returns an exit code; the manifest atomic save inside
        # it carries the state projection we care about. The orchestrator
        # reads the shot dict back directly after this call.
        _render.cmd_render(ns)
        # Return the last attempt's tool-result-shaped dict so the
        # orchestrator can make decisions. Re-read from shot in-memory.
        last = (shot.get("attempts") or [{}])[-1]
        return {
            "status": "ok" if last.get("outcome") in ("pending", "approved") else "failed",
            "provider": last.get("provider"),
            "render_path": last.get("render_path"),
            "cost_usd": last.get("cost_usd", 0.0),
            "latency_s": last.get("latency_s", 0.0),
        }

    def _judge_fn(*, shot: dict, attempt_id: int) -> dict:
        ns = argparse.Namespace(
            shot=shot["shot_id"],
            manifest_path=args.manifest,
            events_db=args.events_db,
            keyframes=3,
            json=True,
        )
        _judge.cmd_judge(ns)
        last = (shot.get("attempts") or [{}])[-1]
        return {
            "judge_score": last.get("judge_score", 0.0),
            "outcome": shot.get("status"),     # approved | rejected | escalated
            "escalation_reason": last.get("escalation_reason"),
        }

    def _revise_fn(*, shot: dict) -> dict:
        ns = argparse.Namespace(
            shot=shot["shot_id"],
            manifest_path=args.manifest,
            events_db=args.events_db,
            json=True,
        )
        _revise.cmd_revise(ns)
        return {"primary": (shot.get("prompt") or {}).get("primary", "")}

    def _generate_ref_fn(*, shot: dict, brief: Mapping[str, Any]) -> dict:
        ns = argparse.Namespace(
            shot=shot["shot_id"],
            manifest_path=args.manifest,
            events_db=args.events_db,
            output_root=args.ref_output_root,
            aspect_ratio="16:9",
            seed=None,
            prompt_override=None,
            provider="auto",
            json=True,
        )
        _ref.cmd_generate_ref(ns)
        refs = (shot.get("prompt") or {}).get("reference_subject_paths") or []
        return {"reference_image_paths": refs}

    # Audio callable — wraps ElevenLabsAudioTool directly (no separate CLI
    # command wrapper needed, since audio is driven entirely from the shot's
    # audio_cues array, not from operator args). One instance shared across
    # all cues for cache efficiency + connection reuse.
    audio_fn = None
    if not args.no_audio:
        audio_tool = ElevenLabsAudioTool()  # resolves key from env/.env

        def _audio_fn(*, shot: dict, cue: Mapping[str, Any], attempt_id: int) -> dict:
            # Translate the manifest cue shape into the adapter's payload.
            # The schema's cue object accepts both SFX and TTS fields under
            # one envelope discriminated by `mode`. Adapter expects the
            # payload fields directly, with output_dir + attempt_id added.
            payload = dict(cue)
            payload["output_dir"] = args.audio_output_root
            payload["attempt_id"] = attempt_id
            return audio_tool(shot["shot_id"], payload)

        audio_fn = _audio_fn

    # Normalize callable — wraps NormalizeTool. Runs post-approval on the
    # winning render (shot.final.render_path). Output lands alongside the
    # source, e.g. artifacts/renders/sh_001/sh_001_norm_v1.mp4. Failure
    # doesn't demote the shot — the orchestrator's _maybe_normalize logs
    # and continues.
    normalize_fn = None
    if not args.no_normalize:
        normalize_tool = NormalizeTool()   # resolves ffmpeg via shutil.which

        def _normalize_fn(*, shot: dict, attempt_id: int) -> dict:
            final = shot.get("final") or {}
            render_rel = final.get("render_path")
            if not render_rel:
                return {
                    "status": "failed",
                    "failure_stage": "no_render_path",
                    "stderr_tail": "shot.final.render_path missing",
                }
            # Resolve to absolute. render_path is schema-relative (no leading
            # /); cwd is the project root under normal operation.
            render_abs = Path(render_rel)
            if not render_abs.is_absolute():
                render_abs = Path.cwd() / render_abs
            return normalize_tool(shot["shot_id"], {
                "src_path": render_abs,
                "output_dir": render_abs.parent,
                "attempt_id": attempt_id,
            })

        normalize_fn = _normalize_fn

    return ToolSet(
        render=_render_fn,
        judge=_judge_fn,
        revise=_revise_fn,
        generate_ref=_generate_ref_fn,
        audio=audio_fn,
        normalize=normalize_fn,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _result_to_jsonable(r: FilmResult) -> dict:
    return {
        "project_id": r.project_id,
        "shot_count": r.shot_count,
        "approved_count": r.approved_count,
        "escalated_count": r.escalated_count,
        "budget_halted_count": r.budget_halted_count,
        "failed_count": r.failed_count,
        "escalations_by_reason": dict(r.escalations_by_reason),
        "audio_status_breakdown": dict(r.audio_status_breakdown),
        "total_audio_credits_used": r.total_audio_credits_used,
        "total_spent_usd": r.total_spent_usd,
        "total_latency_s": r.total_latency_s,
        "all_landed": r.all_landed,
        "halted_reason": r.halted_reason,
        "manifest_path": r.manifest_path,
        "shots": [
            {
                "shot_id": s.shot_id,
                "final_status": s.final_status,
                "attempts": s.attempts,
                "best_judge_score": s.best_judge_score,
                "final_render_path": s.final_render_path,
                "total_cost_usd": s.total_cost_usd,
                "escalation_reason": s.escalation_reason,
                "latency_s": s.latency_s,
                "audio_status": s.audio_status,
                "audio_cues_total": s.audio_cues_total,
                "audio_cues_ok": s.audio_cues_ok,
                "audio_credits_used": s.audio_credits_used,
                "normalized_render_path": s.normalized_render_path,
            }
            for s in r.shots
        ],
    }


def _print_pretty(r: FilmResult) -> None:
    print()
    print("=" * 70)
    print(f"FILM SUMMARY  project {r.project_id}")
    print("=" * 70)
    print(f"  shots            {r.shot_count}")
    print(f"  approved         {r.approved_count}")
    print(f"  escalated        {r.escalated_count}")
    if r.escalations_by_reason:
        for reason, count in sorted(r.escalations_by_reason.items()):
            print(f"    {reason:<24} {count}")
    print(f"  budget halted    {r.budget_halted_count}")
    print(f"  failed           {r.failed_count}")
    print(f"  total spend      ${r.total_spent_usd:.2f}")
    print(f"  wall clock       {r.total_latency_s:.1f}s")
    if r.halted_reason:
        print(f"  halted           {r.halted_reason}")
    if r.audio_status_breakdown:
        print(f"  audio credits    {r.total_audio_credits_used:,}")
        audio_summary = "  ".join(
            f"{status}={count}"
            for status, count in sorted(r.audio_status_breakdown.items())
        )
        print(f"  audio phase      {audio_summary}")
    print()
    print("Per-shot:")
    for s in r.shots:
        tag = {
            "approved": "ok ",
            "escalated": "ESC",
            "budget_halted": "HLT",
            "failed": "X  ",
        }.get(s.final_status, "?  ")
        score_s = f"score={s.best_judge_score:.3f}" if s.best_judge_score else "score=—"
        audio_s = ""
        if s.audio_cues_total > 0:
            audio_s = (
                f"  audio={s.audio_status}[{s.audio_cues_ok}/{s.audio_cues_total}]"
                f" {s.audio_credits_used}c"
            )
        elif s.audio_status != "pending":
            audio_s = f"  audio={s.audio_status}"
        print(
            f"  [{tag}] {s.shot_id:<8} attempts={s.attempts} {score_s}  "
            f"${s.total_cost_usd:.2f}"
            + audio_s
            + (f"  ({s.escalation_reason})" if s.escalation_reason else "")
        )
    print()
    print(f"manifest: {r.manifest_path}")
