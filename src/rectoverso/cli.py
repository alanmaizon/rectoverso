"""rectoverso CLI — argparse-based, read-only/dry-run commands.

Surface (see docs/cli.md for full reference):

    rectoverso manifest show [PATH]
    rectoverso manifest validate [PATH]
    rectoverso budget show [PATH]
    rectoverso budget check --provider <id> --cost <usd> [--creative] [--quota N] [--credits N] [PATH]
    rectoverso events tail [--shot sh_XXX] [--limit N] [--db PATH]
    rectoverso router pick --shot sh_XXX [PATH]
    rectoverso contracts verify --agent <name> [--shot sh_XXX] [PATH]
    rectoverso version

Design principles:
    - Pure inspection. No tool dispatch, no live API calls.
    - Structured JSON output available via `--json` on every inspection command.
    - Exit code 0 on success; non-zero on any error, violation, or refusal.
    - Terminal output is terse and mono-friendly; colors optional.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from . import __version__


DEFAULT_MANIFEST = Path("state/manifest.json")
DEFAULT_EVENTS_DB = Path("state/events.db")
DEFAULT_CAPABILITIES = Path("router/capabilities.yaml")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _emit(payload: Any, *, as_json: bool, pretty_fn=None) -> None:
    """Print either JSON or a custom pretty representation."""
    if as_json:
        print(json.dumps(_to_jsonable(payload), indent=2, sort_keys=True))
        return
    if pretty_fn is not None:
        pretty_fn(payload)
    else:
        print(json.dumps(_to_jsonable(payload), indent=2, sort_keys=True))


def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, Mapping):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "value") and type(obj).__mro__.__len__() > 2:
        # Enum-like
        return obj.value
    return obj


def _bar(ratio: float, width: int = 24) -> str:
    ratio = max(0.0, min(1.0, ratio))
    fill = int(round(ratio * width))
    return "█" * fill + "·" * (width - fill)


# ---------------------------------------------------------------------------
# manifest show | validate
# ---------------------------------------------------------------------------


def _load_manifest_or_die(path: Path):
    from src.producer import load_manifest, ManifestValidationError

    try:
        return load_manifest(path)
    except FileNotFoundError:
        print(f"error: manifest not found at {path}", file=sys.stderr)
        sys.exit(2)
    except ManifestValidationError as e:
        print(f"error: manifest schema validation failed: {e.cause.message}", file=sys.stderr)
        sys.exit(3)
    except json.JSONDecodeError as e:
        print(f"error: manifest is not valid JSON: {e.msg}", file=sys.stderr)
        sys.exit(3)


def cmd_manifest_show(args: argparse.Namespace) -> int:
    load = _load_manifest_or_die(args.path)
    m = load.manifest

    def pretty(_):
        print(f"project_id      {m['project_id']}")
        print(f"schema          {m['manifest_version']}")
        print(f"stage           {m['run_state']['current_stage']}")
        print(f"resumable       {m['run_state']['resumable']}    last_event_id={m['run_state']['last_event_id']}")
        print(f"was_dirty       {load.was_dirty}")
        print()
        shots = m.get("shots", [])
        print(f"shots           {len(shots)}")
        by_status: dict[str, int] = {}
        for s in shots:
            by_status[s["status"]] = by_status.get(s["status"], 0) + 1
        for status, count in sorted(by_status.items()):
            print(f"                  {status:<12} {count}")
        print()
        edit = m.get("edit") or {}
        print(f"edit.renderer   {edit.get('renderer', '—')}  v{edit.get('renderer_version', '—')}")
        print(f"edit.status     {edit.get('status', '—')}")
        if edit.get("render_md5"):
            print(f"render md5      {edit['render_md5']}")
        print()
        b = m.get("budget", {})
        cap = float(b.get("cap_usd", 0.0))
        spent = float(b.get("spent_usd", 0.0))
        ratio = (spent / cap) if cap > 0 else 0.0
        print(f"budget          ${spent:.2f} / ${cap:.2f}   [{_bar(ratio)}]  {ratio*100:.1f}%")
        print(f"alibaba quota   {b.get('alibaba_quota_remaining', '—')} remaining")
        print(f"elevenlabs      {b.get('elevenlabs_credits_remaining', '—')} credits remaining")

    _emit(
        {
            "project_id": m["project_id"],
            "manifest_version": m["manifest_version"],
            "run_state": m["run_state"],
            "was_dirty": load.was_dirty,
            "shot_count": len(m.get("shots", [])),
            "shots_by_status": _status_histogram(m.get("shots", [])),
            "edit": m.get("edit", {}),
            "budget": m.get("budget", {}),
        },
        as_json=args.json,
        pretty_fn=pretty,
    )
    return 1 if load.was_dirty else 0


def _status_histogram(shots: list[dict]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for s in shots:
        hist[s.get("status", "?")] = hist.get(s.get("status", "?"), 0) + 1
    return hist


def cmd_manifest_validate(args: argparse.Namespace) -> int:
    _load_manifest_or_die(args.path)  # raises on invalid; otherwise ok
    if args.json:
        print(json.dumps({"ok": True, "path": str(args.path)}))
    else:
        print(f"ok    {args.path} validates against manifest.schema.json")
    return 0


# ---------------------------------------------------------------------------
# budget show | check
# ---------------------------------------------------------------------------


def cmd_budget_show(args: argparse.Namespace) -> int:
    load = _load_manifest_or_die(args.path)
    b = load.manifest.get("budget", {})

    def pretty(_):
        cap = float(b.get("cap_usd", 0.0))
        spent = float(b.get("spent_usd", 0.0))
        remain = max(0.0, cap - spent)
        ratio = (spent / cap) if cap > 0 else 0.0
        print(f"cap       ${cap:.2f}")
        print(f"spent     ${spent:.2f}   [{_bar(ratio)}]  {ratio*100:.1f}%")
        print(f"remain    ${remain:.2f}")
        print()
        by = b.get("by_provider", {})
        if by:
            print("by provider:")
            for k, v in sorted(by.items(), key=lambda kv: -float(kv[1])):
                print(f"  {k:<30} ${float(v):>7.2f}")
        print()
        print(f"alibaba quota    {b.get('alibaba_quota_remaining', '—')} remaining")
        print(f"elevenlabs       {b.get('elevenlabs_credits_remaining', '—')} credits")
        # Sum invariant check
        total = sum(float(v) for v in (by or {}).values())
        if by and abs(total - spent) > 1e-6:
            print()
            print(f"WARNING: spent_usd ({spent}) != sum(by_provider) ({round(total, 6)})")

    _emit(b, as_json=args.json, pretty_fn=pretty)
    return 0


def cmd_budget_check(args: argparse.Namespace) -> int:
    from src.producer import check_before_render, BudgetCheck

    load = _load_manifest_or_die(args.path)
    check: BudgetCheck = check_before_render(
        load.manifest,
        provider_id=args.provider,
        estimated_cost_usd=args.cost,
        estimated_quota_cost=args.quota,
        estimated_credit_cost=args.credits,
        creative_driven=args.creative,
    )

    def pretty(_):
        verdict = "ALLOW" if check.allowed else "REFUSE"
        print(f"{verdict}    provider={check.provider_id}")
        print(f"  rationale         {check.rationale}")
        print(f"  est. cost         ${check.estimated_cost_usd:.4f}")
        print(f"  projected spent   ${check.projected_spent_usd:.4f} / ${check.cap_usd:.2f}")
        if check.veo_spent_projected_usd is not None:
            print(f"  veo projected     ${check.veo_spent_projected_usd:.4f}")
        if check.alibaba_quota_projected is not None:
            print(f"  alibaba quota     {check.alibaba_quota_projected}")
        if check.elevenlabs_credits_projected is not None:
            print(f"  elevenlabs        {check.elevenlabs_credits_projected}")
        if args.creative:
            print(f"  mode              creative-driven (soft cap {check.detail['soft_cap_ratio']*100:.0f}%)")

    _emit(check, as_json=args.json, pretty_fn=pretty)
    return 0 if check.allowed else 1


# ---------------------------------------------------------------------------
# events tail
# ---------------------------------------------------------------------------


def cmd_events_tail(args: argparse.Namespace) -> int:
    from src.producer import open_event_log

    if not args.db.exists():
        print(f"error: events db not found at {args.db}", file=sys.stderr)
        return 2

    with open_event_log(args.db) as log:
        if args.shot:
            events = log.for_shot(args.shot)[-args.limit :]
        else:
            events = list(reversed(log.recent(limit=args.limit)))

    def pretty(_):
        if not events:
            print("(no events)")
            return
        for e in events:
            ref = f" ← {e.ref_event_id}" if e.ref_event_id else ""
            shot = f" [{e.shot_id}]" if e.shot_id else ""
            agent = f" {e.agent}" if e.agent else ""
            print(f"#{e.event_id:<5} {e.ts}{agent}{shot}  {e.kind}{ref}")

    payload = [
        {
            "event_id": e.event_id,
            "ts": e.ts,
            "kind": e.kind,
            "agent": e.agent,
            "shot_id": e.shot_id,
            "ref_event_id": e.ref_event_id,
            "payload": e.payload,
        }
        for e in events
    ]
    _emit(payload, as_json=args.json, pretty_fn=pretty)
    return 0


# ---------------------------------------------------------------------------
# router pick
# ---------------------------------------------------------------------------


def cmd_router_pick(args: argparse.Namespace) -> int:
    from src.router import engine as router_engine
    from src.router.types import (
        BudgetState,
        PriorFailure,
        ShotSpec,
        RoutingError,
    )

    load = _load_manifest_or_die(args.path)
    m = load.manifest

    shot = next((s for s in m.get("shots", []) if s["shot_id"] == args.shot), None)
    if shot is None:
        print(f"error: shot {args.shot} not found in manifest", file=sys.stderr)
        return 2

    spec = ShotSpec(
        shot_id=shot["shot_id"],
        duration_s=float(shot["duration_s"]),
        has_humans=bool(shot["has_humans"]),
        is_hero=bool(shot["is_hero"]),
        motion_level=shot["motion_level"],
        prior_failures=tuple(
            PriorFailure(provider=a["provider"], outcome=a["outcome"])
            for a in shot.get("attempts", [])
            if a.get("outcome") in ("failed", "rejected")
        ),
        reference_subject_count=len(shot.get("prompt", {}).get("reference_subject_paths", []) or []),
        has_end_frame="end_frame_path" in shot.get("prompt", {}),
    )
    b = m.get("budget", {})
    budget = BudgetState(
        cap_usd=float(b.get("cap_usd", 0.0)),
        spent_usd=float(b.get("spent_usd", 0.0)),
        by_provider=b.get("by_provider", {}),
        alibaba_quota_remaining=int(b.get("alibaba_quota_remaining", 0)),
        elevenlabs_credits_remaining=int(b.get("elevenlabs_credits_remaining", 0)),
    )
    caps = router_engine.load_capabilities(args.capabilities)

    try:
        choice = router_engine.route(spec, budget, caps)
    except RoutingError as e:
        if args.json:
            print(json.dumps({"error": "RoutingError", "message": str(e), "exclusions": dict(e.exclusions)}, indent=2))
        else:
            print(f"error: no provider survived hard rules for {args.shot}")
            for k, v in e.exclusions.items():
                print(f"  {k:<30} {v}")
        return 4

    def pretty(_):
        print(f"shot {spec.shot_id}")
        print(f"  pick     {choice.provider_id} ({choice.model_id})")
        print(f"  est cost ${choice.estimated_cost_usd:.4f}")
        print(f"  rationale {choice.rationale}")
        if choice.alternates:
            print(f"  alternates {', '.join(choice.alternates)}")

    _emit(choice, as_json=args.json, pretty_fn=pretty)
    return 0


# ---------------------------------------------------------------------------
# contracts verify
# ---------------------------------------------------------------------------


def cmd_contracts_verify(args: argparse.Namespace) -> int:
    from src.contracts import ContractViolation, validate_before_dispatch

    load = _load_manifest_or_die(args.path)
    ctx: dict[str, Any] = {}
    if args.revision:
        ctx["revision"] = True
    if args.creative_driven:
        ctx["creative_driven"] = True
    if args.editor_priority:
        ctx["editor_priority"] = args.editor_priority

    try:
        warns = validate_before_dispatch(args.agent, args.shot, load.manifest, ctx)
    except ContractViolation as e:
        if args.json:
            payload = {
                "status": "block",
                "violations": [_to_jsonable(asdict(v)) for v in e.violations],
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"BLOCK   agent={args.agent}  shot={args.shot or 'film'}")
            for v in e.violations:
                print(f"  [{v.contract.value}] {v.reason}")
        return 5

    if args.json:
        payload = {
            "status": "allow",
            "warns": [_to_jsonable(asdict(w)) for w in warns],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"ALLOW   agent={args.agent}  shot={args.shot or 'film'}")
        for w in warns:
            print(f"  warn [{w.contract.value}] {w.reason}")
    return 0


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def cmd_version(args: argparse.Namespace) -> int:
    print(f"rectoverso {__version__}")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rectoverso",
        description="Inspect, dry-run, and preflight the rectoverso pipeline. All commands are read-only.",
    )
    sub = p.add_subparsers(dest="group", required=True)

    # ---- manifest ---------------------------------------------------------
    g_man = sub.add_parser("manifest", help="manifest inspection and validation")
    man_sub = g_man.add_subparsers(dest="op", required=True)

    p_show = man_sub.add_parser("show", help="Pretty-print the current manifest state")
    p_show.add_argument("path", type=Path, nargs="?", default=DEFAULT_MANIFEST)
    p_show.add_argument("--json", action="store_true", help="emit JSON instead of pretty output")
    p_show.set_defaults(func=cmd_manifest_show)

    p_val = man_sub.add_parser("validate", help="Validate against schemas/manifest.schema.json")
    p_val.add_argument("path", type=Path, nargs="?", default=DEFAULT_MANIFEST)
    p_val.add_argument("--json", action="store_true")
    p_val.set_defaults(func=cmd_manifest_validate)

    # ---- budget -----------------------------------------------------------
    g_bud = sub.add_parser("budget", help="USD cap, quotas, and pre-dispatch projections")
    bud_sub = g_bud.add_subparsers(dest="op", required=True)

    p_bshow = bud_sub.add_parser("show", help="Show current budget state + per-provider breakdown")
    p_bshow.add_argument("path", type=Path, nargs="?", default=DEFAULT_MANIFEST)
    p_bshow.add_argument("--json", action="store_true")
    p_bshow.set_defaults(func=cmd_budget_show)

    p_bcheck = bud_sub.add_parser(
        "check",
        help="Dry-run a budget projection for a hypothetical render",
    )
    p_bcheck.add_argument("--provider", required=True, help="provider id, e.g. fal_kling_2_1_pro")
    p_bcheck.add_argument("--cost", type=float, default=0.0, help="estimated USD cost")
    p_bcheck.add_argument("--quota", type=int, default=0, help="estimated alibaba quota cost")
    p_bcheck.add_argument("--credits", type=int, default=0, help="estimated elevenlabs credit cost")
    p_bcheck.add_argument("--creative", action="store_true", help="creative-driven re-render (soft 95%% cap)")
    p_bcheck.add_argument("path", type=Path, nargs="?", default=DEFAULT_MANIFEST)
    p_bcheck.add_argument("--json", action="store_true")
    p_bcheck.set_defaults(func=cmd_budget_check)

    # ---- events -----------------------------------------------------------
    g_ev = sub.add_parser("events", help="Read the append-only event log")
    ev_sub = g_ev.add_subparsers(dest="op", required=True)

    p_tail = ev_sub.add_parser("tail", help="Show recent events (most recent first)")
    p_tail.add_argument("--shot", help="filter to one shot_id (e.g. sh_003)")
    p_tail.add_argument("--limit", type=int, default=30)
    p_tail.add_argument("--db", type=Path, default=DEFAULT_EVENTS_DB)
    p_tail.add_argument("--json", action="store_true")
    p_tail.set_defaults(func=cmd_events_tail)

    # ---- router -----------------------------------------------------------
    g_rt = sub.add_parser("router", help="Dry-run the router against a shot")
    rt_sub = g_rt.add_subparsers(dest="op", required=True)

    p_pick = rt_sub.add_parser("pick", help="Pick a provider for a named shot")
    p_pick.add_argument("--shot", required=True, help="shot_id, e.g. sh_003")
    p_pick.add_argument(
        "--capabilities",
        type=Path,
        default=DEFAULT_CAPABILITIES,
        help="path to capabilities.yaml",
    )
    p_pick.add_argument("path", type=Path, nargs="?", default=DEFAULT_MANIFEST)
    p_pick.add_argument("--json", action="store_true")
    p_pick.set_defaults(func=cmd_router_pick)

    # ---- contracts --------------------------------------------------------
    g_ct = sub.add_parser("contracts", help="Preflight the pair-contract enforcement layer")
    ct_sub = g_ct.add_subparsers(dest="op", required=True)

    p_verify = ct_sub.add_parser(
        "verify",
        help="Run validate_before_dispatch against the current manifest",
    )
    p_verify.add_argument(
        "--agent",
        required=True,
        choices=[
            "editor_agent",
            "shot_judge",
            "audio_agent",
            "creative_director",
            "prompt_smith",
            "renderer",
            "screenwriter",
        ],
    )
    p_verify.add_argument("--shot", help="target shot_id (omit for film-level dispatch)")
    p_verify.add_argument("--revision", action="store_true", help="ctx.revision=True")
    p_verify.add_argument("--creative-driven", action="store_true", help="ctx.creative_driven=True")
    p_verify.add_argument("--editor-priority", help="ctx.editor_priority (critical|high|medium|low)")
    p_verify.add_argument("path", type=Path, nargs="?", default=DEFAULT_MANIFEST)
    p_verify.add_argument("--json", action="store_true")
    p_verify.set_defaults(func=cmd_contracts_verify)

    # ---- version ----------------------------------------------------------
    p_ver = sub.add_parser("version", help="Print the CLI version")
    p_ver.set_defaults(func=cmd_version)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
