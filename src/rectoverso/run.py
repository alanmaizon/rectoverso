"""`rectoverso run <brief.json>` — the Day-3 pipeline driver.

Scope, intentionally narrow:
    brief.json  →  screenwriter  →  for each shot: router → prompt_smith  →
                   manifest.json fully prompted + fully routed

Stops short of Tier-2 agents (ShotJudge/Editor/Audio/CreativeDirector),
renderer dispatches, and the retry loop. The next pipeline stage reads this
manifest and takes it from there.

Run modes:
    - Default: live Anthropic calls (Screenwriter + PromptSmith) via
      producer.default_client(). Consumes Anthropic budget.
    - --dry-run: no API calls at all. A deterministic stub producing a
      believable shot list from the brief, routed normally, and templated
      prompts. Used by tests and for offline local demos.

Output:
    state/manifest.json  — schema-valid manifest, every shot in status="prompted".
    state/events.db      — append-only event log of every dispatch.

Exit codes:
    0 success
    2 bad arguments / missing file
    3 schema-invalid brief or manifest write
    4 router raised RoutingError on a shot
    5 dispatch raised DispatchFailure or ContractViolation
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.contracts import ContractViolation
from src.producer import (
    DispatchFailure,
    EventLog,
    PromptSmithTool,
    ScreenwriterTool,
    default_client,
    dispatch,
    open_event_log,
    save_manifest_atomic,
    validate_manifest,
)
from src.router import engine as router_engine
from src.router.types import (
    BudgetState,
    PriorFailure,
    ProviderChoice,
    RoutingError,
    ShotSpec,
)


DEFAULT_MANIFEST_PATH = Path("state/manifest.json")
DEFAULT_EVENTS_DB = Path("state/events.db")
DEFAULT_CAPABILITIES = Path("router/capabilities.yaml")

# CLAUDE.md § Budget — final envelope seeds.
DEFAULT_CAP_USD = 151.0
DEFAULT_ALIBABA_QUOTA = 72
DEFAULT_ELEVENLABS_CREDITS = 117999

REQUIRED_BRIEF_FIELDS = ("logline", "target_duration_s", "tone", "genre")


class BriefError(ValueError):
    """Raised when brief.json is missing fields or the seeded manifest fails
    schema validation. Maps to exit code 3 in the CLI."""


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def add_subparser(subparsers: "argparse._SubParsersAction[Any]") -> None:
    """Wire `rectoverso run` into the main CLI parser."""
    p = subparsers.add_parser(
        "run",
        help="Drive the brief → screenwriter → router → prompt_smith pipeline",
        description=(
            "Read a brief JSON file, call Screenwriter to produce a shot list, "
            "then for each shot run the router and PromptSmith. Emits a fully "
            "prompted, fully routed manifest."
        ),
    )
    p.add_argument("brief_path", type=Path, help="path to brief.json")
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="where to write the manifest (default state/manifest.json)",
    )
    p.add_argument(
        "--events-db",
        type=Path,
        default=DEFAULT_EVENTS_DB,
        help="append-only event log path (default state/events.db)",
    )
    p.add_argument(
        "--capabilities",
        type=Path,
        default=DEFAULT_CAPABILITIES,
        help="router capabilities.yaml (default router/capabilities.yaml)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="skip Anthropic calls; use a deterministic stub (no USD/credits spent)",
    )
    p.add_argument(
        "--run-mode",
        choices=("submission", "testing"),
        default="testing",
        help=(
            "provider tier to target. 'testing' (default) uses free-quota + "
            "cheap models for iteration. 'submission' uses premium US providers "
            "(Veo 3.1 Fast, Kling Pro) for the final deliverable — burns paid "
            "budget, so do this only for finals."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit a JSON summary to stdout instead of the pretty log",
    )
    p.set_defaults(func=cmd_run)


def cmd_run(args: argparse.Namespace) -> int:
    try:
        brief = _load_brief(args.brief_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BriefError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    client = _StubClient() if args.dry_run else default_client()
    screenwriter = ScreenwriterTool(client=client)
    prompt_smith = PromptSmithTool(client=client)

    try:
        outcome = run(
            brief=brief,
            brief_path=args.brief_path,
            manifest_path=args.out,
            events_db=args.events_db,
            capabilities_path=args.capabilities,
            screenwriter=screenwriter,
            prompt_smith=prompt_smith,
            run_mode=args.run_mode,
        )
    except BriefError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except RoutingError as exc:
        print(f"error: router could not place a shot: {exc}", file=sys.stderr)
        return 4
    except (DispatchFailure, ContractViolation) as exc:
        print(f"error: dispatch failed: {exc}", file=sys.stderr)
        return 5

    if args.json:
        print(json.dumps(outcome, indent=2, sort_keys=True))
    else:
        _print_pretty(outcome)
    return 0


# ---------------------------------------------------------------------------
# Core driver (importable, testable without argparse)
# ---------------------------------------------------------------------------


def run(
    *,
    brief: Mapping[str, Any],
    brief_path: Path,
    manifest_path: Path,
    events_db: Path,
    capabilities_path: Path,
    screenwriter: ScreenwriterTool,
    prompt_smith: PromptSmithTool,
    run_mode: str = "testing",
) -> dict[str, Any]:
    """Execute the driver. Pure of argparse so tests can call this directly.

    Side effects:
        - Writes/overwrites `manifest_path` (atomic, schema-validated).
        - Appends events to `events_db`.

    Returns:
        Summary dict — see `_build_summary` for shape.
    """
    manifest_path = Path(manifest_path)
    events_db = Path(events_db)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    events_db.parent.mkdir(parents=True, exist_ok=True)

    capabilities = router_engine.load_capabilities(capabilities_path)
    manifest = _seed_manifest(brief, brief_path)

    with open_event_log(events_db) as events:
        # Save the skeleton before any dispatch, so a crash is recoverable.
        save_manifest_atomic(
            manifest_path, manifest, last_event_id=events.last_event_id()
        )

        # Screenwriter (film-level; no pair contracts).
        sw_result = dispatch(
            agent="screenwriter",
            shot_id=None,
            manifest=manifest,
            ctx={"brief": manifest["brief"]},
            tool=screenwriter,
            events=events,
        )
        _project_screenwriter(manifest, sw_result.result)
        save_manifest_atomic(
            manifest_path, manifest, last_event_id=sw_result.result_event_id
        )

        # Per-shot: router + PromptSmith.
        ps_usage_totals: dict[str, int] = {}
        for shot in manifest["shots"]:
            # router is a sync worker; we log its decision as a standalone event.
            choice = _route_shot(shot, manifest, capabilities, run_mode=run_mode)
            _project_routing(shot, choice)
            last_id = _write_router_event(events, shot["shot_id"], choice)
            save_manifest_atomic(manifest_path, manifest, last_event_id=last_id)

            # PromptSmith — dispatch forwards ctx as the adapter's payload and
            # contracts read revision/creative_driven from the same dict. Keep
            # both false for initial-prompt passes.
            provider_def = capabilities.providers.get(choice.provider_id, {})
            ps_ctx: dict[str, Any] = {
                "shot": shot,
                "routing": _routing_view(shot["routing"], provider_def),
                "brief": manifest["brief"],
                "revision": False,
                "creative_driven": False,
            }
            ps_result = dispatch(
                agent="prompt_smith",
                shot_id=shot["shot_id"],
                manifest=manifest,
                ctx=ps_ctx,
                tool=prompt_smith,
                events=events,
            )
            _project_prompt(shot, ps_result.result)
            _append_history(
                shot,
                event="prompt_authored",
                by="prompt_smith",
                detail=f"primary={len(ps_result.result['primary'])}ch",
            )
            shot["status"] = "prompted"
            save_manifest_atomic(
                manifest_path, manifest, last_event_id=ps_result.result_event_id
            )
            _accumulate_usage(ps_usage_totals, ps_result.result.get("usage") or {})

    return _build_summary(
        manifest=manifest,
        manifest_path=manifest_path,
        screenwriter_result=sw_result.result,
        prompt_smith_usage=ps_usage_totals,
    )


# ---------------------------------------------------------------------------
# Brief + manifest seeding
# ---------------------------------------------------------------------------


def _load_brief(path: Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"brief not found at {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BriefError(f"brief is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, Mapping):
        raise BriefError("brief must be a JSON object")
    missing = [f for f in REQUIRED_BRIEF_FIELDS if f not in data]
    if missing:
        raise BriefError(f"brief missing required fields: {missing}")
    if not isinstance(data["tone"], list) or not data["tone"]:
        raise BriefError("brief.tone must be a non-empty list")
    if float(data["target_duration_s"]) <= 0:
        raise BriefError("brief.target_duration_s must be > 0")
    return dict(data)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _relative_source_path(path: Path) -> str:
    s = str(path)
    if not (s.startswith("/") or s.startswith("~")):
        return s
    return os.path.relpath(s)


def _derive_project_id(brief: Mapping[str, Any]) -> str:
    raw = str(brief.get("project_id") or "")
    if raw:
        if not raw.startswith("proj_"):
            raw = f"proj_{raw}"
        return raw.lower().replace("-", "_")
    return f"proj_{uuid.uuid4().hex[:8]}"


def _seed_manifest(
    brief: Mapping[str, Any], brief_path: Path
) -> dict[str, Any]:
    ts = _now_iso()
    manifest_brief: dict[str, Any] = {
        "logline": brief["logline"],
        "target_duration_s": float(brief["target_duration_s"]),
        "tone": list(brief["tone"]),
        "genre": brief["genre"],
        # Schema requires a relative path (no leading '/' or '~'). An absolute
        # user-supplied path gets relativized against CWD; os.path.relpath
        # always returns a valid relative path (possibly with ../ prefixes).
        "source_path": _relative_source_path(brief_path),
    }
    if brief.get("artistic_style"):
        manifest_brief["artistic_style"] = brief["artistic_style"]
    if "allow_artistic_experiments" in brief:
        manifest_brief["allow_artistic_experiments"] = bool(
            brief["allow_artistic_experiments"]
        )

    manifest: dict[str, Any] = {
        "manifest_version": "1.0",
        "project_id": _derive_project_id(brief),
        "created_at": ts,
        "updated_at": ts,
        "brief": manifest_brief,
        "script": {"status": "draft", "version": 1, "path": "artifacts/script.json"},
        "shots": [],
        "audio": {"dialogue": [], "sfx": []},
        "edit": {"status": "pending", "renderer": "hyperframes"},
        "budget": {
            "cap_usd": DEFAULT_CAP_USD,
            "spent_usd": 0.0,
            "by_provider": {},
            "alibaba_quota_remaining": DEFAULT_ALIBABA_QUOTA,
            "elevenlabs_credits_remaining": DEFAULT_ELEVENLABS_CREDITS,
        },
        "run_state": {"current_stage": "script", "last_event_id": 0, "resumable": True},
        "creative_decisions": [],
    }
    try:
        validate_manifest(manifest)
    except Exception as exc:
        raise BriefError(f"seeded manifest failed schema validation: {exc}") from exc
    return manifest


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def _project_screenwriter(
    manifest: dict[str, Any], sw_result: Mapping[str, Any]
) -> None:
    """Project Screenwriter output into manifest.shots[].

    Dialogue from the Screenwriter output is intentionally NOT projected — the
    schema's `audio.dialogue[]` items require voice_id/audio_path/duration_s
    that only the Audio Agent produces. The dialogue lines are preserved in
    the `dispatch_result` event payload for Audio Agent to read on its turn.
    """
    shots_in = list(sw_result.get("shots") or [])
    shots_out: list[dict[str, Any]] = []
    ts = _now_iso()
    for i, s in enumerate(shots_in, start=1):
        order = int(s.get("order", i))
        shots_out.append(
            {
                "shot_id": f"sh_{order:03d}",
                "scene": int(s["scene"]),
                "order": order,
                "description": str(s["description"]).strip(),
                "duration_s": float(s["duration_s"]),
                "has_humans": bool(s["has_humans"]),
                "is_hero": bool(s["is_hero"]),
                "motion_level": str(s["motion_level"]),
                "continuity_refs": [str(r) for r in (s.get("continuity_refs") or [])],
                "prompt": {"authored_by": "pending", "primary": "pending"},
                "routing": {
                    "chosen_provider": "pending",
                    "chosen_model": "pending",
                    "rationale": "pending",
                    "decided_by": "pending",
                    "decided_at": ts,
                    "alternates": [],
                },
                "attempts": [],
                "status": "created",
                "history": [
                    {
                        "ts": ts,
                        "event": "shot_created",
                        "by": "screenwriter",
                        "detail": f"scene={s['scene']} duration={s['duration_s']}",
                    }
                ],
                "judge_feedback": [],
                "creative_feedback": [],
                # Audio scaffolding — orchestrator's audio phase consumes
                # audio_cues after the shot approves. Empty list means the
                # operator hasn't hand-authored any cues yet (Screenwriter
                # doesn't populate this in v1; the prompt update is a
                # post-submission follow-up). audio_status="pending" until
                # the orchestrator's audio phase decides ok/partial/failed/skipped.
                "audio_cues": list(s.get("audio_cues") or []),
                "audio_status": "pending",
            }
        )
    manifest["shots"] = shots_out


def _project_routing(shot: dict[str, Any], choice: ProviderChoice) -> None:
    shot["routing"] = {
        "chosen_provider": choice.provider_id,
        "chosen_model": choice.model_id,
        "rationale": choice.rationale,
        "decided_by": "router",
        "decided_at": _now_iso(),
        "alternates": list(choice.alternates),
    }
    _append_history(
        shot,
        event="routed",
        by="router",
        detail=f"{choice.provider_id} est=${choice.estimated_cost_usd:.4f}",
    )


def _project_prompt(shot: dict[str, Any], ps_result: Mapping[str, Any]) -> None:
    prompt: dict[str, Any] = {
        "authored_by": "prompt_smith",
        "primary": str(ps_result["primary"]),
    }
    negative = str(ps_result.get("negative") or "")
    if negative:
        prompt["negative"] = negative
    refs = list(ps_result.get("reference_image_paths") or [])
    if refs:
        prompt["reference_image_paths"] = refs
    shot["prompt"] = prompt


def _append_history(
    shot: dict[str, Any], *, event: str, by: str, detail: str = ""
) -> None:
    entry: dict[str, Any] = {"ts": _now_iso(), "event": event, "by": by}
    if detail:
        entry["detail"] = detail
    shot.setdefault("history", []).append(entry)


def _routing_view(
    routing: Mapping[str, Any], provider_def: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the routing dict passed to PromptSmith. Carries the manifest
    fields plus capability hints the system prompt expects."""
    out: dict[str, Any] = {
        "chosen_provider": routing["chosen_provider"],
        "chosen_model": routing["chosen_model"],
    }
    for key in ("supports_first_last_frame", "max_reference_images", "max_duration_s"):
        if key in provider_def:
            out[key] = provider_def[key]
    caps = provider_def.get("capabilities") or {}
    if "negative_prompt_support" in caps:
        out["supports_negative_prompt"] = bool(caps["negative_prompt_support"])
    if "image_conditioning" in caps:
        out["supports_reference_images"] = bool(caps["image_conditioning"])
    return out


# ---------------------------------------------------------------------------
# Router invocation
# ---------------------------------------------------------------------------


def _route_shot(
    shot: Mapping[str, Any],
    manifest: Mapping[str, Any],
    capabilities,
    *,
    run_mode: str = "testing",
) -> ProviderChoice:
    prompt = shot.get("prompt") or {}
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
        reference_subject_count=len(prompt.get("reference_subject_paths") or []),
        has_end_frame="end_frame_path" in prompt,
        run_mode=run_mode,
    )
    b = manifest.get("budget", {})
    budget = BudgetState(
        cap_usd=float(b.get("cap_usd", 0.0)),
        spent_usd=float(b.get("spent_usd", 0.0)),
        by_provider=dict(b.get("by_provider", {})),
        alibaba_quota_remaining=int(b.get("alibaba_quota_remaining", 0)),
        elevenlabs_credits_remaining=int(b.get("elevenlabs_credits_remaining", 0)),
    )
    return router_engine.route(spec, budget, capabilities)


def _write_router_event(
    events: EventLog, shot_id: str, choice: ProviderChoice
) -> int:
    """Router is a sync worker, not dispatched through `dispatch()`. We still
    log each decision for the audit trail — `router_decision` is a distinct
    kind so `events tail` can distinguish it from agent dispatch results."""
    return events.write(
        "router_decision",
        agent="router",
        shot_id=shot_id,
        payload={
            "provider_id": choice.provider_id,
            "model_id": choice.model_id,
            "estimated_cost_usd": choice.estimated_cost_usd,
            "rationale": choice.rationale,
            "alternates": list(choice.alternates),
        },
    )


# ---------------------------------------------------------------------------
# Dry-run stub client
# ---------------------------------------------------------------------------


class _StubClient:
    """Deterministic LLMClient for --dry-run. Branches on system prompt content.

    Produces:
        - Screenwriter: an 8-shot list summing to the target duration (±5%).
        - PromptSmith: a templated prompt combining provider+description.

    Intent: smoke-test the driver end-to-end without spending credits and
    without network. No attempt at linguistic plausibility — this is a
    mechanical fixture, not a generative model.
    """

    def create_message(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ):
        from src.producer.llm import LLMResponse

        sys_text = _coerce_system_text(system)
        user_text = messages[-1]["content"] if messages else ""
        if "Screenwriter" in sys_text:
            text = _stub_screenwriter_json(user_text)
        elif "PromptSmith" in sys_text:
            text = _stub_prompt_smith_json(user_text)
        else:
            text = "{}"
        return LLMResponse(
            text=text,
            model=model or "stub",
            usage={"input_tokens": 0, "output_tokens": 0},
        )


def _coerce_system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, Mapping):
                t = block.get("text", "")
                if t:
                    parts.append(str(t))
        return "\n".join(parts)
    return ""


def _stub_screenwriter_json(user_text: str) -> str:
    target = _parse_target_duration(user_text) or 30.0
    n = 8
    per = max(1.5, min(round(target / n, 2), 8.0))
    shots: list[dict[str, Any]] = []
    for i in range(n):
        order = i + 1
        shots.append(
            {
                "scene": 1 if order <= 4 else 2,
                "order": order,
                "description": f"Stub shot {order}: placeholder frame description.",
                "duration_s": per,
                "has_humans": order % 2 == 0,
                "is_hero": order in (1, 4, 7),
                "motion_level": "low" if order % 3 != 0 else "medium",
                "continuity_refs": [],
                "dialogue": [],
            }
        )
    # Nudge the last shot so the total lands within ±5% of target.
    total = sum(float(s["duration_s"]) for s in shots)
    adj = round(target - total, 2)
    shots[-1]["duration_s"] = round(
        max(1.5, min(8.0, float(shots[-1]["duration_s"]) + adj)), 2
    )
    return json.dumps(shots)


def _stub_prompt_smith_json(user_text: str) -> str:
    provider = _parse_field(user_text, "chosen_provider") or "unknown"
    description = _parse_field(user_text, "description") or "stub"
    primary = f"[{provider}] {description}. Natural lighting, locked-off camera."
    return json.dumps({"primary": primary, "negative": "", "reference_image_paths": []})


def _parse_target_duration(text: str) -> float | None:
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- target_duration_s:"):
            try:
                return float(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _parse_field(text: str, key: str) -> str | None:
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(f"- {key}:"):
            return line.split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------


def _accumulate_usage(totals: dict[str, int], usage: Mapping[str, Any]) -> None:
    for k, v in usage.items():
        try:
            totals[k] = int(totals.get(k, 0)) + int(v)
        except (TypeError, ValueError):
            continue


def _build_summary(
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    screenwriter_result: Mapping[str, Any],
    prompt_smith_usage: Mapping[str, int],
) -> dict[str, Any]:
    providers: dict[str, int] = {}
    for s in manifest["shots"]:
        p = s["routing"]["chosen_provider"]
        providers[p] = providers.get(p, 0) + 1
    sw_summary = screenwriter_result.get("summary") or {}
    return {
        "project_id": manifest["project_id"],
        "manifest_path": str(manifest_path),
        "shot_count": len(manifest["shots"]),
        "total_duration_s": round(
            sum(float(s["duration_s"]) for s in manifest["shots"]), 3
        ),
        "target_duration_s": manifest["brief"]["target_duration_s"],
        "within_duration_bound": sw_summary.get("within_duration_bound", False),
        "hero_count": sw_summary.get("hero_count", 0),
        "providers": providers,
        "screenwriter_usage": dict(screenwriter_result.get("usage") or {}),
        "prompt_smith_usage": dict(prompt_smith_usage),
    }


def _print_pretty(summary: Mapping[str, Any]) -> None:
    print(f"project_id        {summary['project_id']}")
    print(f"manifest          {summary['manifest_path']}")
    print(
        f"shots             {summary['shot_count']} "
        f"(target {summary['target_duration_s']}s, "
        f"actual {summary['total_duration_s']}s, "
        f"{'within' if summary['within_duration_bound'] else 'OUTSIDE'} ±5%)"
    )
    print(f"hero shots        {summary['hero_count']}")
    print("providers:")
    for p, n in sorted(summary["providers"].items(), key=lambda kv: -kv[1]):
        print(f"  {p:<30} {n}")
    sw = summary.get("screenwriter_usage") or {}
    ps = summary.get("prompt_smith_usage") or {}
    if sw or ps:
        print("usage:")
        if sw:
            print(
                f"  screenwriter       in={sw.get('input_tokens', 0)} "
                f"out={sw.get('output_tokens', 0)} "
                f"cache_read={sw.get('cache_read_input_tokens', 0)}"
            )
        if ps:
            print(
                f"  prompt_smith (sum) in={ps.get('input_tokens', 0)} "
                f"out={ps.get('output_tokens', 0)} "
                f"cache_read={ps.get('cache_read_input_tokens', 0)}"
            )
