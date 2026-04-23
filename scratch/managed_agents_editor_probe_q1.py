"""Managed Agents editor probe — Q1 focus: FileResource mount bidirectionality.

Follow-up to `scratch/managed_agents_editor_probe.py` which left Q1
structurally unanswered (wrong SDK method name `.create()` vs `.add()`).
Keeps Q2 decided (marker parser stays, see research doc) and Vaults
out of scope. See `research/managed_agents_platform_api.md § Day 5
probe findings` for the full context.

What this probe answers:

    Q1a. Does `client.beta.sessions.resources.add(type="file", ...)` work?
         (SDK signature says yes; let's verify end-to-end.)

    Q1b. If the agent writes to the mount path, do we see the new content
         via `client.beta.files.download(file_id)` post-session?

    Q1c. What `purpose` / `downloadable` flag on `files.upload` (if any)
         makes a file round-trippable? Tries three variants sequentially.

    Q1d. If FileResource is structurally one-way, does the agent have
         network access + the right beta header to do an agent-side HTTP
         upload via `curl -X POST /v1/beta/files`? This is the escape
         hatch for artifact extraction.

Shape: one session, ~45-60s, est. $0.30. Writes to
`scratch/managed_agents_editor_probe_q1/{report.json,transcript.txt}`.
Archives all cloud resources at exit. `--run` gated.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "scratch" / "managed_agents_editor_probe_q1"

BETA_HEADER = "managed-agents-2026-04-01"
MODEL_PRIMARY = "claude-opus-4-7"
MODEL_FALLBACK = "claude-opus-4-6"

# Each seed we upload is a distinct file with a distinct marker so we can
# tell them apart in the agent's output + in the post-session download.
SEED_CONTENTS = {
    "variant_a_no_purpose": "SEED_VARIANT_A\n",
    "variant_b_purpose_session_input": "SEED_VARIANT_B\n",
    "variant_c_purpose_shared_resource": "SEED_VARIANT_C\n",
}

# Agent-side marker to confirm a write-back happened.
AGENT_WRITE_MARKER = "MODIFIED_BY_AGENT_Q1_V1"

SYSTEM_PROMPT = f"""You are a sandbox probe agent. Execute the five-step sequence below using the bash tool. Be terse.

Steps:

1. `ls -la /workspace/` — inventory the mount directory. Report what files landed there. If any mount path is missing, note it.

2. For EACH file found in /workspace/ whose name starts with "seed_", in sorted order:
   - `cat /workspace/<filename>` to read it
   - `echo -n {AGENT_WRITE_MARKER!r}__<filename> > /workspace/<filename>` to overwrite with a distinguishable marker
   - `cat /workspace/<filename>` to confirm the overwrite landed locally
   Report each file's original content AND the post-write content.

3. `which curl && curl --version | head -1` — confirm curl is present.

4. AGENT-SIDE UPLOAD TEST. This tests the fallback artifact-extraction path:
   - `echo -n "AGENT_UPLOADED_BLOB_V1" > /tmp/agent_blob.txt`
   - Attempt an HTTP POST to the Anthropic Files API from inside the container. DO NOT include any API key — you don't have one and shouldn't. We're checking whether the container has network reachability and whether the platform auto-injects credentials. Run:
     `curl -v -X POST https://api.anthropic.com/v1/files -H "anthropic-beta: {BETA_HEADER}" -F "file=@/tmp/agent_blob.txt" 2>&1 | tail -30`
   - Report the HTTP status line and any auth-related error verbatim. A 401 means "reachable but no creds" (good — confirms network works, tells us we need to mount a key). A DNS error means "no network" (bad — tells us the environment networking config didn't stick). A 2xx would be surprising.

5. `env | grep -iE '(api_key|anthropic|claude)' | head -10 || echo "NO_ANTHROPIC_ENV"` — check if the platform secretly injected any Anthropic-related env vars that could authenticate step 4.

FINAL LINE of your FINAL message (no prose after):

    PROBE_RESULT: {{"verdict":"<PASS|PARTIAL|FAIL>","files_found":["..."],"overwrites_succeeded":["..."],"overwrites_failed":[{{"file":"...","err":"..."}}],"curl_present":true|false,"http_status":"<status line or err>","anthropic_env":"<raw output>"}}

Valid JSON only after the prefix.
"""

USER_TASK = "Execute the five-step probe. Return PROBE_RESULT JSON as specified."


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


def build_env_params() -> dict:
    return {
        "type": "cloud",
        "networking": {"type": "unrestricted"},
        "packages": {"apt": ["ffmpeg"], "npm": ["hyperframes"]},
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


def upload_variants(client, betas: list[str]) -> list[dict]:
    """Try three upload shapes; return a list of result dicts including file_id
    if the upload succeeded. Uploads are independent — each variant either
    succeeds or fails without blocking the others."""
    upload_sig = inspect.signature(client.beta.files.upload)
    upload_kwargs_available = set(upload_sig.parameters.keys())

    variants = [
        {
            "name": "variant_a_no_purpose",
            "mount_path": "/workspace/seed_a.txt",
            "kwargs": {},  # baseline — what we tried last time
        },
        {
            "name": "variant_b_purpose_session_input",
            "mount_path": "/workspace/seed_b.txt",
            "kwargs": {"purpose": "session_input"},
        },
        {
            "name": "variant_c_purpose_shared_resource",
            "mount_path": "/workspace/seed_c.txt",
            "kwargs": {"purpose": "shared_resource"},
        },
    ]

    results = []
    for v in variants:
        body = SEED_CONTENTS[v["name"]].encode()
        try:
            # Filter kwargs to what the SDK supports so we get a clear
            # server-side error rather than a local TypeError.
            kw = {k: val for k, val in v["kwargs"].items() if k in upload_kwargs_available}
            rejected_locally = [k for k in v["kwargs"] if k not in upload_kwargs_available]
            file = client.beta.files.upload(
                file=(f"{v['name']}.txt", body, "text/plain"),
                betas=betas,
                **kw,
            )
            results.append({
                "variant": v["name"],
                "mount_path": v["mount_path"],
                "kwargs_sent": kw,
                "kwargs_rejected_locally": rejected_locally,
                "file_id": file.id,
                "upload_ok": True,
                "content": SEED_CONTENTS[v["name"]],
            })
            print(f"[probe] uploaded {v['name']} → {file.id} (kwargs={kw})")
        except Exception as e:
            results.append({
                "variant": v["name"],
                "mount_path": v["mount_path"],
                "kwargs_sent": v["kwargs"],
                "upload_ok": False,
                "error_type": type(e).__name__,
                "error": str(e)[:500],
            })
            print(f"[probe] upload {v['name']} FAILED: {type(e).__name__}: {str(e)[:200]}")
    return results


def format_event(ev) -> str:
    t = getattr(ev, "type", "?")
    if hasattr(ev, "content"):
        content = ev.content
        if isinstance(content, list):
            pieces = []
            for b in content:
                bt = getattr(b, "type", "?")
                if bt == "text":
                    txt = getattr(b, "text", "")
                    pieces.append(f"[text]{txt[:600]}")
                elif "tool_use" in bt:
                    name = getattr(b, "name", "?")
                    inp = getattr(b, "input", None)
                    pieces.append(f"[tool_use:{name}] {str(inp)[:300]}")
                elif "tool_result" in bt:
                    inner = getattr(b, "content", "")
                    if isinstance(inner, list):
                        inner = "".join(
                            getattr(ib, "text", "") for ib in inner if getattr(ib, "type", "") == "text"
                        )
                    pieces.append(f"[tool_result] {str(inner)[:600]}")
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
        "q1a_upload_variants": [],
        "q1b_mounts": [],
        "q1c_download_attempts": [],
        "q1d_agent_side_curl": None,
        "probe_result": None,
        "transcript_len": 0,
        "errors": [],
    }

    uploads = []
    env = None
    agent = None
    session = None
    transcript: list[str] = []

    try:
        # ---- Q1c: try three upload shapes ----
        print("[probe] Q1c — uploading three seed-file variants")
        uploads = upload_variants(client, betas)
        report["q1a_upload_variants"] = uploads

        successful_uploads = [u for u in uploads if u["upload_ok"]]
        if not successful_uploads:
            raise RuntimeError("all upload variants failed; cannot proceed with Q1b")

        # ---- Create environment ----
        print("[probe] creating environment")
        env = client.beta.environments.create(
            name=f"rv-editor-probe-q1-{int(time.time())}",
            config=build_env_params(),
            betas=betas,
        )
        print(f"[probe] env.id = {env.id}")

        # ---- Create agent ----
        print("[probe] creating agent")
        agent = client.beta.agents.create(
            model=model,
            name="rv-editor-probe-q1-agent",
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
            title="editor commit probe Q1",
            betas=betas,
        )
        print(f"[probe] session.id = {session.id}")

        # ---- Q1a: mount each successful upload via the CORRECT .add() call ----
        print("[probe] Q1a — mounting seed files via sessions.resources.add(...)")
        for u in successful_uploads:
            try:
                resource = client.beta.sessions.resources.add(
                    session.id,
                    file_id=u["file_id"],
                    type="file",
                    mount_path=u["mount_path"],
                    betas=betas,
                )
                u["mount_ok"] = True
                u["resource_id"] = getattr(resource, "id", None)
                report["q1b_mounts"].append({
                    "variant": u["variant"],
                    "file_id": u["file_id"],
                    "mount_path": u["mount_path"],
                    "resource_id": u["resource_id"],
                    "mount_ok": True,
                })
                print(f"[probe] mounted {u['variant']} at {u['mount_path']} (resource {u['resource_id']})")
            except Exception as e:
                u["mount_ok"] = False
                u["mount_error"] = f"{type(e).__name__}: {e}"
                report["q1b_mounts"].append({
                    "variant": u["variant"],
                    "file_id": u["file_id"],
                    "mount_path": u["mount_path"],
                    "mount_ok": False,
                    "error": u["mount_error"][:400],
                })
                print(f"[probe] mount {u['variant']} FAILED: {type(e).__name__}: {str(e)[:200]}")

        # ---- Open event stream ----
        print("[probe] opening event stream")
        with client.beta.sessions.events.stream(session.id, betas=betas) as stream:
            # Send the kickoff message
            client.beta.sessions.events.send(
                session.id,
                events=[{"type": "user.message", "content": [{"type": "text", "text": USER_TASK}]}],
                betas=betas,
            )

            print("[probe] draining events (waiting for session.status_idle)")
            start = time.time()
            for event in stream:
                formatted = format_event(event)
                transcript.append(formatted)
                elapsed = time.time() - start
                print(f"[{elapsed:6.1f}s] {formatted[:260]}")
                etype = getattr(event, "type", "")
                if etype in {"session.status_idle", "session.idle", "session.ended", "session.status_terminated"}:
                    print("[probe] session idle/terminated — stopping stream")
                    break
                if elapsed > 300:
                    print("[probe] 5-minute cap — stopping stream")
                    break

        # ---- Q1b (second half): re-download each seed file, check for AGENT_WRITE_MARKER ----
        print("\n[probe] Q1b — re-downloading each seed file to check bidirectionality")
        for u in successful_uploads:
            if not u.get("mount_ok"):
                continue
            attempt = {
                "variant": u["variant"],
                "file_id": u["file_id"],
            }
            try:
                resp = client.beta.files.download(u["file_id"], betas=betas)
                if hasattr(resp, "read"):
                    body = resp.read()
                elif isinstance(resp, (bytes, bytearray)):
                    body = bytes(resp)
                else:
                    body = str(resp).encode()
                decoded = body.decode("utf-8", errors="replace")
                attempt["download_ok"] = True
                attempt["content_after"] = decoded
                attempt["bidirectional"] = AGENT_WRITE_MARKER in decoded
                print(
                    f"[probe] {u['variant']}: bidirectional={attempt['bidirectional']} "
                    f"(content={decoded[:80]!r})"
                )
            except Exception as e:
                attempt["download_ok"] = False
                attempt["error"] = f"{type(e).__name__}: {str(e)[:400]}"
                print(f"[probe] download {u['variant']} FAILED: {type(e).__name__}: {str(e)[:200]}")
            report["q1c_download_attempts"].append(attempt)

        # ---- Retrieve session for usage snapshot ----
        try:
            retrieved = client.beta.sessions.retrieve(session.id, betas=betas)
            usage = getattr(retrieved, "usage", None)
            if usage is not None:
                for attr in ("model_dump", "dict", "to_dict"):
                    fn = getattr(usage, attr, None)
                    if callable(fn):
                        try:
                            report["usage"] = dict(fn())
                            break
                        except Exception:
                            pass
                else:
                    try:
                        report["usage"] = {k: v for k, v in vars(usage).items() if not k.startswith("_")}
                    except TypeError:
                        report["usage"] = {"repr": repr(usage)}
        except Exception as e:
            report["errors"].append(f"session.retrieve: {type(e).__name__}: {e}")

        # ---- Extract agent's PROBE_RESULT marker ----
        final_text = extract_final_text(transcript)
        probe_result = parse_probe_result(final_text)
        report["probe_result"] = probe_result
        report["transcript_len"] = len(transcript)

        # ---- Q1d: extract HTTP status from agent's step-4 output ----
        if probe_result and "http_status" in probe_result:
            report["q1d_agent_side_curl"] = {
                "reachable": "200" in probe_result["http_status"] or "401" in probe_result["http_status"] or "403" in probe_result["http_status"],
                "status_line": probe_result["http_status"],
                "interpretation": (
                    "network_ok_no_creds" if "401" in probe_result["http_status"]
                    else "network_ok_succeeded_unexpectedly" if "200" in probe_result["http_status"]
                    else "network_ok_forbidden" if "403" in probe_result["http_status"]
                    else "network_blocked_or_dns_fail"
                ),
            }

    except Exception as e:
        report["errors"].append(f"probe: {type(e).__name__}: {e}")
        print(f"[probe] EXCEPTION: {type(e).__name__}: {e}")

    finally:
        # Archive in reverse creation order so we don't get dependency errors
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
                print(f"[probe] warning: failed to archive {kind} {getattr(resource, 'id', '?')}: {e}")
        # Leave uploaded files in place for post-hoc inspection.

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
    print("=" * 72)
    print("MANAGED AGENTS EDITOR PROBE — Q1 RE-RUN — DRY RUN (no API calls)")
    print("=" * 72)
    print()
    print("Estimated cost: $0.20–$0.40 (one session, ~45-60s agent work)")
    print()
    print("What this re-probe tests that the prior one didn't:")
    print("  1. Correct SDK method: sessions.resources.add(...) (was .create)")
    print("  2. Three files.upload variants testing kwargs acceptance:")
    print("     - variant_a: no extra kwargs (baseline)")
    print("     - variant_b: purpose='session_input'")
    print("     - variant_c: purpose='shared_resource'")
    print("  3. Agent-side HTTP upload fallback:")
    print(f"     curl -X POST https://api.anthropic.com/v1/files -H 'anthropic-beta: {BETA_HEADER}'")
    print("     → tells us if the container has outbound reachability")
    print()
    print("Call sequence:")
    print("  1. client.beta.files.upload(...) × 3 variants (baseline + 2 purpose values)")
    print("  2. client.beta.environments.create(packages={apt:[ffmpeg], npm:[hyperframes]})")
    print(f"  3. client.beta.agents.create(model={MODEL_PRIMARY!r}, system=<{len(SYSTEM_PROMPT)} chars>)")
    print("  4. client.beta.sessions.create(agent=<id>, environment_id=<id>)")
    print("  5. client.beta.sessions.resources.add(session_id, file_id=, type='file',")
    print("       mount_path=) × up to 3 times (once per successful upload)")
    print("  6. client.beta.sessions.events.send(user.message) — kick off agent work")
    print("  7. drain event stream to session.status_idle")
    print("  8. client.beta.files.download(file_id) × up to 3 — check for")
    print(f"     AGENT_WRITE_MARKER={AGENT_WRITE_MARKER!r} in returned bytes")
    print("  9. archive session + agent + environment")
    print()
    print("What STAYS decided from prior probe (not re-tested):")
    print("  - Q2 user.define_outcome: marker parser stays for v1")
    print("  - Q3 usage shape: cache_creation is nested dict, formula updated")
    print("  - Vaults: orthogonal to Editor commit")
    print()
    print("Findings written to:")
    print(f"  - {REPORT_DIR / 'report.json'}")
    print(f"  - {REPORT_DIR / 'transcript.txt'}")
    print()
    print("To execute, rerun with --run")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Q1-focused re-probe of the Managed Agents FileResource "
        "bidirectionality question."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="actually execute the probe (spends real API credits)",
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

    report = None
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
    if report is None:
        print("all model attempts failed", file=sys.stderr)
        return 3

    print("\n=== Q1 FINDINGS ===")
    summary = {
        "Q1a_upload_variants": [
            {
                "variant": u["variant"],
                "upload_ok": u.get("upload_ok"),
                "kwargs_sent": u.get("kwargs_sent"),
                "error": u.get("error"),
            }
            for u in report.get("q1a_upload_variants", [])
        ],
        "Q1b_mounts": report.get("q1b_mounts", []),
        "Q1c_bidirectional_by_variant": [
            {"variant": a["variant"], "bidirectional": a.get("bidirectional")}
            for a in report.get("q1c_download_attempts", [])
        ],
        "Q1d_agent_side_curl": report.get("q1d_agent_side_curl"),
        "usage": report.get("usage"),
        "errors": report.get("errors", []),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
