# rectoverso — submission summary

**A multi-agent AI filmmaking pipeline. Brief in, assembled short film out.**

Live site: [alanmaizon.github.io/rectoverso](https://alanmaizon.github.io/rectoverso/) · Repo: [github.com/alanmaizon/rectoverso](https://github.com/alanmaizon/rectoverso) (public, Apache 2.0)

## What it does

You hand `rectoverso` a creative brief — logline, target duration, tone, genre. A Producer orchestrator coordinates a tiered set of Claude Opus 4.7 agents to break the brief into shots, route each shot to the right video provider (Veo, Kling, or Wan) under hard budget caps, render and judge each shot with a self-verification loop, integrate cross-shot creative feedback at three trigger points, generate dialogue and SFX, and assemble the final composition deterministically as MP4. End-to-end, autonomously, resumably.

## Who benefits

Independent filmmakers, solo creators, and small studios for whom a 30–60 second film today means a multi-week pipeline of writing, shot-listing, prompting, rendering across heterogeneous providers, judging, revising, and assembling. `rectoverso` collapses that into a single agentic run with a verifiable audit trail. The same architecture generalizes to any long-horizon creative pipeline that needs to coordinate specialist work under budget and quality constraints — pre-visualization for working productions, ad creative iteration, narrative prototyping for game cinematics.

## Why it's a credible Managed Agents application

The judging criterion for the Managed Agents prize rewards thoughtful application, not headcount. `rectoverso` runs four tiers and is deliberate about which work belongs at which tier:

- **Tier 1 (Managed Agent)** — Producer orchestrator. Long session, owns the shared manifest, runs cross-shot QC, reconciles on restart.
- **Tier 2 (Managed Agents)** — Shot Judge, Audio Agent, Editor Agent, Creative Director. Multi-turn, file ops, self-verification loops.
- **Tier 3 (Messages API)** — Screenwriter, PromptSmith. Single-turn, no tools, no state. Wrapping these as Managed Agents would inflate the count without serving the architecture.
- **Tier 4 (no LLM)** — Renderer, Router, ffmpeg. The provider router is a pure Python function over a YAML capability matrix with twelve hard rules and an isolated unit test for each. Determinism beats LLM judgment here.

**Agents never talk to each other directly.** Every handoff goes through `state/manifest.json`, validated against a JSON Schema before every write, mirrored by an append-only SQLite event log. This is the load-bearing decision: it makes the pipeline resumable across sessions, the creative loop auditable from observation → suggestion → action, and the system debuggable when something goes wrong at hour three of a long run.

Five **agent-pair contracts** (`src/contracts/`) catch silent-breakage cases pre-dispatch — scenarios where a tool call would succeed superficially but produce plausible-but-wrong output downstream. Each contract is a pure function over the manifest with an isolated test for the failure case it prevents.

The **Editor Agent** runs in a real Anthropic Managed Agents cloud sandbox. It uses HeyGen's Hyperframes (Apache-2.0) — an HTML-based composition framework with a deterministic `frame = floor(time * fps)` renderer — making the final film bit-identical across runs. Same input, same MD5. The rendered film itself becomes a regression-test primitive.

## Why Opus 4.7 specifically

Three places where 4.7's strengths are load-bearing, not optional:

1. **The Creative Director agent** reads the whole film as a coherent artistic object — narrative arc, pacing, tonal consistency against the brief — and writes ranked, structured suggestions the Producer can act on without re-interpreting. This is exactly the kind of long-context, aesthetic-judgment work earlier models hedge on.
2. **The Shot Judge** does vision-grounded scoring across composition, prompt adherence, and continuity, with concrete rejection notes that PromptSmith can act on. A judge whose notes are vague produces a pipeline that loops forever; 4.7's specificity is what closes the loop.
3. **The Producer orchestrator** runs for hours across a single Managed Agents session, holding the whole manifest in working memory, reconciling on restart, deciding when to invoke Creative Director vs. when to escalate. Long-horizon coordination with a stable view of state is the prize-winning capability and the thing 4.7 visibly does well.

## What's in the demo

The recorded demo runs `RECTOVERSO_DEMO_MODE=1` — a deliberate seam specified on Day 1, *not* a workaround. The mode swaps `MockEditorSession` in for the live Managed Agents session via `EditorTool.from_env(demo_mode=True)`; every contract, schema validation, and orchestrator path runs unchanged against fixtures generated from real provider runs earlier in the week. A 4-minute Veo render mid-recording would kill the take; demo mode protects that. The site at the link above is live, populated with real manifest state and event traces from end-to-end runs, and renders the final 1:04 film inline.

The pipeline ran live, end-to-end, against real APIs through Day 3: Veo establishing shots scoring 0.917, Wan revision loops closing 0.683 → 0.907 in one judge-driven retry, Kling I2V with Qwen-generated reference frames hitting 0.757 on a previously-failing human shot. Three providers, full Audio Agent + Editor Agent + Hyperframes render, deterministic MD5 verified across back-to-back runs.

## Where to look in the repo

- `CLAUDE.md` — architecture, tiering, compliance
- `docs/thesis.md` — extended thesis: *can agents direct a film?*
- `docs/agents.md` — per-agent contracts, including the agent-pair contracts table
- `docs/contracts.md` — the five pre-dispatch contracts, executable spec
- `docs/architecture.md` — system diagrams, data model, event-log schema
- `HACKATHON_LOG.md` — six-day rolling engineering journal (read bottom-up for chronological order)
- `tests/` — 403 passing tests, including isolated tests per router hard rule and per contract silent-breakage case
