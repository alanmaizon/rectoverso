"""Condition 2 probe — real tier-2 agent dispatch through our Producer runtime.

Satisfies the PROBE_REPORT.md condition: "have at least one real agent dispatch
(shot_judge or prompt_smith) producing an actual Claude API response through
our dispatch()."

What this script does:
    1. Load ANTHROPIC_API_KEY from .env without touching shell env.
    2. Build a minimal real `prompt_smith` Tool adapter that calls the Anthropic
       Messages API once per dispatch and returns a parsed PromptSmith result.
    3. Run that adapter through src.producer.dispatch against a minimal manifest
       seeded with one shot in "created" status.
    4. Print the dispatch result, the contents of events.db, and the agent's
       actual output so we can see the full audit trail.

This script intentionally lives in scratch/ — it's the smallest end-to-end
proof of life, not production code. Production will wire this into the
orchestration layer.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add project root to path so we can import src/ modules.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_env(env_file: Path) -> dict[str, str]:
    """Minimal .env loader — no third-party dep."""
    env: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


# ---------------------------------------------------------------------------
# Real PromptSmith adapter
# ---------------------------------------------------------------------------
def make_prompt_smith_adapter(api_key: str, system_prompt: str):
    """Return a Tool-Protocol-compliant callable that hits the Anthropic API."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    class PromptSmithTool:
        name = "prompt_smith"

        def __call__(self, shot_id: str | None, payload: dict) -> dict:
            shot = payload["shot"]
            brief = payload["brief"]
            routing = payload["routing"]
            user_msg = (
                f"Shot: {json.dumps(shot, indent=2)}\n\n"
                f"Brief: {json.dumps(brief, indent=2)}\n\n"
                f"Routing: {json.dumps(routing, indent=2)}\n\n"
                "Return ONLY a JSON object with keys: primary, negative, "
                "reference_image_paths (list). No prose, no code fence."
            )
            response = client.messages.create(
                model="claude-opus-4-5",  # SDK pinned to a current model; 4-7 when available
                max_tokens=512,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = response.content[0].text.strip()
            # Strip optional code fences
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text
                if text.endswith("```"):
                    text = text.rsplit("```", 1)[0]
                text = text.strip()
            parsed = json.loads(text)
            return {
                "primary": parsed.get("primary", ""),
                "negative": parsed.get("negative", ""),
                "reference_image_paths": parsed.get("reference_image_paths", []),
                "_usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "model": response.model,
                    "stop_reason": response.stop_reason,
                },
            }

    return PromptSmithTool()


# ---------------------------------------------------------------------------
# Fixture manifest
# ---------------------------------------------------------------------------
def make_fixture_manifest() -> dict:
    return {
        "manifest_version": "1.0",
        "project_id": "proj_probe",
        "created_at": "2026-04-22T21:00:00Z",
        "updated_at": "2026-04-22T21:00:00Z",
        "brief": {
            "logline": "A lighthouse keeper at dawn, mist clearing over empty rocks.",
            "target_duration_s": 30.0,
            "tone": ["quiet", "solitary"],
            "genre": "drama",
            "source_path": "inputs/brief.md",
            "artistic_style": "naturalistic, cold color palette, handheld",
        },
        "script": {"status": "draft", "version": 1, "path": "artifacts/script.md"},
        "shots": [
            {
                "shot_id": "sh_001",
                "scene": 1,
                "order": 1,
                "description": "Wide establishing shot of a lighthouse at dawn; mist clearing; no figures.",
                "duration_s": 4.0,
                "has_humans": False,
                "is_hero": True,
                "motion_level": "low",
                "continuity_refs": [],
                "prompt": {"authored_by": "pending", "primary": "pending"},
                "routing": {
                    "chosen_provider": "alibaba_wan_27_plus",
                    "chosen_model": "wan-2.7-plus",
                    "rationale": "workhorse for non-human non-hero",
                    "decided_by": "router",
                    "decided_at": "2026-04-22T21:00:00Z",
                    "alternates": [],
                },
                "attempts": [],
                "status": "created",
                "history": [],
                "judge_feedback": [],
                "creative_feedback": [],
            }
        ],
        "audio": {"dialogue": [], "sfx": []},
        "edit": {"status": "pending", "renderer": "hyperframes"},
        "budget": {
            "cap_usd": 151.0,
            "spent_usd": 0.0,
            "by_provider": {},
            "alibaba_quota_remaining": 50,
            "elevenlabs_credits_remaining": 117999,
        },
        "run_state": {"current_stage": "make", "last_event_id": 0, "resumable": True},
        "creative_decisions": [],
    }


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------
def main() -> int:
    env = load_env(ROOT / ".env")
    api_key = env.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key.startswith("<") or api_key == "your-key-here":
        print("ANTHROPIC_API_KEY missing or placeholder in .env", file=sys.stderr)
        return 2

    # Load the actual PromptSmith system prompt we wrote this session.
    system_prompt = (ROOT / "prompts" / "prompt_smith.md").read_text()

    # Build the real adapter and a dispatch-compatible manifest.
    tool = make_prompt_smith_adapter(api_key, system_prompt)
    manifest = make_fixture_manifest()

    # Dispatch through our existing Producer runtime.
    from src.producer import dispatch, open_event_log

    events_db = ROOT / "scratch" / "probe_events.db"
    events_db.unlink(missing_ok=True)

    shot = manifest["shots"][0]
    payload = {
        "shot": shot,
        "brief": manifest["brief"],
        "routing": shot["routing"],
    }
    print(f"=== Dispatching prompt_smith for {shot['shot_id']} ===")

    with open_event_log(events_db) as log:
        result = dispatch(
            agent="prompt_smith",
            shot_id=shot["shot_id"],
            manifest=manifest,
            ctx={"payload": payload},  # ctx is where the Producer passes tool payload
            tool=_AdaptedPayloadTool(tool),  # shim: extract payload from ctx
            events=log,
        )

        print(f"\n=== DispatchResult ===")
        print(f"agent        : {result.agent}")
        print(f"shot_id      : {result.shot_id}")
        print(f"intent_event : {result.intent_event_id}")
        print(f"result_event : {result.result_event_id}")
        print(f"warns        : {len(result.warns)}")

        print(f"\n=== Tool result ===")
        for k, v in result.result.items():
            if k == "_usage":
                print(f"  {k}: {v}")
            else:
                s = str(v)
                print(f"  {k}: {s[:200]}{'...' if len(s) > 200 else ''}")

        print(f"\n=== events.db trail ===")
        for e in log.for_shot(shot["shot_id"]):
            keys = sorted(e.payload.keys()) if isinstance(e.payload, dict) else []
            print(f"  #{e.event_id} {e.kind:20s} shot={e.shot_id} ref={e.ref_event_id} payload_keys={keys}")

    return 0


class _AdaptedPayloadTool:
    """Our dispatch() passes the ctx dict as the tool's payload argument.
    The real PromptSmith expects a different payload shape (shot/brief/routing).
    This shim extracts the nested payload so the rest of the probe stays clean.

    In production, the Producer's call site will build the correct payload directly.
    """

    def __init__(self, inner):
        self.name = inner.name
        self._inner = inner

    def __call__(self, shot_id, ctx_payload):
        # ctx_payload is the ctx dict we passed into dispatch().
        return self._inner(shot_id, ctx_payload["payload"])


if __name__ == "__main__":
    sys.exit(main())
