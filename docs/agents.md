# Agent Specifications

Per-agent contracts. Full system prompts live in `prompts/*.md` and are drafted separately.

Tiering recap (see [CLAUDE.md](../CLAUDE.md)):
- **Tier 1 (Managed Agent)**: Producer
- **Tier 2 (Managed Agents)**: Shot Judge, Audio Agent, Editor Agent, Creative Director
- **Tier 3 (plain Messages API)**: Screenwriter, PromptSmith
- **Tier 4 (no LLM)**: Renderer

Coordination: no agent-to-agent direct calls. All handoffs through `state/manifest.json`. The specific manifest fields each pair of agents shares are specified in [§ Agent pair contracts](#agent-pair-contracts) below.

---

## Tier 1 — Producer

| Field | Value |
|---|---|
| Model | `claude-opus-4-7` |
| Tier | Managed Agent |
| Session lifetime | Full project run (hours) |
| Tools | `bash`, file ops, `invoke_shot_judge`, `invoke_audio_agent`, `invoke_editor_agent` (custom) |
| Skills | `shot-manifest-schema`, `fcpxml-conventions` |
| Environment | Container with ffmpeg, ffprobe, python 3.12 |
| Outcomes | Manifest contains N approved shots; each has `final.render_path`, matching audio, and an approved edit; FCPXML validates. |
| System prompt | [prompts/producer.md](../prompts/producer.md) |

**Responsibilities**:
- Owns `state/manifest.json`. Only the Producer and tier-2 specialists write to it.
- Schedules work through status transitions.
- Runs cross-shot QC (continuity across shot neighbors, total runtime within ±10% of target).
- Resolves Shot Judge escalations.
- On session resume, runs reconciliation if `run_state.resumable == false`.

**Invariants the Producer enforces**:
- Validate manifest against `schemas/manifest.schema.json` before every write.
- Write to `state/events.db` BEFORE updating manifest (events are truth).
- Never start a render if `budget.spent_usd + estimated_cost_usd > budget.cap_usd`.
- On invalid state transition, halt and surface the error — never silently coerce.

---

## Tier 2 — Shot Judge

| Field | Value |
|---|---|
| Model | `claude-opus-4-7` (vision) |
| Tier | Managed Agent |
| Invocation | Per-shot, synchronous from Producer |
| Tools | File read (renders, reference images), manifest read/write (shot subtree only) |
| Environment | Shared container with Producer |
| Outcomes | `judge_score ∈ [0,1] AND judge_notes written AND outcome ∈ {approved, rejected}` |
| System prompt | [prompts/shot_judge.md](../prompts/shot_judge.md) |

**Scoring rubric**:
1. **Composition** (0–1): framing, focal point, rule-of-thirds adherence.
2. **Prompt adherence** (0–1): does it match `shot.prompt.primary`?
3. **Continuity** (0–1): consistent with shots in `shot.continuity_refs` (lighting, character, setting).
4. **Artifact check**: hard flag on obvious generation failures (extra limbs, morph, broken text).

**Decision logic**:
- `judge_score = mean(composition, prompt_adherence, continuity) - artifact_penalty`
- `approved` if `judge_score ≥ 0.75` and no artifact flags.
- `rejected` (send back to Renderer with revised prompt) if `0.4 ≤ judge_score < 0.75`.
- `escalated` (human decision) if `judge_score < 0.4` or `attempts.length ≥ 3`.

**Writes to manifest**: appends to `shots[i].attempts[-1]` with `judge_score`, `judge_notes`, `outcome`, `rejection_reason`. Appends to `shots[i].history[]`.

**Fallback path**: if vision scoring is unreliable in practice, score on prompt adherence via text-only (Renderer writes a VLM-generated caption of the clip; Judge compares against prompt). Document which mode is active in `judge_notes`.

---

## Tier 2 — Audio Agent

| Field | Value |
|---|---|
| Model | `claude-opus-4-7` |
| Tier | Managed Agent |
| Invocation | Async-parallel with rendering (doesn't depend on video) |
| Tools | `bash` (ElevenLabs API), file ops, `ffprobe` |
| Environment | Container with ffmpeg, ElevenLabs SDK, API key via secret |
| Outcomes | Every shot with dialogue has `audio_path`; `duration_s ≤ shot.duration_s × 0.95`; timing recorded. |
| System prompt | [prompts/audio_agent.md](../prompts/audio_agent.md) |

**Per-shot loop**:
1. Read `shots[i]` and associated script dialogue lines.
2. **Estimate credit cost** before every ElevenLabs call:
   - `eleven_multilingual_v2`: ~1 credit/char. Use for approved/final dialogue.
   - `eleven_turbo_v2_5`: ~0.5 credit/char. Use for iteration loops.
   - SFX: ~50 credits/second of generated audio.
   Refuse the call if `budget.elevenlabs_credits_remaining < estimated_cost`.
3. Generate VO, probe duration with `ffprobe`.
4. If `vo_duration_s > shot.duration_s × 0.95`: regenerate with faster pacing (stability/similarity params, or SSML rate hints). Max 3 iterations. Use turbo model for iteration attempts; upgrade to v2 for the approved take.
5. Generate SFX cues where the script calls for them.
6. Decrement `budget.elevenlabs_credits_remaining` by actual (not estimated) credit cost reported by the API response.
7. Write `dialogue[]`, `sfx[]`, and timing metadata to manifest.

**Music**: music_path is single-track for v1. Generated once per project, not per shot. Can be stubbed with a license-free bed.

---

## Tier 2 — Editor Agent

| Field | Value |
|---|---|
| Model | `claude-opus-4-7` |
| Tier | Managed Agent |
| Invocation | Once, after all shots `approved` and audio complete |
| Tools | `bash` (via `agent_toolset_20260401`), file ops, the `HyperframesTool` subprocess adapter in `src/producer/hyperframes.py` |
| Skills | `hyperframes`, `hyperframes-cli`, `gsap` (HeyGen-maintained skills bundled with `npx skills add heygen-com/hyperframes`) |
| Environment | Managed Agents cloud sandbox (Ubuntu-class, Node 22+ pre-installed, `packages.apt: ["ffmpeg"]`). Hyperframes auto-downloads Chrome at first render. |
| Outcomes | `npx hyperframes lint --json` reports `{"ok": true, "errorCount": 0}` AND `npx hyperframes render --output out.mp4` produces a non-zero-byte MP4 |
| System prompt | [prompts/editor_agent.md](../prompts/editor_agent.md) |

**Output spec (default renderer: `hyperframes`)**:
- HTML composition (`artifacts/edit/index.html`) with deterministic track layout: picture spine on track 0, dialogue on 1, music on 2 (−8dB ducked under dialogue via GSAP), SFX on 3.
- `class="clip"` on every visible timed element (framework requirement).
- Only deterministic logic — no `Date.now()`, no `Math.random()`, no network fetches.
- Transitions: hard cut by default; GSAP dissolves only where brief calls for them.
- Timecode: 30fps default (Hyperframes). 24fps via `data-fps` on the root if brief demands cinematic feel.

**Self-verification loop**:
1. Author/edit `index.html` (invoke `hyperframes` + `gsap` skills before writing — they encode framework patterns not in generic web docs).
2. `npx hyperframes lint --json` → parse `findings[]`; fix all errors; repeat until `errorCount == 0`.
3. `npx hyperframes render --output out.mp4` → verify exit code 0 AND non-zero output size.
4. If render fails: inspect stderr, revise, regenerate. **Max 3 render iterations**.
5. If still failing: fall back to FCPXML (below).

**Fallback path** (only when Hyperframes retries exhaust):
Set `edit.renderer = "fcpxml"`, emit FCPXML 1.13 + `ffmpeg concat` MP4. Schema keeps both renderers valid; `composition_path` points at either `index.html` or the `.fcpxml` file. Document the fallback in `history`. Shipping an MP4 always beats shipping nothing.

---

## Tier 2 — Creative Director

| Field | Value |
|---|---|
| Model | `claude-opus-4-7` (vision) |
| Tier | Managed Agent |
| Invocation | Three trigger points (see below), synchronous from Producer |
| Tools | Manifest read (all), manifest write (`shots[].creative_feedback[]` only) |
| Environment | Shared container with Producer |
| Outcomes | 1–6 `creative_feedback[]` entries written, ranked by priority, each with a concrete `suggestion` the Producer can act on without re-interpretation |
| System prompt | [prompts/creative_director.md](../prompts/creative_director.md) |

**Invocation triggers** (Producer is the only caller):
1. **Mid-production coherence check** — after 3, 6, and 9 shots reach `approved`. Catches pacing/tonal drift while changes are still cheap.
2. **Pre-Editor full-film review** — after all shots reach `approved`, before invoking Editor Agent. Mandatory; highest-value invocation.
3. **Tie-breaker** — when Producer has two specialists with contradictory `suggestion` fields on the same shot at the same priority, and the authority/cost rules don't resolve it.

**Scope boundaries** (what Creative Director is NOT):
- Does not write to `status`, `final`, `attempts[]`, `history[]`, `budget`, `run_state`, `shots[].artistic_direction`, or `creative_decisions[]`. Those are Producer-owned; CD's write surface is exactly `shots[].creative_feedback[]`.
- Does not invoke other agents.
- Does not second-guess Shot Judge on individual-shot technical quality; its job is whether the shot fits the film.
- Does not propose changes to `brief`.

**Reading discipline**:
- Reads `brief` (especially `artistic_style`, `tone`, `genre`) as the anchor against which every suggestion is evaluated.
- Reads `shots[]`: `description`, `duration_s`, `motion_level`, `status`, `judge_feedback[]`, existing `creative_feedback[]`, `final.render_path`.
- Reads `audio.dialogue[]` for pacing perception.
- Reads `budget` so suggestions are realistic under current constraints.
- Does not watch every clip — descriptions, durations, and Judge notes are sufficient for most invocations. Opens hero-shot renders only when pacing/tone analysis requires visual confirmation.

**Hard caps** (Producer enforces):
- Max 3 `"mid_production"` invocations per project (the `"pre_editor"` invocation is separate and mandatory).
- Max 6 feedback entries written per invocation; at most 1 `critical` priority per invocation in typical cases.

---

## Tier 3 — Screenwriter (plain Messages API)

| Field | Value |
|---|---|
| Model | `claude-opus-4-7` |
| Tier | Single-turn API call |
| Caller | Producer |
| Input | `brief.logline`, `brief.target_duration_s`, `brief.tone`, `brief.genre`, constraints from `router/capabilities.yaml` |
| Output | JSON shot list — for each shot: `scene`, `order`, `description`, `duration_s`, `has_humans`, `is_hero`, `motion_level`, `continuity_refs`, dialogue lines (if any) |
| Prompt caching | Yes — brief + capabilities stay stable across iterations |

**Rules**:
- Total of `shot.duration_s` across shots MUST be within ±5% of `target_duration_s`.
- Shot count target: 8–15 for a 30–60s film.
- Motion level distribution: bias toward `low`/`medium` — `high` shots cost more and fail more often.
- Prefer `has_humans: false` shots when the brief allows it (v1 provider reliability skews that way).
- **Hero shot flagging**: mark 3–5 shots as `is_hero: true` — these are the establishing/cinematic moments where visual quality matters most. These unlock specialty-tier providers (Veo). All other shots stay on workhorse tier (Wan/Kling).
- Hero shots with `has_humans: true` cannot route to Veo (hard rule) — these become "hero-for-Kling" and should be flagged such that PromptSmith gives them extra attention on the Kling prompt grammar.

Producer parses response, writes to `shots[]`, sets each shot's initial `status = created`.

---

## Tier 3 — PromptSmith (plain Messages API)

| Field | Value |
|---|---|
| Model | `claude-opus-4-7` |
| Tier | Single-turn API call |
| Caller | Producer |
| Input | One `ShotSpec` + chosen `provider`/`model` + provider prompt grammar reference |
| Output | `prompt.primary`, `prompt.negative`, optional `reference_image_paths` hints |
| Prompt caching | Yes — provider grammar references stay stable |

**Per-provider grammar notes** (encode in system prompt):
- **Veo 3.1**: natural-language descriptive, camera language ("wide shot, slow dolly"), avoid negative prompts (unsupported), bake negatives into descriptions.
- **Kling (fal.ai)**: supports negative prompt, responds well to style tags and reference images for subject consistency.
- **Runway Gen-4**: benefits from explicit style/era tags, shorter prompts.

---

## Tier 4 — Renderer (no LLM)

Plain Python worker. Not an agent.

**Interface**:
```python
def render_shot(shot_id: str, provider: str, model: str, prompt: dict) -> AttemptResult:
    # 1. Submit job to provider API
    # 2. Poll until complete (with exponential backoff)
    # 3. Download to artifacts/renders/{shot_id}/v{attempt_id}.mp4
    # 4. Return AttemptResult(cost_usd, latency_s, render_path, error?)
```

**Responsibilities**:
- Provider adapters (one module per provider) implementing a common `ProviderAdapter` ABC.
- Retry on transient failures (429, 5xx) with backoff. Max 3 retries before raising.
- Log every API call to `state/events.db`: `(event_id, provider, shot_id, attempt_id, cost_usd, latency_s, outcome)`.
- In `DEMO_MODE=1`, read from `demo/fixtures/{shot_id}/v{attempt_id}.mp4` instead of calling APIs.

**fal.ai key rotation** (applies to the fal adapter specifically):
- Two keys available: `FAL_KEY_PRIMARY` ($68 credit), `FAL_KEY_SECONDARY` ($68 credit).
- Default policy: try primary; on `401`/`403`/`quota_exceeded` error, failover to secondary and mark primary exhausted in-process. Do not alternate per-request (wastes cache-friendly session state).
- Once both keys return quota errors, raise — router should exclude fal providers from subsequent routing decisions.
- Track per-key spend in `state/events.db` (`key_id: "primary" | "secondary"`) so post-run reports show actual burn per key.

No LLM use. No creative decisions. Pure I/O and retry logic.

---

## Invocation patterns (Producer → specialists)

Hybrid model (see CLAUDE.md architecture):
- **Synchronous tool calls** (Producer waits): Shot Judge, Editor Agent, Creative Director.
- **Async-parallel** (runs while rendering): Audio Agent.

Concretely: Producer has four custom tools wired to Managed Agents sessions:
```
invoke_shot_judge(shot_id: str) -> JudgeVerdict               # synchronous
invoke_audio_agent(shot_ids: list[str]) -> None               # fires-and-polls manifest
invoke_editor_agent() -> EditorResult                         # synchronous, end of pipeline
invoke_creative_director(trigger: str) -> CreativeReview      # synchronous, three trigger points
```

`trigger ∈ {"mid_production", "pre_editor", "tie_breaker"}`. `CreativeReview` returns the count of new `creative_feedback[]` entries written; Producer reads them from the manifest to decide and act.

Each tool implementation: creates (or resumes) the child agent session, streams events to Producer's log, returns a summary. Producer's event stream contains `agent.tool_use` entries for every delegation — the audit trail for the demo video.

**Fallback if multi-agent coordination (research preview) is unavailable**: invoke children via `subprocess.run()` of a CLI that wraps the child's system prompt with the Messages API. Same outcome, less ceremony, still demo-able.

---

## Agent pair contracts

Agents never call each other. A "contract" here is the specific slice of `state/manifest.json` that one agent writes and another reads, plus the ordering invariant Producer must honor. Every contract has a *silent breakage mode* — the scenario where skipping the invariant produces plausible-looking but wrong output. Enforce in Producer; assume nothing downstream.

| Pair | Write side | Read side | Manifest surface | Ordering invariant | Silent breakage |
|---|---|---|---|---|---|
| **Audio → Editor** | Audio Agent writes `audio.dialogue[i].duration_s`, `.timing`, `.compressibility_s` | Editor Agent reads same | `audio.dialogue[]`, `shots[i].duration_s` | Audio must complete for shot `i` before Editor proposes timing changes affecting shot `i` | Editor proposes "shorten 0.3s" on a shot whose dialogue is 2.8s with `compressibility_s=0`. Audio can't deliver; shot loops. |
| **Shot Judge → PromptSmith** | Judge writes `attempts[-1].judge_notes`, `rejection_reason` | PromptSmith reads on revision | `shots[i].attempts[-1]`, `shots[i].artistic_direction` | PromptSmith MUST read `attempts[-1]` before writing a revision; never write a revision without a preceding rejected attempt | PromptSmith writes the same prompt on attempt 2; Judge rejects for the same reason; shot exhausts attempts. |
| **Creative Director → PromptSmith** | CD writes `creative_feedback[].suggestion`; Producer translates into `shots[i].artistic_direction` | PromptSmith reads `artistic_direction` as binding context | `shots[i].creative_feedback[]` (CD-authored), `shots[i].artistic_direction` (Producer-authored) | Producer MUST translate CD's textual suggestion into `artistic_direction` before re-dispatching to PromptSmith. CD never writes `artistic_direction` directly. | CD's guidance never lands in the render; the re-render looks identical to the one that triggered CD's complaint. |
| **Creative Director ↔ Shot Judge** | Judge writes `shots[i].judge_feedback[]`; CD writes `shots[i].creative_feedback[]` | CD reads Judge's feedback; Judge does not read CD's | `shots[i].judge_feedback[]`, `shots[i].creative_feedback[]` | CD filters `judge_feedback[]` to entries tied to the approved attempt; ignores feedback from rejected takes | CD flags "tonal drift" based on stale notes from a rejected render, producing suggestions that no longer apply. |
| **Creative Director ↔ Editor** | Both write `shots[i].creative_feedback[]` | Producer reads both; each agent may read the other's entries for context but does not respond to them | `shots[i].creative_feedback[]` | Scope separation: Editor scoped to mechanical timing (cut lengths, transitions, total runtime); CD scoped to narrative arc and tone. When they contradict at the same priority, authority rule breaks the tie: CD wins. | Ping-pong: Editor extends, CD re-flags as "too slow," Editor shortens, CD re-flags as "too fast." The convergence gate in Producer (same feedback after addressed → escalate) bounds this. |

**Pairs deliberately NOT formalized** — either no interaction or a one-shot relationship that needs no invariant:

- *Screenwriter → downstream agents*: Screenwriter runs once at the top. Downstream agents read `shots[i].description` but there is no feedback loop back to Screenwriter. No invariant beyond "Screenwriter completes before anyone else starts."
- *Shot Judge ↔ Audio Agent*: no shared fields, no interaction.
- *Renderer ↔ anyone*: Renderer is a worker invoked with explicit arguments (`shot_id`, `provider`, `model`, `prompt`). Not a manifest-mediated relationship.

**Producer's obligation**: every pair contract above translates to one or more validation checks before tool dispatch. E.g., before invoking Editor for timing analysis on shot `i`, Producer verifies `audio.dialogue[]` has at least one entry with `shot_id == i` (or confirms shot `i` is silent). Producer should fail loud when a contract is about to break rather than dispatch and hope.
