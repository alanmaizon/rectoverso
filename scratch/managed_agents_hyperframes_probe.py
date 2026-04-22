"""Managed Agents sandbox verification — Condition 1 of PROBE_REPORT.md.

Spins up a real Managed Agents session with a bash-equipped agent. The agent's
job: run `npx hyperframes@latest init ... && npx hyperframes render` inside
the Anthropic-managed Ubuntu sandbox and report whether an MP4 was produced.

Success criteria (strict): the agent reports VERDICT=PASS and includes the
output MP4 size in its final message. If the agent reports VERDICT=FAIL we
capture the failure cause and report it verbatim.

Keeps side effects minimal: the environment and agent are created fresh for
this probe and archived at the end to avoid accumulating billable resources.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BETA_HEADER = "managed-agents-2026-04-01"
MODEL_PRIMARY = "claude-opus-4-7"
MODEL_FALLBACK = "claude-opus-4-5"

SYSTEM_PROMPT = """You are a sandbox verification agent. Your job is to run a specific sequence of bash commands and report back whether they succeeded.

Execute these steps in order, using the bash tool. Do NOT skip steps. After each step, print a one-line summary.

1. `node --version` and `npm --version` — confirm Node is available.
2. `ffmpeg -version | head -1` — confirm FFmpeg is available.
3. `mkdir -p /tmp/hf && cd /tmp/hf && npx --yes hyperframes@latest init probe --non-interactive --example blank` — scaffold a blank project. This may download ~85MB of Chrome the first time; that is normal.
4. `cd /tmp/hf/probe && npx hyperframes lint --json` — validate the composition.
5. `cd /tmp/hf/probe && npx hyperframes render --output out.mp4` — render to MP4.
6. `ls -la /tmp/hf/probe/out.mp4 && md5sum /tmp/hf/probe/out.mp4` — confirm the file exists and report its size + hash.

On the FINAL line of your FINAL message, output EXACTLY one of these two results:

    PROBE_RESULT: {"verdict":"PASS","mp4_bytes":<int>,"mp4_md5":"<hex>","notes":"<short>"}
    PROBE_RESULT: {"verdict":"FAIL","failed_at":"<step>","exit_code":<int>,"stderr_tail":"<last 300 chars>","notes":"<short>"}

No prose after PROBE_RESULT. Nothing fancy. The line must be parseable JSON after the prefix.
"""

USER_TASK = (
    "Run the six-step verification sequence. "
    "Return the PROBE_RESULT JSON line as specified in your system prompt."
)


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def build_env_params() -> dict:
    """Environment config: cloud sandbox, unrestricted network (npm + CDN),
    ffmpeg declared explicitly. Node is pre-installed per Anthropic docs."""
    return {
        "type": "cloud",
        "networking": {"type": "unrestricted"},
        "packages": {
            "type": "packages",
            "apt": ["ffmpeg"],
        },
    }


def build_agent_tools() -> list[dict]:
    """agent_toolset_20260401 with bash + file ops. always_allow so the
    sandbox self-drives without per-call prompts."""
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
    """Render an event compactly for our probe log."""
    t = getattr(ev, "type", "?")
    # Try common shapes
    if hasattr(ev, "content"):
        content = ev.content
        # content may be a list of blocks
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
                    # content may be nested blocks
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
    """Return the last substantial [text]... block from the agent."""
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


def run_probe(api_key: str, model: str) -> tuple[dict | None, list[str]]:
    import anthropic
    from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent  # noqa: F401

    client = anthropic.Anthropic(api_key=api_key)
    betas = [BETA_HEADER]

    print(f"[probe] creating environment (model={model}, beta={BETA_HEADER})")
    env = client.beta.environments.create(
        name=f"rv-hf-probe-{int(time.time())}",
        config=build_env_params(),
        betas=betas,
    )
    print(f"[probe] environment.id = {env.id}")

    agent = None
    session = None
    transcript: list[str] = []

    try:
        print("[probe] creating agent")
        agent = client.beta.agents.create(
            model=model,
            name="rv-hf-probe-agent",
            system=SYSTEM_PROMPT,
            tools=build_agent_tools(),
            betas=betas,
        )
        print(f"[probe] agent.id = {agent.id}")

        print("[probe] creating session")
        session = client.beta.sessions.create(
            agent=agent.id,
            environment_id=env.id,
            title="hyperframes sandbox verification",
            betas=betas,
        )
        print(f"[probe] session.id = {session.id}")

        print("[probe] opening event stream")
        with client.beta.sessions.events.stream(session.id, betas=betas) as stream:
            # Send the user task now that the stream is open
            client.beta.sessions.events.send(
                session.id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": USER_TASK}],
                    }
                ],
                betas=betas,
            )
            print("[probe] user.message sent; awaiting events (will stop on session.status_idle)")

            start = time.time()
            for event in stream:
                formatted = format_event(event)
                transcript.append(formatted)
                elapsed = time.time() - start
                print(f"[{elapsed:6.1f}s] {formatted[:220]}")
                if getattr(event, "type", "") in {"session.status_idle", "session.idle", "session.ended"}:
                    print("[probe] session idle — stopping stream")
                    break

        # Give the API a moment to flush final messages, then pull recent events
        # and extract the final agent text block.
        time.sleep(0.5)
        recent = list(client.beta.sessions.events.list(session.id, limit=40, order="desc", betas=betas))
        for e in recent:
            formatted = format_event(e)
            if formatted not in transcript:
                transcript.append(formatted)

        final_text = extract_final_text(transcript)
        result = parse_probe_result(final_text)
        return result, transcript

    finally:
        # Tidy up so we don't leave a pile of probe envs/agents lying around.
        for kind, resource in (("session", session), ("agent", agent), ("environment", env)):
            if resource is None:
                continue
            try:
                if kind == "session":
                    client.beta.sessions.archive(resource.id, betas=betas)
                elif kind == "agent":
                    client.beta.agents.archive(resource.id, betas=betas)
                elif kind == "environment":
                    client.beta.environments.archive(resource.id, betas=betas)
                print(f"[probe] archived {kind} {resource.id}")
            except Exception as e:
                print(f"[probe] warning: failed to archive {kind} {getattr(resource,'id','?')}: {e}")


def main() -> int:
    env_vars = load_env(ROOT / ".env")
    api_key = env_vars.get("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
    if not api_key:
        print("ANTHROPIC_API_KEY missing", file=sys.stderr)
        return 2

    for model in (MODEL_PRIMARY, MODEL_FALLBACK):
        print(f"\n=========== attempt: model={model} ===========")
        try:
            result, transcript = run_probe(api_key, model)
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

    # Persist transcript for the probe report
    out = ROOT / "scratch" / "hyperframes-probe" / "managed_agents_probe_transcript.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(transcript))
    print(f"\n[probe] transcript written to {out}")

    print("\n=== PROBE_RESULT ===")
    if result is None:
        print("FAILED to parse PROBE_RESULT line. See transcript.")
        return 4
    print(json.dumps(result, indent=2))
    return 0 if result.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
