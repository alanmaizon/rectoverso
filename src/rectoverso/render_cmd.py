"""`rectoverso render --shot <id>` — dispatch a single shot through its router-chosen renderer.

Scoped narrowly in this Day-3 first pass: **Alibaba Wan only** (free quota,
$0 USD risk). Kling (fal.ai) and Veo (Vertex) need their own adapters; this
command refuses them with a clear error so we don't accidentally spend paid
budget before those adapters exist.

Flow per invocation:

    1. Load manifest + sanity-check the shot is eligible (status, provider).
    2. Pre-flight: budget check + contract validation.
    3. Record a new attempts[] row in `rendering` state; save manifest.
    4. dispatch() the WanRendererTool through the Producer runtime (events +
       contract gate run inside dispatch).
    5. Project result into attempts[-1] (render_path, md5, size, latency,
       outcome, approved_by omitted — Shot Judge hasn't run).
    6. Transition shot status to `judging`.
    7. record_spend() on the budget (quota only for Wan; no USD).

Explicit non-goals for this first pass:
    - Not auto-invoking Shot Judge after the render. The shot lands in
      `judging` status and waits.
    - Not retrying on failure. A single attempt per invocation; the operator
      re-runs the command to retry.
    - Not touching Kling / Veo providers. Out of scope until their adapters land.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.contracts import ContractViolation
from src.producer import (
    DispatchFailure,
    WanRendererTool,
    check_before_render,
    dispatch,
    load_manifest,
    open_event_log,
    record_spend,
    save_manifest_atomic,
)


SUPPORTED_PROVIDERS = ("alibaba_wan_2_7_plus", "alibaba_wan_2_7_turbo")


def add_subparser(subparsers: "argparse._SubParsersAction[Any]") -> None:
    p = subparsers.add_parser(
        "render",
        help="Dispatch a single shot to its router-chosen renderer (Wan only in v0.2)",
        description=(
            "Submit the named shot to its chosen provider, poll for completion, "
            "download the MP4, and update the manifest. Currently only Alibaba "
            "Wan is supported — Kling/Veo require their own adapters."
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
        "--output-root",
        type=Path,
        default=Path("artifacts/renders"),
        help="root directory for rendered MP4s (default: artifacts/renders)",
    )
    p.add_argument(
        "--resolution",
        choices=("720p", "1080p"),
        default="720p",
        help="Wan render resolution (default: 720p to conserve quota)",
    )
    p.add_argument("--json", action="store_true", help="emit JSON summary instead of pretty")
    p.set_defaults(func=cmd_render)


def cmd_render(args: argparse.Namespace) -> int:
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

    provider_id = shot["routing"]["chosen_provider"]
    if provider_id not in SUPPORTED_PROVIDERS:
        print(
            f"error: provider {provider_id!r} not yet supported by `rectoverso render` "
            f"(supported: {', '.join(SUPPORTED_PROVIDERS)}). "
            "Kling/Veo adapters will land in a later pass.",
            file=sys.stderr,
        )
        return 8

    # Status gate — refuse to render from non-prompted states so we don't
    # stomp on an in-flight attempt or a shot that hasn't been set up yet.
    if shot["status"] not in ("prompted", "routed", "rejected"):
        print(
            f"error: shot {args.shot} is in status {shot['status']!r}; "
            "render expects one of: prompted, routed, rejected",
            file=sys.stderr,
        )
        return 9

    # Pre-flight budget (Wan is quota-metered; no USD but we still project).
    check = check_before_render(
        manifest,
        provider_id=provider_id,
        estimated_cost_usd=0.0,
        estimated_quota_cost=1,
    )
    if not check.allowed:
        print(f"budget refused: {check.rationale}", file=sys.stderr)
        return 10

    # Scaffold the new attempt row in `rendering` state BEFORE dispatch, so a
    # crash mid-dispatch is recoverable from the manifest alone.
    attempt_id = len(shot["attempts"]) + 1
    now = _now_iso()
    shot["attempts"].append(
        {
            "attempt_id": attempt_id,
            "provider": provider_id,
            "started_at": now,
            "outcome": "pending",
        }
    )
    shot["status"] = "rendering"
    shot["history"].append(
        {"ts": now, "event": "rendering", "by": "renderer", "detail": f"attempt {attempt_id}"}
    )

    # Wan tool. dispatch() passes the ctx dict to the tool as its payload
    # argument, so the render payload must live INSIDE ctx (alongside the
    # `creative_driven` flag that the contract registry inspects).
    tool = WanRendererTool()
    ctx: dict[str, Any] = {
        "creative_driven": False,
        "model": shot["routing"]["chosen_model"],
        "prompt": shot["prompt"]["primary"],
        "negative_prompt": shot["prompt"].get("negative", ""),
        "duration_s": int(round(float(shot["duration_s"]))),
        "resolution": args.resolution,
        "output_dir": args.output_root / args.shot,
        "attempt_id": attempt_id,
    }

    args.events_db.parent.mkdir(parents=True, exist_ok=True)
    args.output_root.mkdir(parents=True, exist_ok=True)
    save_manifest_atomic(
        args.manifest_path, manifest, last_event_id=manifest["run_state"]["last_event_id"]
    )

    if not args.json:
        print(f"[render] shot {args.shot}  provider={provider_id}  model={shot['routing']['chosen_model']}")
        print(f"[render] prompt: {shot['prompt']['primary'][:100]}...")
        print(f"[render] submitting; this can take 1-5 minutes")

    with open_event_log(args.events_db) as events:
        try:
            result = dispatch(
                agent="renderer",
                shot_id=args.shot,
                manifest=manifest,
                ctx=ctx,
                tool=tool,
                events=events,
            )
        except ContractViolation as exc:
            # Roll back the scaffolded attempt — contract block means nothing
            # was actually sent to the provider.
            shot["attempts"].pop()
            shot["status"] = "prompted"
            save_manifest_atomic(
                args.manifest_path,
                manifest,
                last_event_id=events.last_event_id(),
            )
            print(f"contract block: {exc}", file=sys.stderr)
            return 5
        except DispatchFailure as exc:
            shot["attempts"][-1]["outcome"] = "failed"
            shot["attempts"][-1]["error"] = str(exc.cause)[:500]
            shot["status"] = "failed"
            shot["history"].append(
                {"ts": _now_iso(), "event": "dispatch_failed", "by": "renderer", "detail": str(exc.cause)[:200]}
            )
            save_manifest_atomic(
                args.manifest_path,
                manifest,
                last_event_id=events.last_event_id(),
            )
            print(f"dispatch failed: {exc}", file=sys.stderr)
            return 5

        final_event_id = result.result_event_id

    # Project the tool result into the manifest
    attempt = shot["attempts"][-1]
    tool_out = dict(result.result)
    attempt["completed_at"] = _now_iso()
    attempt["latency_s"] = float(tool_out.get("latency_s", 0.0))
    attempt["cost_usd"] = float(tool_out.get("cost_usd", 0.0))

    if tool_out.get("status") == "ok":
        attempt["outcome"] = "pending"  # awaiting Shot Judge
        # Schema requires relative paths (^(?!/|~)[^\0]+$). The tool returns
        # whatever absolute-or-relative path was assembled from --output-root;
        # normalize against cwd so the manifest is portable.
        attempt["render_path"] = _relative_to_cwd(tool_out["render_path"])
        shot["status"] = "judging"
        shot["history"].append(
            {"ts": _now_iso(), "event": "judging", "by": "renderer", "detail": f"attempt {attempt_id} rendered"}
        )
        # Record quota consumption on the budget.
        record_spend(
            manifest,
            provider_id=provider_id,
            actual_cost_usd=0.0,
            actual_quota=int(tool_out.get("quota_cost", 1)),
        )
    else:
        attempt["outcome"] = "failed"
        attempt["error"] = tool_out.get("stderr_tail") or tool_out.get("failure_stage", "")
        shot["status"] = "failed"
        shot["history"].append(
            {
                "ts": _now_iso(),
                "event": "render_failed",
                "by": "renderer",
                "detail": tool_out.get("failure_stage", "unknown"),
            }
        )

    save_manifest_atomic(args.manifest_path, manifest, last_event_id=final_event_id)

    summary = {
        "shot_id": args.shot,
        "provider": provider_id,
        "model": shot["routing"]["chosen_model"],
        "attempt_id": attempt_id,
        "status": tool_out.get("status"),
        "render_path": tool_out.get("render_path", ""),
        "render_md5": tool_out.get("render_md5"),
        "output_size_bytes": tool_out.get("output_size_bytes", 0),
        "latency_s": tool_out.get("latency_s"),
        "task_id": tool_out.get("task_id"),
        "shot_status": shot["status"],
        "manifest": str(args.manifest_path),
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print()
        verdict = "ok" if summary["status"] == "ok" else "FAIL"
        print(f"[render] {verdict}  latency={summary['latency_s']}s  bytes={summary['output_size_bytes']}")
        if summary["render_path"]:
            print(f"[render] saved to {summary['render_path']}")
            print(f"[render] md5 {summary['render_md5']}")
        print(f"[render] shot status -> {summary['shot_status']}")
    return 0 if tool_out.get("status") == "ok" else 11


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _relative_to_cwd(path_str: str) -> str:
    """Convert any path (absolute or relative) to a form the manifest schema
    accepts — relativePath pattern forbids a leading '/' or '~' but permits
    '..' segments. `os.path.relpath` always produces a schema-valid result."""
    return os.path.relpath(path_str, start=Path.cwd())
