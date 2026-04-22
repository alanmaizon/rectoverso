# Hackathon log — rectoverso

Rolling engineering journal for the "Built with Opus 4.7" hackathon (Apr 21–26, 2026).
Appended by Claude Code at the end of each session and by the Producer at major pipeline milestones.

Format: `[ISO-timestamp] <tag>: <one-line entry>`. Multi-line notes allowed under a timestamped header.

---

## Day 2 — Wed Apr 22

### 2026-04-22T21:30:00Z — creative-pipeline pivot landed: Creative Director + pair contracts + test spec
Day-2 research (`RESEARCH_DAY2.md`) reframed the pipeline from deterministic automation to an artistic AI team. Design shifts implemented end-to-end today:

**New agent — Creative Director (Tier 2, Managed Agent).** [prompts/creative_director.md](prompts/creative_director.md). Reads the film as a whole; writes to `shots[].creative_feedback[]` only. Three invocation triggers: mid-production coherence check (after 3/6/9 shots approved), mandatory pre-Editor full-film review, and tie-breaker for contradictory specialist feedback. Does NOT write `status`, `final`, `artistic_direction`, or `creative_decisions[]` — those stay Producer-owned. Max 3 `mid_production` invocations per project; max 6 feedback entries per invocation.

**Schema extensions** ([schemas/manifest.schema.json](schemas/manifest.schema.json)):
- `brief.artistic_style` (optional) — tonal anchor every PromptSmith prompt bakes in and CD evaluates against.
- `brief.allow_artistic_experiments` (optional, default false) — gates failure-recovery-as-style-pivot behavior.
- Top-level `creative_decisions[]` (required, append-only) — film-level reorders/merges/splits/scope_changes/style_pivots/duration_adjusts. Each entry carries `source_feedback_refs` pointing back to the `creative_feedback[]` entries that drove it — this is the audit trail the demo video will show.
- `audio.dialogue[].compressibility_s` (optional, ≥0) — Audio Agent's self-assessment of how much tighter a take could be without losing intelligibility. Load-bearing for the Editor↔Audio contract: Editor reads this instead of spawning an Audio round-trip to ask "can you compress?"

**Producer conflict-resolution rules** added to [prompts/producer.md](prompts/producer.md). Five-rule aggregation order: hard rules → priority → Creative Director authority → cheaper intervention → brief-anchor check. Four-gate re-render flow: priority → budget (95% cap ratio) → convergence → artistic-experiment. Hard caps: 2 creative-driven re-renders per shot; 4+ feedback entries on one shot → escalate.

**Agent pair contracts** — new § in [docs/agents.md](docs/agents.md). Five contracts where silent breakage produces plausible-but-wrong output:
- Audio → Editor (dialogue duration + compressibility before timing suggestions)
- Shot Judge → PromptSmith (judge_notes drive the revision; else same prompt rewrites)
- Creative Director → PromptSmith (CD's suggestion, translated to artistic_direction by Producer)
- Creative Director ↔ Shot Judge (CD filters judge_feedback to the approved attempt)
- Creative Director ↔ Editor (scope split: mechanical timing vs. narrative arc; CD wins at equal priority)

**Executable specification** — reference resolver at [tests/creative/resolver.py](tests/creative/resolver.py) encodes the Producer's rules; [tests/creative/test_loop_scenarios.py](tests/creative/test_loop_scenarios.py) exercises them across 16 scenarios. Plus 19 schema-validation tests at [tests/manifest/test_creative_fields.py](tests/manifest/test_creative_fields.py). **All 35 tests pass** (`pip install -r tests/requirements.txt && pytest tests/`). Producer's runtime must satisfy the same invariants.

**Architecture diagram** — Creative Director added to the Mermaid system overview; new § 4a "Creative feedback loop" sequence diagram in [docs/architecture.md](docs/architecture.md) shows the full round-trip from `invoke_creative_director` → feedback written → Producer gates → `artistic_direction` set → PromptSmith revision → re-render → Shot Judge.

Still open: implementing the actual Producer runtime; the `invoke_creative_director` tool adapter; Screenwriter's hook for flagging `is_hero`.

### 2026-04-22T17:30:00Z — Vertex + Veo preflight green; architecture doc landed
Fresh GCP project (`project-87d15b7f-7332-458c-a73`) authenticated under `anna.phalan@gmail.com`.

Auth path: **ADC, no service-account keys** — org policy `iam.managed.disableServiceAccountKeyCreation` blocks SA key creation on this project; the user can't override (not Org Policy Admin on a fresh org). Moot: ADC is the recommended pattern anyway. `.env.example` updated with the full `gcloud` one-time setup sequence.

Two non-obvious gotchas diagnosed and fixed:
1. **ADC quota project must be set** (`gcloud auth application-default set-quota-project <PROJECT>`) — otherwise Vertex calls route to a Google fallback project where the API is disabled → 403 `SERVICE_DISABLED`.
2. **`x-goog-user-project` header is required** on raw HTTP calls to Vertex publisher-model endpoints under ADC. The Google SDK adds it automatically; curl does not. Both the preflight script and the production Veo adapter will need it.

Verified reachable in `us-central1`: `veo-3.1-fast-generate-001` (GA), `veo-3.1-generate-001`, plus 3.0 and 2.0 variants. Seeded `VEO_MODEL_ID=veo-3.1-fast-generate-001` — GA variant for submission stability over preview. The $15 project-wide Veo cap still holds; we have $300 in GCP credits, but the cap is a scope-control decision (hero shots only), not a budget-availability one.

Deliverables:
- [scripts/verify_vertex.sh](scripts/verify_vertex.sh) — 8-check preflight (gcloud, ADC, quota project, project ID, token, API enabled, IAM role, model reachability). Costs nothing; runs in ~5s.
- [.env.example](.env.example) — Vertex section rewritten with full setup instructions, `VEO_MODEL_ID` added.
- [docs/architecture.md](docs/architecture.md) — top-level technical overview with 5 mermaid diagrams (system overview, shot lifecycle, producer sequence, router decision flow, data model ERD). For the demo video screen-grab and for reducing hallucinations during Days 3–5 coding.

### 2026-04-22T16:45:00Z — ElevenLabs budget: 117,999 credits
ElevenLabs confirmed: **117,999 credits** pre-paid. Treating like Alibaba Wan — $0 USD + quota counter. Added `budget.elevenlabs_credits_remaining` to manifest schema (required field).

Default model mapping (encoded in capabilities.yaml):
- `eleven_multilingual_v2` — finals (~1 credit/char)
- `eleven_turbo_v2_5` — iteration (~0.5 credit/char)
- `eleven_sound_effects` — SFX (~50 credits/second)

Audio Agent per-call rule: estimate credit cost BEFORE the call, refuse if remaining < estimate. Decrement by actual (API-reported) cost after. New hard rule: `elevenlabs_credits_exhausted`.

Envelope for a 60-second film, rough estimate:
- Dialogue: 5K chars × v2 ≈ 5K credits
- SFX: 15 cues × 3s × 50 = 2,250 credits
- Total per-film: ~7–10K credits. Budget accommodates ~12 full runs before exhaustion.

Final budget envelope:
- Anthropic: $500 (orchestration)
- fal.ai: $136 (Kling)
- Vertex Veo: $15 hard cap
- Alibaba Wan: $0 + free quota (50–100 gens)
- ElevenLabs: $0 + 117,999 credits
- **`cap_usd` seed: $151**

### 2026-04-22T16:10:00Z — real budget confirmed, fal.ai two-key rotation
fal.ai: 2 keys × $68 = **$136 available**. Two keys are for failover, not parallelism (alternating per-request wastes session cache). Policy: primary first, failover to secondary on `401`/`403`/`quota_exceeded`, then exclude fal providers from routing once both exhausted.

Revised budget envelope:
- Anthropic: $500 (orchestration)
- fal.ai: $136 (Kling)
- Alibaba Wan: $0 USD + free quota
- Vertex Veo: $15 hard cap
- ElevenLabs: TBC

Total video/audio USD: $151 + ElevenLabs. The $800 `cap_usd` seed was aspirational; real initial cap is ~$151. Project manifest should seed `cap_usd` conservatively once ElevenLabs budget is confirmed.

Added `.env.example` with `FAL_KEY_PRIMARY` / `FAL_KEY_SECONDARY` convention. Renderer adapter will read both; event log tracks `key_id` per call.

### 2026-04-22T15:30:00Z — Wan family clarified, Qwen/nano-banana deferred
Corrected an oversimplification in the previous entry. "Wan 2.5/2.6" is not a single model:
- Wan family spans 2.1–2.7 with tier variants (Plus/Max = quality, Turbo/Flash = speed, Preview = experimental).
- Different modes: T2V (default), I2V (use when reference image present), R2V/S2V (not in v1 loop).
- Wan 2.1-VACE-Plus is video-editing/inpainting — wrong use case; moved to deferred.

Revised capability matrix:
- `alibaba_wan_2_7_plus` — final/approved renders (non-hero, non-human).
- `alibaba_wan_2_7_turbo` — iteration + rejected-shot retries.
- Both marked `cost_per_second_usd: 0.0` + `quota_metered: true`. Budget now tracks `alibaba_quota_remaining` as a first-class field (schema updated; router refuses quota-metered providers when exhausted).
- Added router hard rules: `alibaba_quota_exhausted` and `wan_turbo_for_iteration_only`.

Qwen-image-2.0 and Google nano-banana moved to `day_4_candidates` in capabilities.yaml. In v1, reference images come from `inputs/refs/` manually.

**Pending signoff** (defaults locked but flagged for review):
- Default Wan tier split: `plus` for finals, `turbo` for iteration. Alternative would be all-plus.
- Budget math: Wan = $0 USD + quota counter. Alternative would be phantom-USD accounting (e.g., $0.05/sec) to let the router naturally trade off.

### 2026-04-22T14:00:00Z — provider strategy locked
Replaced v0 provider tiering (which treated Veo as a primary workhorse) with role-based split:
- **Workhorses**: Alibaba Wan 2.6 (non-hero, non-human), Kling 2.x via fal.ai (all humans), ElevenLabs (all audio).
- **Specialty**: Veo 3.1 Fast — hero shots only, **$15 project-wide hard cap**, **never humans** (EU restriction).

Changes made:
- `router/capabilities.yaml` — re-tiered, added `alibaba_wan_2_6` provider (dropped `fal_wan_2_5`), demoted Veo to specialty, added `spend_cap_usd: 15.0` field, added 6 explicit `hard_rules` with predicate strings the router must enforce.
- `schemas/manifest.schema.json` + `docs/manifest-schema.md` — added required `is_hero: bool` field on shots. Screenwriter sets it; router uses it to gate specialty-tier access.
- `docs/agents.md` — Screenwriter now flags 3–5 hero shots per film; hero+humans shots (can't route to Veo) get extra attention from PromptSmith on Kling grammar.
- `CLAUDE.md` — Provider priority section rewritten to match.

Open: "Wan 2.5/2.6" — treated as one provider with 2.6 preferred and 2.5 as fallback model ID. If 2.5 and 2.6 should be distinct router entries (e.g., 2.5 cheaper for iteration), revisit.

---

## Day 1 — Tue Apr 21

### 2026-04-21T23:15:00Z — scaffold
Repo scaffolded. Deliverables in place:
- `CLAUDE.md` (architecture, compliance, tiering, deadlines)
- `docs/manifest-schema.md` + `schemas/manifest.schema.json` (manifest contract)
- `docs/agents.md` (per-agent specs)
- `prompts/producer.md` (Producer system prompt v0)
- `router/capabilities.yaml` (provider matrix seeded; capability scores are TODO for Day 2 verification)
- Directory skeleton (`state/`, `artifacts/`, `inputs/`, `demo/fixtures/`, `tests/`)
- `.gitignore`

No pipeline code. Per plan, Day 1 is scaffolding only.

### Open questions to resolve Day 2
- Confirm FCPXML version for Final Cut Pro 12.2 (currently assumed 1.13; editor agent spec cites this).
- Verify Managed Agents multi-agent coordination access (research preview). If unavailable, fall back to subprocess-wrapped Messages API calls.
- Verify Outcomes feature access. If unavailable, Producer runs the iteration loop explicitly.

### Next
- Day 2 (Wed Apr 23): Tarik AMA noon EST. Build Tier 3 agents (Screenwriter, PromptSmith) as plain Messages API calls. Stub Renderer with a fake adapter that returns a fixture. Write router tests.

---

<!-- New entries go above this line, newest at top of the day; days in reverse chronological order at the file top. -->
