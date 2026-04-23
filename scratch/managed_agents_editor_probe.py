"""Managed Agents editor-commit probe — settles three open questions.

Companion to `research/managed_agents_platform_api.md`. The Editor commit
needs three things the docs don't fully specify:

    Q1. Is a `FileResource` mount bidirectional? If the agent writes to the
        mounted path, do we see the new content via the Files API after
        the session idles? Determines whether artifact extraction can
        route through Files API (clean) or must go through an agent-side
        HTTP upload (more moving parts).

    Q2. What's the shape of `user.define_outcome`? The event type is listed
        in event-stream docs but no dedicated docs page exists (url 404s).
        If its shape is tractable and the platform reports verification
        back to us, we can retire the `EDITOR_RESULT:` marker parser. If
        not, marker stays for v1.

    Q3. What's the exact key layout of `session.usage` after retrieval?
        We need to populate `cost_usd` on the dispatch result via the
        Opus 4.7 cost formula — which means grabbing `input_tokens`,
        `output_tokens`, `cache_creation_input_tokens`, `cache_read_
        input_tokens` by their real SDK field names. Probing once now
        is cheaper than a post-mortem mid-Editor session.

Shape: one short session (~30-60s agent work, expected cost $0.20-$0.50).
Writes a structured report to
`scratch/managed_agents_editor_probe/report.json`, plus the full
transcript to `transcript.txt`. Archives all cloud resources at exit.

**Run semantics** — does NOT fire unless `--run` is passed. Default mode
prints the planned calls so the draft is reviewable before the cost lands.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "scratch" / "managed_agents_editor_probe"

BETA_HEADER = "managed-agents-2026-04-01"
MODEL_PRIMARY = "claude-opus-4-7"
# Fallback follows CLAUDE.md's "fast mode on Opus 4.6" note — 4.5 is stale
# (pre-dated this pipeline's pin). Prior hyperframes probe had 4.5 as
# fallback; we correct it here rather than inherit the bug.
MODEL_FALLBACK = "claude-opus-4-6"

# Seed file content we upload; agent is told to read this + overwrite.
SEED_FILE_CONTENT = "SEED_CONTENT_V1\n"
SEED_FILE_MOUNT_PATH = "/workspace/seed.txt"
AGENT_WRITE_MARKER = "MODIFIED_BY_AGENT_V1"
AGENT_NEW_FILE_PATH = "/workspace/agent_wrote.txt"
AGENT_NEW_FILE_CONTENT = "AGENT_CREATED_THIS_V1"

SYSTEM_PROMPT = f"""You are a sandbox probe agent. Your job is to execute a specific five-step sequence and report back whether each step succeeded. Be terse — the operator reading your output cares about the literal facts only.

Steps, in order. Use the bash tool.

0. Discovery — `pwd && echo '---' && ls -la $HOME 2>/dev/null | head -20 && echo '---' && ls -la /workspace 2>/dev/null || echo "NO_WORKSPACE_DIR"`. This tells us the real working directory and whether /workspace/ exists at all. Report the pwd literally.

1. `cat {SEED_FILE_MOUNT_PATH} 2>&1 | head -5` — read the mounted seed file. If the mount path does not exist, report "MOUNT_PATH_MISSING". Otherwise report its contents verbatim.

2. `echo -n {AGENT_WRITE_MARKER!r} > {SEED_FILE_MOUNT_PATH} 2>&1 && cat {SEED_FILE_MOUNT_PATH}` — overwrite the seed mount path with the marker string and confirm the write landed locally. If the write fails (read-only overlay, etc.), report the full error.

3. `echo -n {AGENT_NEW_FILE_CONTENT!r} > {AGENT_NEW_FILE_PATH} 2>&1 && ls -la $(dirname {AGENT_NEW_FILE_PATH})` — write a brand-new file to the workspace parent dir and list it. Same failure reporting if the path is not writable.

4. `env | grep -E '^(ANTHROPIC|CLAUDE|SESSION|AGENT|WORKSPACE)' | head -20` — dump any platform-set environment variables. If none match, output `NO_PLATFORM_ENV`.

On the FINAL line of your FINAL message, output EXACTLY one of:

    PROBE_RESULT: {{"verdict":"PASS","steps_ok":[0,1,2,3,4],"pwd":"<cwd>","workspace_exists":true,"seed_read":"<contents>","notes":"<short>"}}
    PROBE_RESULT: {{"verdict":"PARTIAL","steps_ok":[<list>],"pwd":"<cwd>","workspace_exists":<bool>,"notes":"<which steps failed and why>"}}
    PROBE_RESULT: {{"verdict":"FAIL","failed_at":<step>,"stderr_tail":"<last 300>","notes":"<short>"}}

No prose after PROBE_RESULT. It must be valid JSON after the prefix. PARTIAL is explicitly allowed — if step 1 fails because /workspace/seed.txt isn't mounted, continue running steps 3 and 4 anyway so we capture the full layout picture.
"""

USER_TASK = (
    "Run the four-step probe sequence. "
    "Return the PROBE_RESULT JSON line as specified in your system prompt."
)

# One of the probe objectives: what shape does `user.define_outcome` want?
# We try the most obvious SDK-Pydantic-friendly shapes and record which (if
# any) are accepted by the .send() call without a validation error.
OUTCOME_RUBRIC_TEXT = (
    "Success rubric: all four probe steps return exit code 0 and the agent "
    "emits a PROBE_RESULT line with verdict=PASS."
)
OUTCOME_SHAPES_TO_TRY: list[tuple[str, dict]] = [
    (
        "shape_content_blocks",
        {
            "type": "user.define_outcome",
            "content": [{"type": "text", "text": OUTCOME_RUBRIC_TEXT}],
        },
    ),
    (
        "shape_rubric_string",
        {"type": "user.define_outcome", "rubric": OUTCOME_RUBRIC_TEXT},
    ),
    (
        "shape_outcome_object",
        {
            "type": "user.define_outcome",
            "outcome": {"description": OUTCOME_RUBRIC_TEXT},
        },
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def build_env_params(seed_file_id: str) -> dict:
    # Docs show `packages` as a plain object without a `type` discriminator.
    # Prior hyperframes probe included `"type": "packages"` and worked, but
    # it's redundant with the surrounding key and we'd rather the probe
    # fail loud on a wrong shape than silently accept extra fields.
    # If the SDK rejects this exact shape, the ValidationError message
    # will point us to the right discriminator to add.
    return {
        "type": "cloud",
        "networking": {"type": "unrestricted"},
        "packages": {
            "apt": ["ffmpeg"],
            "npm": ["hyperframes"],   # test caching npm at env level
        },
    }


def build_agent_tools() -> list[dict]:
    return [
        {
            "type": "agent_toolset_20260401",
            "default_config": {
                "enabled": True,
                "permission_policy": {"type": "always_allow"},
            },
            "configs": [
                {"name": "bash", "enabled": True, "permission_policy": {"type": "always_allow"}},
                {"name": "read", "enabled": True, "permission_policy": {"type": "always_allow"}},
                {"name": "write", "enabled": True, "permission_policy": {"type": "always_allow"}},
                {"name": "edit", "enabled": True, "permission_policy": {"type": "always_allow"}},
            ],
        }
    ]


def format_event(ev) -> str:
    """Render an event compactly. Copied from the hyperframes probe."""
    t = getattr(ev, "type", "?")
    if hasattr(ev, "content"):
        content = ev.content
        if isinstance(content, list):
            pieces = []
            for b in content:
                bt = getattr(b, "type", "?")
                if bt == "text":
                    txt = getattr(b, "text", "")
                    pieces.append(f"[text]{txt[:500]}")
                elif "tool_use" in bt:
                    name = getattr(b, "name", "?")
                    inp = getattr(b, "input", None)
                    pieces.append(f"[tool_use:{name}] {str(inp)[:200]}")
                elif "tool_result" in bt:
                    inner = getattr(b, "content", "")
                    if isinstance(inner, list):
                        inner = "".join(
                            getattr(ib, "text", "") for ib in inner if getattr(ib, "type", "") == "text"
                        )
                    pieces.append(f"[tool_result] {str(inner)[:500]}")
                else:
                    pieces.append(f"[{bt}]")
            return f"{t} :: {' | '.join(pieces)}"
    return t


def extract_final_text(transcript: list[str]) -> str:
    for entry in reversed(transcript):
        m = re.search(r"\[text\](.+)$", entry, re.DOTALL)
        if m:
            return m.group(1).strip()
    return ""


def parse_probe_result(text: str) -> dict | None:
    m = re.search(r"PROBE_RESULT:\s*(\{.+\})\s*$", text, re.MULTILINE | re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def usage_to_dict(usage) -> dict:
    """Shallow-copy usage SDK object into a plain dict for reporting. SDKs
    differ; this probe is partly about discovering the key set — so we
    iterate __dict__ / vars() rather than hardcoding field names."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    # Pydantic/BaseModel style
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(usage, attr, None)
        if callable(fn):
            try:
                return dict(fn())
            except Exception:
                pass
    # Fallback: vars()
    try:
        return {k: v for k, v in vars(usage).items() if not k.startswith("_")}
    except TypeError:
        return {"repr": repr(usage)}


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


def run_probe(api_key: str, model: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    betas = [BETA_HEADER]

    report: dict = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model,
        "q1_files_api_bidirectional": {},
        "q2_user_define_outcome": {},
        "q3_session_usage_shape": {},
        "probe_result": None,
        "transcript_len": 0,
        "errors": [],
    }

    seed_file = None
    env = None
    agent = None
    session = None
    transcript: list[str] = []

    try:
        # ---- Upload seed file for Q1 ----
        print("[probe] uploading seed file via Files API")
        seed_file = client.beta.files.upload(
            file=("seed.txt", SEED_FILE_CONTENT.encode(), "text/plain"),
            betas=betas,
        )
        print(f"[probe] seed_file.id = {seed_file.id}")
        report["q1_files_api_bidirectional"]["seed_file_id"] = seed_file.id
        report["q1_files_api_bidirectional"]["seed_content_before"] = SEED_FILE_CONTENT

        # ---- Create environment ----
        print("[probe] creating environment")
        env = client.beta.environments.create(
            name=f"rv-editor-probe-{int(time.time())}",
            config=build_env_params(seed_file.id),
            betas=betas,
        )
        print(f"[probe] env.id = {env.id}")

        # ---- Create agent ----
        print("[probe] creating agent")
        agent = client.beta.agents.create(
            model=model,
            name="rv-editor-probe-agent",
            system=SYSTEM_PROMPT,
            tools=build_agent_tools(),
            betas=betas,
        )
        print(f"[probe] agent.id = {agent.id}")

        # ---- Create session ----
        print("[probe] creating session")
        session = client.beta.sessions.create(
            agent=agent.id,
            environment_id=env.id,
            title="editor commit probe",
            betas=betas,
        )
        print(f"[probe] session.id = {session.id}")

        # ---- Mount the seed file (Q1) ----
        print("[probe] mounting seed file as session resource")
        try:
            client.beta.sessions.resources.create(
                session.id,
                resource={
                    "type": "file",
                    "file_id": seed_file.id,
                    "mount_path": SEED_FILE_MOUNT_PATH,
                },
                betas=betas,
            )
            report["q1_files_api_bidirectional"]["mount_succeeded"] = True
        except Exception as e:
            print(f"[probe] mount failed: {type(e).__name__}: {e}")
            report["q1_files_api_bidirectional"]["mount_succeeded"] = False
            report["q1_files_api_bidirectional"]["mount_error"] = f"{type(e).__name__}: {e}"

        # ---- Open event stream ----
        print("[probe] opening event stream")
        with client.beta.sessions.events.stream(session.id, betas=betas) as stream:
            # ---- Send initial user.message ----
            client.beta.sessions.events.send(
                session.id,
                events=[{"type": "user.message", "content": [{"type": "text", "text": USER_TASK}]}],
                betas=betas,
            )

            # ---- Try every user.define_outcome shape (Q2) ----
            # Try ALL shapes even after a success — a shape being accepted
            # by SDK Pydantic validation does NOT guarantee the platform
            # acts on it (silent no-op is possible). The subsequent
            # `observed_events` scan below catches the behavioral signal;
            # this loop catches the SDK-validation-layer signal. Some
            # shapes may fail with "outcome already defined" once one
            # lands — that error itself is informative (tells us outcomes
            # are single-shot per session).
            report["q2_user_define_outcome"]["shapes_tried"] = []
            for shape_name, shape_payload in OUTCOME_SHAPES_TO_TRY:
                try:
                    client.beta.sessions.events.send(
                        session.id,
                        events=[shape_payload],
                        betas=betas,
                    )
                    report["q2_user_define_outcome"]["shapes_tried"].append({
                        "shape": shape_name,
                        "payload": shape_payload,
                        "accepted": True,
                    })
                    print(f"[probe] user.define_outcome ACCEPTED with shape={shape_name}")
                except Exception as e:
                    report["q2_user_define_outcome"]["shapes_tried"].append({
                        "shape": shape_name,
                        "payload": shape_payload,
                        "accepted": False,
                        "error_type": type(e).__name__,
                        "error": str(e)[:400],
                    })
                    print(f"[probe] user.define_outcome shape={shape_name} REJECTED: {type(e).__name__}: {str(e)[:200]}")

            # ---- Drain events ----
            print("[probe] draining events (waiting for session.status_idle)")
            start = time.time()
            for event in stream:
                formatted = format_event(event)
                transcript.append(formatted)
                elapsed = time.time() - start
                print(f"[{elapsed:6.1f}s] {formatted[:220]}")
                # Capture any outcome-related events verbatim
                etype = getattr(event, "type", "")
                if "outcome" in etype.lower():
                    report["q2_user_define_outcome"].setdefault("observed_events", []).append({
                        "type": etype,
                        "repr": repr(event)[:500],
                    })
                if etype in {"session.status_idle", "session.idle", "session.ended", "session.status_terminated"}:
                    print("[probe] session idle/terminated — stopping stream")
                    break
                # Hard cap: 5 minutes
                if elapsed > 300:
                    print("[probe] 5-minute cap — stopping stream")
                    break

        # ---- Post-session: retrieve session + usage shape (Q3) ----
        time.sleep(0.5)
        print("[probe] retrieving session for usage shape")
        try:
            retrieved = client.beta.sessions.retrieve(session.id, betas=betas)
            usage = getattr(retrieved, "usage", None)
            report["q3_session_usage_shape"]["usage"] = usage_to_dict(usage)
            stats = getattr(retrieved, "stats", None)
            report["q3_session_usage_shape"]["stats"] = usage_to_dict(stats)
            status = getattr(retrieved, "status", None)
            report["q3_session_usage_shape"]["status"] = str(status) if status else None
        except Exception as e:
            report["errors"].append(f"session.retrieve: {type(e).__name__}: {e}")

        # ---- Post-session: Files API re-read seed (Q1) ----
        print("[probe] re-reading seed file via Files API to check bidirectionality")
        try:
            content_after = client.beta.files.download(seed_file.id, betas=betas)
            if hasattr(content_after, "read"):
                body = content_after.read()
            elif isinstance(content_after, (bytes, bytearray)):
                body = bytes(content_after)
            else:
                body = str(content_after).encode()
            seed_after = body.decode("utf-8", errors="replace")
            report["q1_files_api_bidirectional"]["seed_content_after"] = seed_after
            report["q1_files_api_bidirectional"]["bidirectional"] = (
                AGENT_WRITE_MARKER in seed_after
            )
            if AGENT_WRITE_MARKER in seed_after:
                print("[probe] Q1 RESULT: Files API mount IS bidirectional — agent's write landed")
            else:
                print(
                    f"[probe] Q1 RESULT: Files API mount is NOT bidirectional — "
                    f"seed content unchanged (got {seed_after[:80]!r})"
                )
        except Exception as e:
            report["q1_files_api_bidirectional"]["download_error"] = f"{type(e).__name__}: {e}"
            print(f"[probe] seed file re-download failed: {type(e).__name__}: {e}")

        # ---- Extract agent's PROBE_RESULT marker ----
        final_text = extract_final_text(transcript)
        probe_result = parse_probe_result(final_text)
        report["probe_result"] = probe_result
        report["transcript_len"] = len(transcript)

    except Exception as e:
        report["errors"].append(f"probe: {type(e).__name__}: {e}")
        print(f"[probe] EXCEPTION: {type(e).__name__}: {e}")

    finally:
        # ---- Archive all resources so we don't accrue cost ----
        for kind, resource in (("session", session), ("agent", agent), ("environment", env)):
            if resource is None:
                continue
            try:
                if kind == "session":
                    client.beta.sessions.archive(resource.id, betas=[BETA_HEADER])
                elif kind == "agent":
                    client.beta.agents.archive(resource.id, betas=[BETA_HEADER])
                elif kind == "environment":
                    client.beta.environments.archive(resource.id, betas=[BETA_HEADER])
                print(f"[probe] archived {kind} {resource.id}")
            except Exception as e:
                print(f"[probe] warning: failed to archive {kind} {getattr(resource,'id','?')}: {e}")

        # Don't auto-delete the seed file — let the operator inspect post-run.
        # `client.beta.files.delete(seed_file.id)` would be the cleanup.

    # ---- Write transcript + report ----
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "transcript.txt").write_text("\n".join(transcript))
    (REPORT_DIR / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\n[probe] transcript written to {REPORT_DIR / 'transcript.txt'}")
    print(f"[probe] report written to {REPORT_DIR / 'report.json'}")
    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def print_plan() -> None:
    """Dry-run: summarize what the probe WILL do without spending money."""
    print("=" * 72)
    print("MANAGED AGENTS EDITOR PROBE — DRY RUN (no API calls)")
    print("=" * 72)
    print()
    print("Estimated cost: $0.20–$0.50 (short session, ~30-60s agent work)")
    print("Estimated wall time: 1–3 min (including provisioning + archive)")
    print()
    print("Calls the probe will make:")
    print("  1. client.beta.files.upload(seed.txt)")
    print(f"     → body: {SEED_FILE_CONTENT!r}")
    print()
    print("  2. client.beta.environments.create(config={")
    print("       'type': 'cloud',")
    print("       'networking': {'type': 'unrestricted'},")
    print("       'packages': {'apt': ['ffmpeg'], 'npm': ['hyperframes']}}")
    print("     })")
    print()
    print(f"  3. client.beta.agents.create(model={MODEL_PRIMARY!r}, system=<{len(SYSTEM_PROMPT)} chars>)")
    print()
    print("  4. client.beta.sessions.create(agent=<id>, environment_id=<id>)")
    print()
    print("  5. client.beta.sessions.resources.create(")
    print("       resource={'type': 'file', 'file_id': <seed>, 'mount_path':")
    print(f"                 {SEED_FILE_MOUNT_PATH!r}}})")
    print()
    print("  6. client.beta.sessions.events.send(user.message)")
    print()
    print(f"  7. client.beta.sessions.events.send(user.define_outcome) × all {len(OUTCOME_SHAPES_TO_TRY)} shapes (sequentially, not early-terminated):")
    for shape_name, shape in OUTCOME_SHAPES_TO_TRY:
        print(f"     - {shape_name}: {json.dumps(shape)[:90]}")
    print()
    print("  8. client.beta.sessions.events.stream(...) — drain to idle")
    print()
    print("  9. client.beta.sessions.retrieve(session.id) → inspect .usage / .stats")
    print()
    print(" 10. client.beta.files.download(seed_file.id) → check if agent's write")
    print("     landed back in the Files-API view of the mounted file")
    print()
    print(" 11. Archive session + agent + environment")
    print()
    print("Findings written to:")
    print(f"  - {REPORT_DIR / 'report.json'}")
    print(f"  - {REPORT_DIR / 'transcript.txt'}")
    print()
    print("To execute the probe for real, rerun with --run")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe the Managed Agents platform for the three open "
        "questions in research/managed_agents_platform_api.md"
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="actually execute the probe (spends real API credits). "
        "Without this flag, prints the planned calls and exits.",
    )
    args = parser.parse_args()

    if not args.run:
        print_plan()
        return 0

    env_vars = load_env(ROOT / ".env")
    api_key = env_vars.get("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
    if not api_key:
        print("ANTHROPIC_API_KEY missing", file=sys.stderr)
        return 2

    for model in (MODEL_PRIMARY, MODEL_FALLBACK):
        print(f"\n=========== attempt: model={model} ===========")
        try:
            report = run_probe(api_key, model)
        except Exception as e:
            print(f"[probe] EXCEPTION: {type(e).__name__}: {e}")
            if "model" in str(e).lower() and model == MODEL_PRIMARY:
                print("[probe] trying fallback model")
                continue
            raise
        break
    else:  # pragma: no cover
        print("all model attempts failed", file=sys.stderr)
        return 3

    print("\n=== PROBE FINDINGS ===")
    print(json.dumps({
        "Q1_files_bidirectional": report.get("q1_files_api_bidirectional", {}).get("bidirectional"),
        "Q2_outcome_shape_accepted": [
            s["shape"] for s in report.get("q2_user_define_outcome", {}).get("shapes_tried", [])
            if s.get("accepted")
        ],
        "Q3_usage_keys": sorted((report.get("q3_session_usage_shape", {}).get("usage") or {}).keys()),
        "probe_result": report.get("probe_result"),
        "errors": report.get("errors", []),
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
