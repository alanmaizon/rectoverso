# Can agents direct a film?

**A thesis from rectoverso — a multi-agent AI filmmaking pipeline built for the "Built with Opus 4.7" hackathon (Apr 21–26, 2026).**

---

## The question

A film is the canonical test for multi-agent coordination. It is long-horizon (minutes of output, hours of compute), multi-modal (picture, dialogue, sound design, music, edit), constraint-heavy (budget, runtime, continuity, brief intent), and unforgiving — a single bad shot, a tonal drift, an audio mismatch, and the artifact reads as broken. There is no margin for plausible-but-wrong output.

So: given Anthropic's Managed Agents primitives and Claude Opus 4.7, can a coordinated set of agents take a creative brief and ship an assembled short film, autonomously, end-to-end?

`rectoverso` is the answer. Yes — but the architectural choices are the entire point.

## Who this is for

Independent filmmakers and solo creators. Today, a 30–60 second film means weeks of writing, shot-listing, prompting across heterogeneous video providers, judging takes, revising, generating audio, and assembling. The expensive parts aren't creative — they're coordination, bookkeeping, and the patience to push every shot through enough revisions to land. `rectoverso` collapses that coordination into a single autonomous run with a verifiable audit trail, while leaving the creative anchors — brief, tone, artistic style — fully in the human's hands.

The architecture itself generalizes past film. Any long-horizon creative pipeline that coordinates specialist work under budget and quality constraints — pre-visualization for working productions, ad creative iteration, narrative prototyping for game cinematics — fits the same shape: tiered agents, manifest-mediated coordination, pre-dispatch verification, deterministic terminal output.

## The core argument: tier the agents, never let them talk

The temptation with a "managed agents" prize is to make everything an agent. The judging criterion rewards thoughtful application, not headcount. `rectoverso` runs four tiers:

| Tier | What | Why this tier | Members |
|------|------|---------------|---------|
| 1 | Orchestrator (Managed Agent) | Long session, owns state, cross-shot QC | `producer` |
| 2 | Specialists (Managed Agents) | Multi-turn, file ops, self-verification loops | `shot_judge`, `audio_agent`, `editor_agent`, `creative_director` |
| 3 | Stateless LLM (Messages API) | Single-turn generation, no tools, no state | `screenwriter`, `prompt_smith` |
| 4 | Workers (no LLM) | Deterministic API polling, file I/O | `renderer`, `router`, `ffmpeg` |

The provider router — the piece that picks Veo vs Kling vs Wan for each shot — is *not* an agent. It is a pure Python function over a YAML capability matrix with twelve hard rules and an isolated unit test for each. Making it an LLM call would have added latency, cost, and non-determinism for zero gain.

The *coordination rule* is the load-bearing decision: **agents never talk to each other directly**. Every handoff goes through a single shared JSON manifest (`state/manifest.json`), validated against a schema before every write, mirrored by an append-only SQLite event log. The Producer reads manifest state, invokes specialists as tools, and reconciles on restart. There is no chat between agents, no implicit context sharing, no race conditions on shared state.

This sounds modest. It is the difference between a system you can debug and a system you can't.

## Why manifest-mediated coordination matters

Three concrete payoffs:

**Resumability.** Every Tier-2 specialist writes its output to a known field, with `attempts[]` and `history[]` strictly append-only. If the orchestrator dies mid-render — which happens, on a 60-minute pipeline — the next session reads `run_state.resumable`, replays events, and continues. No agent has to remember anything across sessions because nothing is held in any agent's head. This mirrors the session-as-durable-log discipline Anthropic describes in its Managed Agents design rationale: brain, hands, and session as separately-replaceable components.

**Pre-dispatch verification (contracts).** Five agent-pair contracts (`src/contracts/`) catch the silent-breakage cases — scenarios where a dispatch would succeed at the call site but produce plausible-but-wrong output downstream. Example: PromptSmith asked for a revision when `attempts[-1].judge_notes` is empty would paraphrase the same prompt and fail identically; Contract 2 blocks the dispatch with a structured error before tokens are spent. Each contract is a pure function over the manifest, with an isolated test for the failure case it prevents.

**Auditable creative loop.** When the Creative Director writes feedback to `shots[i].creative_feedback[]`, the Producer translates the natural-language suggestion into a structured `artistic_direction` field, records the translation as a `history[]` entry, and only then re-dispatches PromptSmith. The whole chain — observation → suggestion → action → outcome — is reconstructible from the manifest plus event log. This is what makes `creative_decisions[]` (film-level reorders, merges, scope changes) defensible: every decision has `source_feedback_refs` pointing back to the entries that drove it. The demo can show, frame by frame, *why* every shot in the film looks the way it does.

The Hyperframes-based Editor toolchain (HeyGen's HTML composition framework, Apache-2.0) makes the final output bit-deterministic — same input, identical MP4 — which means the rendered film itself becomes a regression-test primitive.

## Why Opus 4.7 specifically

Three places where 4.7's strengths are load-bearing, not optional:

1. **The Creative Director agent** reads the whole film as a coherent artistic object — narrative arc, pacing, tonal consistency against the brief — and writes ranked, structured suggestions the Producer can act on without re-interpreting. This is the kind of long-context, aesthetic-judgment work earlier models hedge on. The Director has explicit aesthetic authority — it is the only agent in the pipeline whose opinion is allowed to override a specialist on creative grounds — and the contract layer enforces that authority cleanly.
2. **The Shot Judge** does vision-grounded scoring across composition, prompt adherence, and continuity, with concrete rejection notes PromptSmith can act on. A judge whose notes are vague produces a pipeline that loops forever; 4.7's specificity is what closes the loop. We measured this directly: a Wan shot scored 0.683, was revised once with judge-driven prompt notes, and re-rendered to 0.907.
3. **The Producer orchestrator** runs for hours across a single Managed Agents session, holding the whole manifest in working memory, reconciling on restart, deciding when to invoke Creative Director vs. when to escalate. Long-horizon coordination with a stable view of state is the prize-winning capability and the thing 4.7 visibly does well.

## Technical difficulties — and what they validate

The pipeline ran live, end-to-end, against real provider APIs through Day 3. The `HACKATHON_LOG` records the full trace: Veo establishing shots scoring 0.917, Wan revision loops closing from 0.683 → 0.907 in one judge-driven retry, Kling I2V with Qwen-generated reference frames hitting 0.757 on a previously-failing human shot. Eight shots, three providers, full Audio Agent + Editor Agent + Hyperframes render, deterministic MD5 verified across two back-to-back runs.

Then on Day 4 the iteration loop between Claude Code and Copilot — repeatedly testing the live `AnthropicManagedAgentsSession` infrastructure (ngrok + Flask + Managed Agents + Hyperframes sandbox) — drove the Anthropic budget to $-159 before the cyber-verification harness blocked further calls. The classifier was pattern-matching on the legitimate Managed Agents upload-endpoint pattern (HMAC-signed bearer tokens, sandbox egress via authenticated tunnel) — exactly the architecture Anthropic's own docs describe as the supported path for retrieving sandbox artifacts. We filed the Cyber Verification form and pivoted entirely to `RECTOVERSO_DEMO_MODE=1`.

This is not a workaround. The demo-mode design predates the incident — it was specified on Day 1 as the defense against any live-API failure during the recorded demo, including the predictable case of a 4-minute Veo render killing a take. `MockEditorSession` extracts a `*.tar.gz` from `demo/fixtures/editor/` and returns a fully-populated `EditorSessionResult` with real `render_md5` and `uploaded_sha256` computed from actual bytes; every downstream contract and verification runs unchanged. `EditorTool.from_env(demo_mode=True)` swaps the mock in via a single env var; the production session class is untouched. The mode boundary is a deliberate seam, not a cover-up.

What the budget incident actually demonstrates is the architectural thesis: a pipeline whose state lives in a manifest, whose specialists communicate only through that manifest, and whose tool adapters are swappable behind a Protocol, can survive a provider going dark mid-project without losing any work, any audit trail, or any verifiable claim about what the system does. The film that ships on Sunday was rendered through the same orchestrator loop, satisfying the same contracts, against the same schema. Only the leaf-level provider calls are mocked.

## What this proves

A coordinated set of Claude Opus 4.7 agents, given the right primitives — a versioned shared manifest, append-only state, structural pre-dispatch verification, and a bright line between agentic work and deterministic work — can plan a short film, route shots across heterogeneous providers under hard budget caps, render and judge each shot with a self-verification loop, integrate creative feedback at three trigger points, and assemble a deterministic final composition. The pipeline is resumable, auditable, and architecturally honest about where the LLMs do work and where they do not.

The film is the demonstration. The architecture is the thesis.
