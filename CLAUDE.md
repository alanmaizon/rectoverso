# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`rectoverso` is a multi-agent AI filmmaking pipeline built for the "Built with Opus 4.7" hackathon (Apr 21–26, 2026). Input: a creative brief. Output: an assembled short film (shots + voiceover/SFX + Hyperframes HTML composition rendered deterministically to MP4), produced autonomously by a Producer orchestrator coordinating specialist agents through a shared shot manifest.

The repository is a new-original-work submission — no code ported from prior projects. Planning context and architectural decisions live in [init.txt](init.txt) (conversation transcript that produced this design).

## Compliance (hackathon rules)

Per Anthropic's Discord clarification (Apr 21):
- OSS libraries and third-party APIs are allowed (Veo, fal.ai, ElevenLabs, any pip/npm package).
- The project must be new original work — no forking a prior codebase and submitting the delta.

Rules for this repo:
- No files copied from prior personal projects (author has a related Give(a)Go project; treat as reference only, never as source).
- No vendoring prior code as a dependency or git submodule.
- Domain knowledge transfer is fine (provider capabilities, prompt patterns, architectural lessons).
- When uncertain whether something counts as reuse, flag it rather than assume.

## Build-time vs. run-time Claude

Two different Claude surfaces operate here — don't conflate them:
- **Build-time**: Claude Code CLI (Opus 4.7) running in VSCode — the tool the developer uses to write this codebase.
- **Run-time**: Claude Managed Agents (Opus 4.7) — the orchestration fabric invoked when a user runs the pipeline.

Code added to this repo runs at **run-time**. It is consumed by Managed Agents; it is not Claude Code itself.

## Architecture — tiered agents

Not everything is a Managed Agent. Tier decides based on whether work is long-running, stateful, and tool-using:

| Tier | What | Why | Members |
|------|------|-----|---------|
| 1 | Orchestrator (Managed Agent) | Owns manifest, long session, cross-shot QC | `producer` |
| 2 | Specialists (Managed Agents) | Multi-turn, file ops, self-verification loops | `shot_judge`, `audio_agent`, `editor_agent`, `creative_director` |
| 3 | Plain Messages API | Single-turn, no tools, no state | `screenwriter`, `prompt_smith` |
| 4 | Workers (no LLM) | Deterministic API polling, file I/O | `renderer` (submits/polls video APIs), `ffmpeg` glue |

Coordination rule: **agents never talk to each other directly**. All handoffs go through the shot manifest. The Producer reads manifest state, invokes specialists as tools (synchronous: Shot Judge, Editor, Creative Director; async-parallel: Audio), and reconciles on restart.

Creative Director is the only agent with explicit aesthetic authority — it reads the whole manifest and writes ranked suggestions to `shots[].creative_feedback[]` at three trigger points (mid-production at 3/6/9 approved shots, mandatory pre-Editor full-film review, and tie-breaker when specialists contradict each other). The Producer owns all acting-on-feedback; Creative Director never writes `status`, `artistic_direction`, or invokes other agents. Full contract in [docs/agents.md](docs/agents.md); system prompt at [prompts/creative_director.md](prompts/creative_director.md).

When deciding where a new capability lives, apply the tier test — if it's stateless generation, don't make it a Managed Agent just to inflate the "managed agents used" count. The prize criterion rewards thoughtful application.

## Agent pair contracts (preprocessing / verification)

Before any tool dispatch, the Producer calls `src.contracts.validate_before_dispatch(agent, shot_id, manifest, ctx)`. This is the verification step that catches silent breakage — scenarios where a dispatch would succeed at the call site but produce plausible-but-wrong output downstream. Spec: [docs/contracts.md](docs/contracts.md). Five contracts, one module each in `src/contracts/`, every silent-breakage case has an isolated test in `tests/contracts/`.

Three things the preprocessing layer checks:
- **Intent** — is the dispatch well-formed? (revision on an unknown shot, Editor invoked without a target, creative-driven dispatch with no CD feedback)
- **Architecture** — does the pair invariant hold? (`compressibility_s` before Editor, `judge_notes` before revision, `artistic_direction` updated after CD feedback, CD authority honored before Editor)
- **Edge cases** — the failure modes in [docs/agents.md § Agent pair contracts](docs/agents.md#agent-pair-contracts) where silent drift produces wrong output rather than an error

Return shape: `list[Violation]` of warn-severity findings (log to `history[]`, continue), or raises `ContractViolation` for block-severity findings (halt). Contracts are pure functions over the manifest — no I/O, no mutation. The Producer is responsible for wrapping calls with `state/events.db` writes; contracts are event-free by design so that a fresh harness can re-run them deterministically against a recovered manifest.

Adding a new contract means: (1) a module in `src/contracts/` that calls `register(ContractName.X, check)`, (2) a row in the `contracts_for_dispatch` table, (3) an isolated silent-breakage test. Changing the registry is changing the enforcement surface.

## The shot manifest (keystone)

Single source of truth at `state/manifest.json`. Every agent reads inputs from it and writes outputs back via status transitions. This is what makes the pipeline resumable across sessions.

Schema (see `docs/manifest-schema.md` once created; for now use init.txt:502-631 as spec):
- Top-level: `manifest_version`, `project_id`, `brief`, `script`, `shots[]`, `audio`, `edit`, `budget`, `run_state`.
- Shot object: `shot_id`, `description`, `duration_s`, `continuity_refs`, `prompt`, `routing`, `attempts[]` (append-only), `final`, `status`, `history[]` (append-only).
- Status state machine: `created → prompted → routed → rendering → judging → approved`, with `rejected → rendering (retry)` and `failed → routed (fallback)` loops.

Rules:
- `attempts[]` and `history[]` are append-only. Never mutate prior entries.
- Validate against `schemas/manifest.schema.json` before every write. Fail loud on invalid writes.
- `run_state.resumable` is `false` during non-atomic operations, `true` when the manifest is consistent. On restart, if `false`, Producer reconciles before accepting new work.

SQLite (`state/events.db`) is the append-only event log (every provider call, cost, latency). The JSON manifest is the current shot state. SQLite is derivable from events; they should never disagree. If they do, SQLite wins.

## Provider priority

Role-based, not tier-ranked. Two classes:

**Workhorses** (the bulk of every film):
- **Alibaba Wan 2.7 Plus (14B)** — final/approved renders for non-hero, non-human shots.
- **Alibaba Wan 2.7 Turbo** — iteration loop + rejected-shot retries (speed-optimized, lower fidelity — not for finals).
- **Kling 2.x via fal.ai** — all human shots. Non-negotiable for humans.
- **ElevenLabs** — all audio.

Wan access is **free-quota metered**, not USD. Budget tracks `alibaba_quota_remaining` separately; USD cost for Wan is `0.0`. Quota expires 2026-07-19 (well past submission).

**Specialty** (3–5 hero shots per film, flagged `is_hero: true` by Screenwriter):
- **Vertex AI Veo 3.1 Fast** — establishing/cinematic moments only. **$300 GCP credits available**; operational cap is **$15 project-wide — a scope-control decision, not an availability limit.** The $15 bounds how much of the film is Veo-tier; $300 is the headroom for retries, demo-day safety, or Day-5 expansion if a scope change gets signed off. **Never humans** (EU restriction is a router hard rule, not a preference).

**Day-4 consideration** (reference-image generation for Kling I2V subject consistency, if time allows):
- Qwen-image-2.0, Google nano-banana. Not integrated for Days 1–3. Reference images in v1 come from `inputs/refs/` manually.

**Deferred (not this week)**: Runway Gen-4, Imagen 4, LTX, Seedance, Wan 2.1-VACE-Plus (video-edit tool, wrong use case).

The router is the core IP. Its contract:
- Input: `ShotSpec(has_humans, is_hero, motion_level, duration_s, budget_remaining_usd, prior_failures)`
- Output: `ProviderChoice(provider_id, model_id, estimated_cost_usd, rationale)`
- Decision priority: (1) hard rules from `router/capabilities.yaml` (humans_never_veo, veo_spend_cap, duration_bound, global_budget_cap), (2) prior session failures, (3) cost, (4) tier preference.
- Every hard rule must have an isolated unit test in `tests/router/`.

## Editor toolchain — Hyperframes

Editor Agent (Tier 2) renders the final film via **Hyperframes** ([hyperframes.heygen.com](https://hyperframes.heygen.com)) — an HTML-based composition framework with a deterministic `frame = floor(time * fps)` renderer. HeyGen, Apache-2.0, third-party OSS dependency. Verified end-to-end in a real Managed Agents cloud sandbox (see `scratch/hyperframes-probe/PROBE_REPORT.md`).

Why Hyperframes:
- **Agent-native**: non-interactive CLI (`npx hyperframes init/lint/render`), JSON lint output, plain-text progress on stdout. The bash tool in the Managed Agents toolset drives it directly.
- **Deterministic**: same input → bit-identical MP4. Enables snapshot-based regression tests on rendered output — genuinely rare in video pipelines.
- **Runtime fit**: Node 22+ and npm are pre-installed in the Managed Agents sandbox; declare `packages.apt: ["ffmpeg"]` in the environment config and Chrome auto-downloads at first render (~107 MB, one-time).
- **Claude skill ecosystem**: `hyperframes`, `hyperframes-cli`, `gsap` skills (installable via `npx skills add heygen-com/hyperframes`) encode framework-specific patterns. The Editor Agent invokes them before authoring compositions.

The schema pins `edit.renderer` to the constant `"hyperframes"`. Other editable-output fields (`edit.composition_path` for the HTML, `edit.composition_archive_path` for the downloadable zip, `edit.render_path` for the MP4, `edit.render_md5` for the determinism signature) cluster around it. If the renderer-choice space ever needs to widen again, that's a schema change and a test-suite change — not a flag flip.

The Python-side tool adapter is `src.producer.HyperframesTool` (`src/producer/hyperframes.py`) — a subprocess wrapper that runs `npx hyperframes lint --json` then `npx hyperframes render`, captures exit code + stdout/stderr tails + output MD5, and returns a dict that maps cleanly into a `dispatch_result` EventLog payload.

## Demo mode

`DEMO_MODE=1` makes provider adapters read from `demo/fixtures/` instead of calling live APIs. Fixtures get generated on Day 6 before recording. **Required** for the demo video — a 4-minute Veo call mid-recording kills the take. Don't rely on live APIs in the recorded demo.

## Budget

Actual available funds:
- **Anthropic**: $500 credits (Opus 4.7 orchestration, Managed Agents).
- **fal.ai**: $136 total = 2 × $68 keys (Kling 2.x). Two keys are for failover and rate-limit resilience — renderer tries primary first, falls over to secondary on auth/quota error.
- **Alibaba Wan**: $0 USD, free quota (50–100 calls, expires 2026-07-19). Tracked as `budget.alibaba_quota_remaining`.
- **Vertex AI Veo**: $300 GCP credits available; operational cap **$15** (specialty tier, 3–5 hero shots/film). $15 is the scope cap, not the availability cap — the Producer must still refuse any render that would push `spent_usd + estimated_cost_usd` past `cap_usd`. Raising `cap_usd` is a scope decision, not a bug fix.
- **ElevenLabs**: $0 USD, 117,999 credits available (pre-paid). Tracked as `budget.elevenlabs_credits_remaining`. Audio Agent estimates credit cost before each call.

Total USD cap: **$151** ($136 fal + $15 Veo). Seed `budget.cap_usd: 151` in the first real manifest. Wan and ElevenLabs add zero USD; they're gated by their own quota counters.

Prompt caching is load-bearing for staying under the Anthropic budget. Managed Agents caches aggressively by default, but **the cache breaks on system prompt or skill-list changes** — once a config works, stop tweaking it.

## Non-goals (kills scope creep)

- Not building a general-purpose video platform. One 30–60s film, 8–15 shots, one genre slice.
- Not integrating every video provider. Two models on primary path is enough.
- Not building a web UI. CLI + manifest inspection is the interface.
- Not implementing a manual compositing GUI. The Editor Agent assembles an HTML composition (Hyperframes) rendered deterministically to MP4 — single renderer, no alternate formats. If the render loop exhausts, it escalates; it does not silently ship a different artifact.
- Not writing tests for every branch — router decisions, manifest validation, and state transitions need tests. Provider adapters can rely on fixture replay.

## Deadlines

- **Submission: Sun Apr 26, 8 PM EST.** Stop building by 4 PM EST; final 4 hours are for demo video.
- Mon Apr 27: async round 1 (top 6).
- Tue Apr 28, 12 PM EST: live final.

## Working with Claude Code on this repo

- Use plan mode (`shift+tab` once) for any multi-file change. Commit the plan, then execute.
- Use auto mode (`shift+tab` three times) once the setup is trusted — not dangerous mode.
- `/loop` is well-suited to router test regressions — "re-run router tests, fix any regressions" as a background loop.
- At the end of each Claude Code session, append progress notes to `HACKATHON_LOG.md`. This feeds the demo video script on Sunday.
- Claude Code has no memory between sessions. If context matters, it goes in a file — CLAUDE.md, `docs/`, or `HACKATHON_LOG.md`.
