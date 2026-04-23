"""`rectoverso revise --shot <id>` — rewrite a rejected shot's prompt.

Closes the iteration cycle: Shot Judge rejected an attempt → PromptSmith reads
the judge's notes and revises the prompt → operator re-runs `rectoverso render`
against the same shot, now with the rewritten prompt.

Flow per invocation:

    1. Load manifest + sanity-check: shot.status == "rejected", attempts[-1]
       has outcome=rejected with non-empty judge_notes.
    2. Stamp attempts[-1].prompt_revision = shot.prompt.primary (if missing)
       so PromptSmith sees the exact text the judge was evaluating.
    3. Dispatch PromptSmithTool with ctx.revision=True. Contract 2
       (shot_judge -> prompt_smith) runs inside dispatch and re-verifies the
       judge_notes invariant.
    4. Project the returned prompt into shots[i].prompt (overwriting primary,
       negative, reference_image_paths). Append a "prompt_revised" history
       entry. Leave shot.status as "rejected" — render_cmd accepts rejected
       as a retry state, so the next `rectoverso render` picks it up.
    5. Save manifest atomically.

Explicit non-goals for this first pass:
    - Not auto-chaining render + judge. That's for a later `--auto` flag or
      a wrapping driver.
    - Not bumping the router — the shot keeps its existing provider.
      Re-routing after repeated failures is a separate concern.
    - Not handling status=escalated. Escalated means human review; re-running
      PromptSmith against the same judge_notes won't change the outcome.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.contracts import ContractViolation
from src.producer import (
    DispatchFailure,
    PromptSmithTool,
    dispatch,
    load_manifest,
    open_event_log,
    save_manifest_atomic,
)


def add_subparser(subparsers: "argparse._SubParsersAction[Any]") -> None:
    p = subparsers.add_parser(
        "revise",
        help="Re-author a rejected shot's prompt using Shot Judge's feedback",
        description=(
            "Dispatch PromptSmith with revision=True against a shot whose most "
            "recent attempt was rejected. Writes a rewritten prompt back into "
            "the manifest so the next `rectoverso render` picks up the new text."
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
    p.add_argument("--json", action="store_true", help="emit JSON summary instead of pretty")
    p.set_defaults(func=cmd_revise)


def cmd_revise(args: argparse.Namespace) -> int:
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

    if shot["status"] != "rejected":
        print(
            f"error: shot {args.shot} is in status {shot['status']!r}; "
            "revise expects status == 'rejected'. Run `rectoverso judge` first.",
            file=sys.stderr,
        )
        return 9

    attempts = shot.get("attempts") or []
    if not attempts:
        print(f"error: shot {args.shot} has no attempts to revise against", file=sys.stderr)
        return 9

    last_attempt = attempts[-1]
    if last_attempt.get("outcome") != "rejected":
        print(
            f"error: attempts[-1].outcome is "
            f"{last_attempt.get('outcome')!r} (expected 'rejected')",
            file=sys.stderr,
        )
        return 9
    judge_notes = (last_attempt.get("judge_notes") or "").strip()
    if not judge_notes:
        print(
            f"error: attempts[-1].judge_notes is empty — "
            "PromptSmith has no signal to revise on. Re-run `rectoverso judge`.",
            file=sys.stderr,
        )
        return 9

    # Stamp the exact prompt that was rendered+rejected, so PromptSmith's
    # `prior primary` context reflects what the judge actually evaluated.
    # Only set it if missing — never overwrite a prior stamp.
    if "prompt_revision" not in last_attempt:
        prior_primary = (shot.get("prompt") or {}).get("primary") or ""
        if prior_primary:
            last_attempt["prompt_revision"] = prior_primary

    # Build routing view the same way run.py does. We don't have capabilities
    # loaded here, but PromptSmith's user payload only requires the chosen
    # provider/model — capability hints are optional.
    routing = shot.get("routing") or {}
    routing_view: dict[str, Any] = {
        "chosen_provider": routing.get("chosen_provider", ""),
        "chosen_model": routing.get("chosen_model", ""),
    }

    # Leave client=None — PromptSmithTool's call_json lazy-resolves default_client
    # only when the real HTTP call fires. Keeps tests trivial to monkeypatch.
    tool = PromptSmithTool()
    ctx: dict[str, Any] = {
        "shot": shot,
        "routing": routing_view,
        "brief": manifest.get("brief") or {},
        "revision": True,
        "creative_driven": False,
    }

    args.events_db.parent.mkdir(parents=True, exist_ok=True)

    if not args.json:
        prior = (shot.get("prompt") or {}).get("primary", "")
        print(f"[revise] shot {args.shot}  attempt #{last_attempt['attempt_id']} rejected")
        print(f"[revise] prior prompt: {prior[:100]}{'...' if len(prior) > 100 else ''}")
        print(f"[revise] judge_notes: {judge_notes[:120]}{'...' if len(judge_notes) > 120 else ''}")
        print(f"[revise] dispatching PromptSmith (revision=True); 5-15s typical")

    with open_event_log(args.events_db) as events:
        try:
            result = dispatch(
                agent="prompt_smith",
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

    ps_out = dict(result.result)

    # Project the new prompt into shots[i].prompt. Overwrite fully — the
    # revision replaces the prior text, not patches it.
    new_prompt: dict[str, Any] = {
        "authored_by": "prompt_smith",
        "primary": str(ps_out["primary"]),
    }
    neg = str(ps_out.get("negative") or "").strip()
    if neg:
        new_prompt["negative"] = neg
    refs = list(ps_out.get("reference_image_paths") or [])
    if refs:
        new_prompt["reference_image_paths"] = refs
    shot["prompt"] = new_prompt

    ts = _now_iso()
    shot.setdefault("history", []).append(
        {
            "ts": ts,
            "event": "prompt_revised",
            "by": "prompt_smith",
            "detail": (
                f"attempt {last_attempt['attempt_id']} rejected; "
                f"new primary={len(new_prompt['primary'])}ch"
            ),
        }
    )

    save_manifest_atomic(args.manifest_path, manifest, last_event_id=final_event_id)

    summary = {
        "shot_id": args.shot,
        "attempt_rejected": last_attempt["attempt_id"],
        "new_primary_length": len(new_prompt["primary"]),
        "has_negative": "negative" in new_prompt,
        "reference_image_paths": new_prompt.get("reference_image_paths", []),
        "model": ps_out.get("model"),
        "usage": ps_out.get("usage") or {},
        "shot_status": shot["status"],
        "next_action": f"rectoverso render --shot {args.shot}",
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print()
        print(f"[revise] REVISED  primary={summary['new_primary_length']}ch")
        print(f"[revise] new prompt: {new_prompt['primary'][:200]}{'...' if len(new_prompt['primary']) > 200 else ''}")
        if neg:
            print(f"[revise] negative: {neg[:120]}{'...' if len(neg) > 120 else ''}")
        print(f"[revise] shot status -> {summary['shot_status']} (ready for re-render)")
        print(f"[revise] next: {summary['next_action']}")
    return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
