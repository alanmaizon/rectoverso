"""`rectoverso generate-ref --shot <id>` — generate a reference image for a shot.

Closes the Day-4 gap surfaced on Day-3: Kling 2.1 is I2V-only, and a humanless
start frame produces a humanless output even if the prompt asks for a subject.
Feeding Kling a reference that ALREADY contains the subject fixes this.

Flow per invocation:

    1. Load manifest, find the shot. Refuse if the shot has no description/prompt.
    2. Compose a STILL-FRAME image prompt from the shot + brief. This differs
       from the video prompt (which describes motion); the image prompt has
       to describe one decisive frame.
    3. Dispatch QwenImageTool (Tool Protocol, no pair contracts — image
       generation has no upstream agent whose output it depends on yet).
    4. Save PNG to `artifacts/refs/{shot_id}_v{n}.png`.
    5. Append the new path to `shot.prompt.reference_subject_paths[]` so the
       next `rectoverso render` picks it up via _kling_image_url.
    6. History entry `reference_generated`.

Design note — not updating the video prompt here. This command is additive:
it supplies a reference image; the existing video prompt stays as-is. A
separate `rectoverso revise` can rewrite the video prompt if needed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.contracts import ContractViolation
from src.producer import (
    DispatchFailure,
    NanoBananaImageTool,
    QwenImageTool,
    dispatch,
    load_manifest,
    open_event_log,
    save_manifest_atomic,
)


PROVIDER_CHOICES = ("qwen", "nano-banana", "auto")


def _build_tool(provider: str):
    """Instantiate the image-gen adapter matching the chosen provider.

    All image-gen adapters share Tool Protocol `name="image_generator"` and
    payload shape (prompt/negative_prompt/aspect_ratio/seed/output_dir/
    attempt_id), so the dispatch call site is identical.
    """
    if provider == "qwen":
        return QwenImageTool()
    if provider == "nano-banana":
        return NanoBananaImageTool()
    raise ValueError(f"unknown image provider: {provider!r}")


def add_subparser(subparsers: "argparse._SubParsersAction[Any]") -> None:
    p = subparsers.add_parser(
        "generate-ref",
        help="Generate a reference image for a shot via DashScope Qwen-Image",
        description=(
            "Compose an image prompt from the shot description + brief style, "
            "call Qwen-Image via DashScope (async), save the PNG under "
            "artifacts/refs/, and append the path to "
            "shot.prompt.reference_subject_paths so the next render picks it up. "
            "Typical use: unblocking Kling I2V shots that have no subject-in-frame."
        ),
    )
    p.add_argument("--shot", required=True, help="shot_id, e.g. sh_006")
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
        default=Path("artifacts/refs"),
        help="directory for generated reference images (default: artifacts/refs)",
    )
    p.add_argument(
        "--aspect-ratio",
        choices=("16:9", "4:3", "1:1", "3:4", "9:16"),
        default="16:9",
        help="image aspect ratio (default 16:9 for video reference)",
    )
    p.add_argument("--seed", type=int, help="seed for reproducibility (Qwen only — Gemini ignores)")
    p.add_argument(
        "--prompt-override",
        help="use this image prompt verbatim instead of composing from the shot",
    )
    p.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default="auto",
        help=(
            "which image gen to use. 'qwen' (DashScope Qwen-Image-Plus, free quota), "
            "'nano-banana' (Gemini 2.5 flash image, ~$0.04/image), or 'auto' "
            "(try Qwen first, fall back to nano-banana on content_policy). Default: auto."
        ),
    )
    p.add_argument("--json", action="store_true", help="emit JSON summary")
    p.set_defaults(func=cmd_generate_ref)


def cmd_generate_ref(args: argparse.Namespace) -> int:
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

    description = (shot.get("description") or "").strip()
    if not description:
        print(
            f"error: shot {args.shot} has no description; pass --prompt-override "
            "to bypass prompt composition",
            file=sys.stderr,
        )
        return 9

    brief = manifest.get("brief") or {}
    image_prompt = (
        args.prompt_override.strip()
        if args.prompt_override
        else _compose_image_prompt(description, brief, shot)
    )

    output_dir = args.output_root
    attempt_id = _next_ref_attempt_id(shot)

    ctx: dict[str, Any] = {
        "prompt": image_prompt,
        "negative_prompt": _default_negatives(brief),
        "aspect_ratio": args.aspect_ratio,
        "output_dir": output_dir,
        "attempt_id": attempt_id,
    }
    if args.seed is not None:
        ctx["seed"] = args.seed

    args.events_db.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Provider routing:
    #   auto:        qwen first, on content_policy fall through to nano-banana
    #   qwen:        qwen only
    #   nano-banana: nano-banana only
    provider_plan = (
        ["qwen", "nano-banana"] if args.provider == "auto" else [args.provider]
    )

    if not args.json:
        print(f"[generate-ref] shot {args.shot}  aspect={args.aspect_ratio}  provider={args.provider}")
        print(f"[generate-ref] image prompt: {image_prompt[:120]}{'...' if len(image_prompt) > 120 else ''}")
        print(f"[generate-ref] dispatching {provider_plan[0]}; 5-15s typical")

    tool_out, final_event_id, _plan_slot, fallback_plan_slot = _dispatch_with_fallback(
        provider_plan=provider_plan,
        shot_id=args.shot,
        manifest=manifest,
        ctx=ctx,
        events_db=args.events_db,
        verbose=not args.json,
    )
    if tool_out is None:
        # Contract or DispatchFailure already reported to stderr
        return 5

    # Prefer the adapter's self-reported provider name ("gemini_nano_banana",
    # "dashscope_qwen_image") over the CLI plan slot ("nano-banana", "qwen")
    # for audit rows — it's more specific and survives flag renames.
    provider_used = tool_out.get("provider") or _plan_slot
    fallback_from = _fallback_from_adapter_name(fallback_plan_slot)
    status = tool_out.get("status")

    if status != "ok":
        summary = {
            "shot_id": args.shot,
            "status": status,
            "provider": provider_used,
            "failure_stage": tool_out.get("failure_stage"),
            "stderr_tail": tool_out.get("stderr_tail", ""),
            "fallback_from": fallback_from,
        }
        ts = _now_iso()
        shot.setdefault("history", []).append({
            "ts": ts,
            "event": "reference_failed",
            "by": provider_used or "image_generator",
            "detail": tool_out.get("failure_stage", "unknown"),
        })
        save_manifest_atomic(args.manifest_path, manifest, last_event_id=final_event_id)
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            print(f"[generate-ref] FAILED  provider={provider_used}  stage={summary['failure_stage']}")
            if fallback_from:
                print(f"[generate-ref] fell back from {fallback_from}")
            print(f"[generate-ref] stderr: {summary['stderr_tail'][:200]}")
        return 11

    # Success — project the reference path into shot.prompt.reference_subject_paths
    image_path_rel = _relative_to_cwd(tool_out["image_path"])
    prompt_block = shot.setdefault("prompt", {})
    refs = list(prompt_block.get("reference_subject_paths") or [])
    if image_path_rel not in refs:
        refs.append(image_path_rel)
    prompt_block["reference_subject_paths"] = refs

    ts = _now_iso()
    detail = (
        f"size={tool_out.get('size')} "
        f"md5={tool_out.get('image_md5', '')[:8]} "
        f"attempt_id={attempt_id} provider={provider_used}"
    )
    if fallback_from:
        detail += f" (fallback_from={fallback_from})"
    shot.setdefault("history", []).append({
        "ts": ts,
        "event": "reference_generated",
        "by": provider_used or "image_generator",
        "detail": detail,
    })

    save_manifest_atomic(args.manifest_path, manifest, last_event_id=final_event_id)

    summary = {
        "shot_id": args.shot,
        "status": "ok",
        "provider": provider_used,
        "fallback_from": fallback_from,
        "image_path": image_path_rel,
        "image_md5": tool_out.get("image_md5"),
        "output_size_bytes": tool_out.get("output_size_bytes"),
        "latency_s": tool_out.get("latency_s"),
        "size": tool_out.get("size"),
        "task_id": tool_out.get("task_id"),
        "cost_usd": tool_out.get("cost_usd", 0.0),
        "reference_count": len(refs),
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print()
        print(
            f"[generate-ref] OK  provider={provider_used}  "
            f"latency={summary['latency_s']}s  cost=${summary['cost_usd']:.3f}"
        )
        if fallback_from:
            print(f"[generate-ref] fell back from {fallback_from}")
        print(f"[generate-ref] saved to {summary['image_path']}")
        print(f"[generate-ref] md5 {summary['image_md5']}")
        print(f"[generate-ref] shot.prompt.reference_subject_paths has {summary['reference_count']} entry")
        print(f"[generate-ref] next: rectoverso render --shot {args.shot}")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dispatch_with_fallback(
    *,
    provider_plan: list[str],
    shot_id: str,
    manifest: Mapping[str, Any],
    ctx: Mapping[str, Any],
    events_db: Path,
    verbose: bool,
) -> tuple[dict | None, int, str | None, str | None]:
    """Dispatch the image generator against each provider in `provider_plan`
    until one returns status=ok OR we exhaust the plan. Fallback is ONLY
    triggered on `failure_stage == "content_policy"` — any other failure is
    returned immediately (same prompt will fail the other provider too for
    most transient / auth / validation issues).

    Returns (tool_out, last_event_id, provider_used, fallback_from).
    provider_used is the one whose result we returned; fallback_from is the
    earlier provider that tripped content policy, or None.
    tool_out is None ONLY if the dispatch itself raised (Contract/DispatchFailure);
    those errors are already printed to stderr.
    """
    last_event_id = 0
    tool_out: dict | None = None
    provider_used: str | None = None
    fallback_from: str | None = None

    with open_event_log(events_db) as events:
        for i, provider in enumerate(provider_plan):
            try:
                tool = _build_tool(provider)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return None, last_event_id, provider, fallback_from

            try:
                result = dispatch(
                    agent="image_generator",
                    shot_id=shot_id,
                    manifest=manifest,
                    ctx=ctx,
                    tool=tool,
                    events=events,
                )
            except ContractViolation as exc:
                print(f"contract block: {exc}", file=sys.stderr)
                return None, last_event_id, provider, fallback_from
            except DispatchFailure as exc:
                print(f"dispatch failed: {exc}", file=sys.stderr)
                return None, last_event_id, provider, fallback_from

            last_event_id = result.result_event_id
            tool_out = dict(result.result)
            provider_used = provider

            status = tool_out.get("status")
            stage = tool_out.get("failure_stage", "")

            if status == "ok":
                return tool_out, last_event_id, provider, fallback_from

            # Only fall through on content-policy failures, and only if there's
            # another provider queued up.
            can_fall_through = (
                stage == "content_policy"
                and i + 1 < len(provider_plan)
            )
            if can_fall_through:
                fallback_from = provider
                if verbose:
                    next_provider = provider_plan[i + 1]
                    print(
                        f"[generate-ref] {provider} refused (content_policy); "
                        f"falling back to {next_provider}"
                    )
                continue
            # Non-content-policy failure, or exhausted plan — return as-is.
            return tool_out, last_event_id, provider, fallback_from

    return tool_out, last_event_id, provider_used, fallback_from


def _fallback_from_adapter_name(plan_slot: str | None) -> str | None:
    """Translate a CLI plan-slot name (as used in `--provider`) to the
    adapter's self-reported provider name, so history rows are consistent
    with the `by` field whether the fallback happened or not."""
    if plan_slot is None:
        return None
    return {
        "qwen": "dashscope_qwen_image",
        "nano-banana": "gemini_nano_banana",
    }.get(plan_slot, plan_slot)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _relative_to_cwd(path_str: str) -> str:
    """Schema's relativePath pattern forbids leading '/' or '~'."""
    import os
    return os.path.relpath(path_str, start=Path.cwd())


def _next_ref_attempt_id(shot: Mapping[str, Any]) -> int:
    """Count how many prior reference_generated events exist and return n+1.
    Ensures generated files are unique across retries."""
    history = shot.get("history") or []
    prior = sum(1 for h in history if h.get("event") == "reference_generated")
    return prior + 1


def _compose_image_prompt(
    description: str, brief: Mapping[str, Any], shot: Mapping[str, Any]
) -> str:
    """Build a still-frame image prompt from the shot description + brief style.

    The shot's video prompt describes motion ("camera pushes in slowly") which
    makes no sense for a still image. This helper extracts the spatial/subject
    content and composes it with the brief's color/style anchors.
    """
    parts: list[str] = []
    parts.append(f"A single cinematic still frame: {description}")

    style_bits: list[str] = []
    if brief.get("artistic_style"):
        style_bits.append(str(brief["artistic_style"]))
    tone = brief.get("tone") or []
    if tone:
        style_bits.append(", ".join(str(t) for t in tone))
    if style_bits:
        parts.append(" ".join(style_bits) + ".")

    # Composition anchors — Qwen-Image benefits from explicit framing hints.
    parts.append("Medium shot, centered subject, shallow depth of field.")
    parts.append("Naturalistic diffused light, high detail, photographic composition.")

    # Artistic direction layer (if present on the shot itself).
    if shot.get("artistic_direction"):
        parts.append(str(shot["artistic_direction"]))

    return " ".join(parts)


def _default_negatives(brief: Mapping[str, Any]) -> str:
    """Consistent negatives for our brief profile. Callers can override via
    shot.prompt.negative but this gives a reasonable floor."""
    negatives = [
        "low resolution",
        "blurry",
        "distorted",
        "watermark",
        "text",
        "logo",
        "extra fingers",
        "bad anatomy",
    ]
    tone = [str(t).lower() for t in (brief.get("tone") or [])]
    style = str(brief.get("artistic_style") or "").lower()
    if "cold" in style or "naturalistic" in style:
        negatives.extend(["oversaturated", "warm sunlight", "studio lighting"])
    return ", ".join(negatives)
