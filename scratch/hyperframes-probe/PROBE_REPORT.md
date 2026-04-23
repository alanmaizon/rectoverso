# Hyperframes probe — findings

**Date**: 2026-04-22 (Day 2)
**Probe scope**: Verify Hyperframes is agent-compatible, deterministic, and monitorable from a Producer-like Python dispatcher before committing to any architectural pivot.
**Outcome**: ✅ All six probe criteria pass. Recommend conditional pivot (see bottom).

> **Superseded (2026-04-23):** This probe report exists as the audit trail for the Editor-renderer decision. Where it speaks of an "FCPXML fallback," that reflects exploratory design at probe time and does NOT match production policy. After Managed Agents sandbox verification succeeded in default mode (§ Condition evaluation below), FCPXML was dropped entirely. **Hyperframes is the sole renderer.** No alternate-format fallback exists in the current schema, prompts, or adapter.

---

## Environment

| Component | Version | Status |
|---|---|---|
| Node | v25.9.0 | ≥22 required, ✅ |
| FFmpeg | 8.1 | ✅ |
| npm | 11.12.1 | ✅ |
| Platform | Darwin 25.4.0 (Apple Silicon) | Managed Agents sandbox compatibility NOT yet verified |
| Chrome | Auto-downloaded by Hyperframes, ~84 MB one-time | Cached under Puppeteer; subsequent runs skip download |

---

## Probe results

### 1. Init — non-interactive, agent-friendly ✅

```bash
npx hyperframes init hyperframes-probe --non-interactive --example blank
```

Produces a clean project tree in ~5s:
```
hyperframes-probe/
├── AGENTS.md          # project-level agent conventions (58 lines)
├── CLAUDE.md          # Claude Code specific skill pointers (73 lines)
├── hyperframes.json   # project config; points at GitHub registry
├── index.html         # root composition; GSAP preloaded
└── meta.json          # project id/name
```

The `AGENTS.md` states hard constraints the framework enforces: `data-start`/`data-duration`/`data-track-index` required on timed elements; `class="clip"` mandatory on visible timed elements; GSAP timelines must be `paused` and registered to `window.__timelines`; **no `Date.now()`, `Math.random()`, or network fetches in composition code** (determinism guarantee).

### 2. Lint — structured JSON preflight ✅

```bash
npx hyperframes lint --json
```

Emits `{"ok": true, "errorCount": 0, "warningCount": 0, "infoCount": N, "findings": [...], "filesScanned": N, "_meta": {...}}`. Exit code 0 on success, non-zero on errors. Parseable by any agent; perfect preflight gate before the more expensive render.

### 3. Render — non-interactive by default ✅

```bash
npx hyperframes render --output render1.mp4
```

Interactive TUI is opt-in via `--human-friendly`; default mode is the one agents want. Stdout is a structured progress stream (compile → frame extract → audio → capture → encode → assemble), each stage with a clear label and percentage.

First render: 17.9s wall time (includes 84 MB Chrome download).
Second+ renders: **2.0s** for a 3-second 1920×1080 @ 30fps composition with a GSAP tween.

### 4. Determinism — bit-identical across invocation methods ✅

Three independent render invocations (two via shell `npx`, one via Python `subprocess.run`) all produced identical MD5:

```
MD5 (render1.mp4)              = b29b84cc8432ffe33259af6beb23f1e1
MD5 (render2.mp4)              = b29b84cc8432ffe33259af6beb23f1e1
MD5 (render_from_python.mp4)   = b29b84cc8432ffe33259af6beb23f1e1
```

This is the genuinely rare property. It means our test suite can snapshot MP4 bytes and assert bit-identical output — no `"render looks different this time"` flakiness. Implication for our pipeline: we can write snapshot regressions for the Editor Agent's output in CI.

### 5. Agent-driven workflow — clean surface for Producer dispatch ✅

[drive_from_python.py](drive_from_python.py) simulates the Editor tool adapter. It writes nothing the Producer runtime doesn't already need: subprocess invocation, stdout/stderr capture, exit code, duration, output MD5 + size. Every field maps cleanly into our `dispatch_result` event payload.

Key fields for the EventLog:
- `exit_code` (int)
- `duration_s` (float)
- `output_path` (relative to project)
- `output_size_bytes` (int — sanity check for silent zero-byte failures)
- `output_md5` (str — snapshot assertion + cross-run comparison)
- `stdout_tail` (500 chars — progress trail for the audit)
- `stderr_tail` (500 chars — empty on success in our probe)

### 6. Skills install — works, multi-agent aware ✅

Two equivalent entry points:
- `npx hyperframes skills` (interactive picker; uses the bundled CLI)
- `npx skills add heygen-com/hyperframes` (generic, also interactive; uses the skills CLI)

Installs five skills to `.agents/skills/` with rich reference material:
- `hyperframes` (SKILL.md + 10+ reference files on captions, transitions, motion principles, typography, TTS, audio-reactive, etc.)
- `hyperframes-cli` — init/lint/preview/render/transcribe/tts
- `hyperframes-registry` — the `hyperframes add` block/component pattern
- `website-to-hyperframes` — website-URL → video pipeline
- `gsap` — GSAP tweens/timelines/easing tailored for Hyperframes

Installation is multi-agent: the installer symlinks skills for Claude Code, Codex, Cursor, Amp, and 20+ others. That's a big "ecosystem" win for our demo narrative — we're using infrastructure designed to work across agent harnesses, which matches the scaling_managed_agents "stable interfaces" principle.

---

## Answers to the user's two questions

### Q1 — "Will the agents be able to use Hyperframes as a workspace?"

Yes, with a clean mental model: **the Hyperframes project directory is the Editor Agent's workspace; `index.html` is the durable state; `npx hyperframes render` is the projection**. That mirrors our existing manifest/events split.

Concretely, the Editor Agent's job becomes:
1. Read approved shots from `state/manifest.json`.
2. Read audio files from `artifacts/audio/`.
3. Emit/update `artifacts/edit/index.html` (the composition) + copy assets into `artifacts/edit/assets/`.
4. Run `npx hyperframes lint --json` (preflight — fail-loud on invalid composition).
5. Run `npx hyperframes render --output artifacts/edit/final.mp4`.
6. Write `edit.composition_path`, `edit.render_path`, `edit.status` to the manifest.

Every step is file-and-subprocess based. No GUI, no Final Cut dependency, no interactive prompts. This is the Tool Protocol we already defined.

### Q2 — "How will we monitor that?"

The monitoring surface falls out of our existing architecture without new machinery:

- **Dispatch events**: every `npx hyperframes render` call wraps in our `dispatch()` function, which writes `dispatch_intent` → `dispatch_result` (or `dispatch_failure`) to `state/events.db`. Stdout, stderr, exit code, render time, output MD5, and output size all land in the event payload.
- **Preflight validation**: `lint --json` output becomes part of the intent event, so the audit trail shows every preflight decision before the render was attempted.
- **Deterministic regression**: bit-identical MD5s mean Hyperframes outputs can be snapshot-asserted in tests — a class of regression we currently can't write for FCPXML.
- **Live preview for the demo video**: `npx hyperframes preview` runs a live-reload server on localhost. Good for recording "watch the composition evolve as the Editor agent edits the HTML" — visually compelling for the demo.
- **Unchanged**: budget accounting, contract violations, shot status transitions — all continue to flow through the existing Producer invariants. Hyperframes adds a new tool adapter; it doesn't change the Producer's observability story.

---

## Gaps / caveats

1. **Managed Agents sandbox compatibility is NOT yet proven.** The probe ran on macOS Apple Silicon with no sandbox. Linux containers with seccomp/namespace restrictions may or may not support the Puppeteer + headless Chrome chain (Puppeteer is often run with `--no-sandbox` in containers; Managed Agents sandboxes may block that). Verify before any prompt/runtime rewrite lands.
2. **Cold-start cost.** First render downloads 84 MB of Chrome. For the demo-mode run this is one-time, but the first run in a fresh container takes ~18s extra. Pre-baking a Chrome install into the container image eliminates this.
3. **Composition bundles use CDN.** The bundled `index.html` references `cdn.jsdelivr.net/npm/gsap@3.14.2`. Hyperframes *inlines* this at compile time (visible in our stdout: `[Compiler] Inlined CDN script`), so run-time rendering doesn't need network. But the first compile does. Plan accordingly for offline demo mode.
4. **Editor Agent output shape changes.** From FCPXML (single file) to Hyperframes (HTML + assets directory). Schema updates in `edit.*` fields. Prompt rewrite needed. All localized; no downstream contract or runtime changes.
5. **Render time scales with video content.** Our probe is zero-video (text-only title). Real films with MP4 shot clips will render slower; Hyperframes extracts frames via FFmpeg from input videos. Budget ~0.5–2× real-time render depending on codec and resolution.

---

## Pivot recommendation

**Conditional yes.** Conditions, in order:

1. **Before touching production prompts/schema**: prove Managed Agents sandbox can run `npx hyperframes render`. Spin up a minimal Managed Agent session, shell out to the same commands we ran here. If Chrome can't launch in the sandbox, stop — fall back to FCPXML. If it can, proceed.
2. **Before rewriting Editor Agent runtime**: have at least one real agent dispatch (`shot_judge` or `prompt_smith`) producing an actual Claude API response through our `dispatch()`. Don't debug two new things simultaneously on Saturday.
3. **Then pivot in this order**:
   a. Update [schemas/manifest.schema.json](../../schemas/manifest.schema.json) — generic `edit.composition_path`, `edit.renderer` (="hyperframes"), `edit.renderer_version`. Keep `fcpxml_path` optional for fallback.
   b. Update [prompts/editor_agent.md](../../prompts/editor_agent.md) — output spec section, self-verification loop.
   c. Update [docs/agents.md § Tier 2 — Editor Agent](../../docs/agents.md) — Skills row references `hyperframes-compose` skills.
   d. Update [CLAUDE.md](../../CLAUDE.md) — remove "Not implementing manual FCP-style compositing" non-goal or reshape it; note that Hyperframes rendering is now the default path; FCPXML becomes fallback (per editor_agent.md § Fallback) or roadmap.
   e. Add a `hyperframes` tool adapter under `src/producer/` (new module — wraps the subprocess call and maps results into `DispatchResult`).

The existing contracts layer (`src/contracts/`), Producer runtime (`src/producer/` dispatch/events/manifest_io), Router, Tier-2 prompts other than Editor, and Tier-3 prompts — all untouched by this pivot. That's the architectural win. The Editor was always the thinnest tier-2 agent precisely because its job is narrow: read manifest → emit composition → render. Hyperframes fits the shape of that job better than FCPXML did.

---

---

## Condition evaluation (2026-04-23 follow-up)

Performed on 2026-04-23 to evaluate whether the two pre-pivot conditions in § Pivot recommendation are satisfied.

### Condition 1 — Managed Agents sandbox compatibility

**Evaluation: VERIFIED (empirical, end-to-end).**

Executed 2026-04-23 via [scratch/managed_agents_hyperframes_probe.py](../managed_agents_hyperframes_probe.py). A real Managed Agents session (beta header `managed-agents-2026-04-01`) was created, ran the six-step verification, and produced an MP4 inside the cloud sandbox.

**Probe result (agent-emitted, JSON-parsed):**
```json
{
  "verdict": "PASS",
  "mp4_bytes": 27346,
  "mp4_md5": "a0d5625a16271e0274563466ab36ee4e",
  "notes": "all 6 steps ok; node 22.22.2, ffmpeg 6.1.1, hyperframes 0.4.12, lint clean, 10s blank render"
}
```

**Sandbox runtime (observed, not promised):**
| Component | Version in the sandbox | Source |
|---|---|---|
| OS | Ubuntu (22.04-class; `ubuntu5` suffix on ffmpeg package) | Managed Agents default image |
| Node | 22.22.2 | Pre-installed |
| npm | 10.9.7 | Pre-installed |
| FFmpeg | 6.1.1-3ubuntu5 | Declared via `packages.apt: ["ffmpeg"]` |
| Chrome | Downloaded at render-time, 107.4 MB | Hyperframes auto-downloads via Puppeteer |

**What the agent actually did (excerpts from the transcript):**
1. `node --version` / `npm --version` → ok
2. `ffmpeg -version | head -1` → `ffmpeg version 6.1.1-3ubuntu5`
3. `npx --yes hyperframes@latest init probe --non-interactive --example blank` → created probe dir
4. `cd probe && npx hyperframes lint --json` → `{"ok": true, errorCount: 0}`
5. `cd probe && npx hyperframes render --output out.mp4` → downloaded 107.4MB Chrome, rendered in ~10s of wall time within the larger 28s step
6. `ls + md5sum /tmp/hf/probe/out.mp4` → 27346 bytes, `a0d5625a16271e0274563466ab36ee4e`

Total session wall time: **77 seconds** (environment provisioning + Chrome download + render + audit). Session terminated cleanly on `session.status_idle`. Environment, agent, and session were archived on exit to avoid billing residue.

**What this proves:**
- The Managed Agents cloud sandbox supports the full Puppeteer + Chrome + FFmpeg chain Hyperframes requires.
- Chrome download inside the sandbox works (network reachability + disk + extraction).
- The default `npx hyperframes render` path works without `--docker` or any sandbox-specific flags.
- Running Hyperframes through the Managed Agents bash tool is a viable Editor Agent implementation path.

**What this does NOT prove** (worth noting but not blocking):
- Render determinism across Mac vs sandbox: the probe's sandbox MD5 (`a0d5625a…`) differs from the Mac MD5 (`b29b84cc…`). That's because the sandbox rendered the DEFAULT blank template, while the Mac rendered our custom `index.html` with a title. Cross-platform bit-for-bit determinism is a follow-up measurement, not a prerequisite for the pivot.
- Rendering with real video assets (we used a text-only composition). Larger compositions with MP4 inputs will take longer and exercise FFmpeg decode paths we didn't test here. Budget ~0.5–2× real-time per [report § 5 Gaps caveats](#gaps--caveats).
- Long-session stability (our session was 77s; no issue observed).

### Condition 2 — Real tier-2 agent dispatch through our runtime

**Evaluation: VERIFIED.**

Probe script: [scratch/real_dispatch_probe.py](../real_dispatch_probe.py). One-shot PromptSmith dispatch against `sh_001` of a fixture manifest.

Execution record:
- Env: rebuilt project venv, installed `anthropic==0.96.0`.
- API call: real `client.messages.create()` against `claude-opus-4-5-20251101` (4-7 not yet available on API — swap in production when it lands).
- Tokens: 2965 input, 57 output, `stop_reason: end_turn`.
- Dispatched via `src.producer.dispatch(agent="prompt_smith", shot_id="sh_001", manifest, ctx, tool, events)` — our existing Producer runtime, unchanged.
- Output (Claude's actual response, PromptSmith system prompt loaded from [prompts/prompt_smith.md](../../prompts/prompt_smith.md)):

  > "Wide establishing shot of a lighthouse at dawn, mist slowly clearing from the rocks, cold natural light, no figures visible, handheld camera with subtle drift, naturalistic, quiet atmosphere."

- Quality check: honors brief tone (`quiet`, `solitary`), artistic_style (`handheld`, cold palette), `has_humans: false` (explicit "no figures"), `motion_level: low` (`subtle drift` not "quick pan"), Wan provider grammar (physically-grounded, no negatives).
- events.db trail:
  ```
  #1 dispatch_intent    shot=sh_001 ref=None  payload_keys=['ctx']
  #2 dispatch_result    shot=sh_001 ref=1     payload_keys=['result']
  ```
- Contracts fired correctly: no `revision` flag → `shot_judge_to_prompt_smith` contract skipped, no `creative_driven` → `cd_to_prompt_smith` skipped. Clean run, zero warns.

**Everything works**: contract registry routing, dispatch wrapper, event log, Tool Protocol, Anthropic SDK integration, system-prompt loading, JSON output parsing. End-to-end proof of life for the Producer runtime with a real agent.

### Overall verdict

| Condition | Status | Notes |
|---|---|---|
| C1 — Managed Agents sandbox compat | ✅ **Verified** (end-to-end) | Real Managed Agents session rendered an MP4 (27346 bytes, md5 a0d5625a…); Node 22.22.2 + ffmpeg 6.1.1 + Chrome 107.4MB download all worked |
| C2 — Real tier-2 dispatch | ✅ Verified | PromptSmith via Anthropic API through `dispatch()`, events.db confirms audit trail |

**Both conditions satisfied. Pivot unblocked.**

Recommended sequence remains as written in the original § Pivot recommendation:
- 3a. schema update (generic `edit.composition_path`, `edit.renderer`, `edit.renderer_version`; keep `fcpxml_path` optional for fallback)
- 3b. `prompts/editor_agent.md` rewrite
- 3c. `docs/agents.md § Tier 2 Editor Agent` update
- 3d. `CLAUDE.md` update (§ Non-goals, § Architecture)
- 3e. `src/producer/hyperframes.py` — tool adapter wrapping the subprocess call into a `DispatchResult`

FCPXML fallback remains in the spec for the editor's self-verification-fails path; this isn't defensive scaffolding, it's a real resilience feature the editor prompt already names. No code changes beyond the five items above.

---

## Deliverables (in this probe directory)

- [AGENTS.md](AGENTS.md), [CLAUDE.md](CLAUDE.md), [hyperframes.json](hyperframes.json), [meta.json](meta.json) — generated by `hyperframes init`
- [index.html](index.html) — minimal composition with GSAP tween
- [render1.mp4](render1.mp4), [render2.mp4](render2.mp4), [render_from_python.mp4](render_from_python.mp4) — three deterministic renders, identical MD5
- [drive_from_python.py](drive_from_python.py) — reference implementation for the Producer's Editor tool adapter
- [.agents/skills/](.agents/skills/) — five installed skills (hyperframes, hyperframes-cli, hyperframes-registry, website-to-hyperframes, gsap)
- [sandbox_check.mp4](sandbox_check.mp4) — constrained-Puppeteer render (Condition 1 Mac sub-probe); bit-identical MD5
- [../real_dispatch_probe.py](../real_dispatch_probe.py) — end-to-end real-agent dispatch probe (Condition 2)
- [../managed_agents_hyperframes_probe.py](../managed_agents_hyperframes_probe.py) — Managed Agents cloud sandbox probe (Condition 1 verification)
- [managed_agents_probe_transcript.txt](managed_agents_probe_transcript.txt) — full transcript of the sandbox session
- [managed_agents_probe_stdout.txt](managed_agents_probe_stdout.txt) — runner stdout with timestamps
