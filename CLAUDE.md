# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`rectoverso` is a multi-agent AI filmmaking pipeline built for the "Built with Opus 4.7" hackathon (Apr 21–26, 2026). Input: a creative brief. Output: an assembled short film (shots + voiceover/SFX + FCPXML edit timeline), produced autonomously by a Producer orchestrator coordinating specialist agents through a shared shot manifest.

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
- **Vertex AI Veo 3.1 Fast** — establishing/cinematic moments only. **Hard $15 spend cap across the whole project.** **Never humans** (EU restriction is a router hard rule, not a preference).

**Day-4 consideration** (reference-image generation for Kling I2V subject consistency, if time allows):
- Qwen-image-2.0, Google nano-banana. Not integrated for Days 1–3. Reference images in v1 come from `inputs/refs/` manually.

**Deferred (not this week)**: Runway Gen-4, Imagen 4, LTX, Seedance, Wan 2.1-VACE-Plus (video-edit tool, wrong use case).

The router is the core IP. Its contract:
- Input: `ShotSpec(has_humans, is_hero, motion_level, duration_s, budget_remaining_usd, prior_failures)`
- Output: `ProviderChoice(provider_id, model_id, estimated_cost_usd, rationale)`
- Decision priority: (1) hard rules from `router/capabilities.yaml` (humans_never_veo, veo_spend_cap, duration_bound, global_budget_cap), (2) prior session failures, (3) cost, (4) tier preference.
- Every hard rule must have an isolated unit test in `tests/router/`.

## Demo mode

`DEMO_MODE=1` makes provider adapters read from `demo/fixtures/` instead of calling live APIs. Fixtures get generated on Day 6 before recording. **Required** for the demo video — a 4-minute Veo call mid-recording kills the take. Don't rely on live APIs in the recorded demo.

## Budget

Actual available funds:
- **Anthropic**: $500 credits (Opus 4.7 orchestration, Managed Agents).
- **fal.ai**: $136 total = 2 × $68 keys (Kling 2.x). Two keys are for failover and rate-limit resilience — renderer tries primary first, falls over to secondary on auth/quota error.
- **Alibaba Wan**: $0 USD, free quota (50–100 calls, expires 2026-07-19). Tracked as `budget.alibaba_quota_remaining`.
- **Vertex AI Veo**: $15 hard cap (specialty tier, 3–5 hero shots/film).
- **ElevenLabs**: $0 USD, 117,999 credits available (pre-paid). Tracked as `budget.elevenlabs_credits_remaining`. Audio Agent estimates credit cost before each call.

Total USD cap: **$151** ($136 fal + $15 Veo). Seed `budget.cap_usd: 151` in the first real manifest. Wan and ElevenLabs add zero USD; they're gated by their own quota counters.

Prompt caching is load-bearing for staying under the Anthropic budget. Managed Agents caches aggressively by default, but **the cache breaks on system prompt or skill-list changes** — once a config works, stop tweaking it.

## Non-goals (kills scope creep)

- Not building a general-purpose video platform. One 30–60s film, 8–15 shots, one genre slice.
- Not integrating every video provider. Two models on primary path is enough.
- Not building a web UI. CLI + manifest inspection is the interface.
- Not implementing manual FCP-style compositing (text overlays, effects). FCPXML out; a real editor can polish.
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
