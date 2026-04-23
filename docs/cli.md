# `rectoverso` CLI

A read-only, dry-run inspector for the rectoverso pipeline. Every command is safe to run at any time — no tool dispatch, no live API calls, no manifest mutation, no cost.

Use it to answer: what's in the manifest right now, is it valid, will this hypothetical render fit the budget, which provider would the router pick, do the pair contracts allow this dispatch.

## The `run` subcommand (driver; non-read-only)

`run` is the only subcommand that mutates state. Everything else is read-only inspection. `run` reads a `brief.json`, drives the **Screenwriter → Router → PromptSmith** half of the pipeline, and writes `state/manifest.json` + `state/events.db`. It stops short of Tier-2 agents (Shot Judge, Editor, Audio, Creative Director) and the renderer — by design; this is the upstream half, runnable in under a second under `--dry-run`.

### Brief shape

```jsonc
{
  "logline": "A lighthouse keeper at dawn, mist clearing over empty rocks.",
  "target_duration_s": 30.0,
  "tone": ["quiet", "solitary"],
  "genre": "drama",
  "artistic_style": "naturalistic, cold color palette, handheld",  // optional
  "allow_artistic_experiments": false                                // optional
}
```

Required fields: `logline`, `target_duration_s`, `tone` (non-empty array), `genre`. `source_path` is derived from the brief's file path. Optional fields above are passed through verbatim.

### Two modes

**`--dry-run`** (no network, no cost, deterministic):
```bash
./bin/rectoverso run inputs/brief.json --dry-run
```
Uses a stub `LLMClient` that branches on system-prompt content. Produces an 8-shot list summing to `target_duration_s` (±5%), 3 hero shots, routed normally through `router/capabilities.yaml`. Token usage reports zero. Perfect for tests and offline demos.

**Live** (Anthropic Messages API, consumes tokens):
```bash
./bin/rectoverso run inputs/brief.json
```
Reads `ANTHROPIC_API_KEY` from `.env` or the shell environment. Loads the real system prompts from `prompts/screenwriter.md` and `prompts/prompt_smith.md`. Dispatches through `src.producer.dispatch` — so contract validation, event logging, and schema validation on every manifest write all still apply.

### Output

```
project_id        proj_0f25e9f2
manifest          state/manifest.json
shots             8 (target 30.0s, actual 30.0s, within ±5%)
hero shots        3
providers:
  fal_kling_2_1_pro              4
  vertex_veo_3_1_fast            2
  alibaba_wan_2_7_plus           2
usage:
  screenwriter       in=0 out=0 cache_read=0
  prompt_smith (sum) in=0 out=0 cache_read=0
```

Pass `--json` for a machine-readable summary (same fields, JSON shape).

### Flags

- `brief_path` *(positional, required)* — path to `brief.json`
- `--out PATH` — manifest output (default `state/manifest.json`)
- `--events-db PATH` — events log (default `state/events.db`)
- `--capabilities PATH` — router capabilities YAML (default `router/capabilities.yaml`)
- `--dry-run` — stub LLM client; no Anthropic calls
- `--json` — structured summary output

### Exit codes (run-specific)

| Code | Meaning |
|---|---|
| `0` | Pipeline completed; manifest fully prompted |
| `2` | Brief file not found |
| `3` | Brief is malformed JSON, missing required fields, or seeded manifest fails schema validation |
| `4` | Router `RoutingError` on some shot — no provider survived hard rules |
| `5` | Dispatch failure (`DispatchFailure` or `ContractViolation` from the Producer runtime) |

### Example end-to-end flow

```bash
# 1. Dry-run to scaffold and sanity-check the pipeline wiring offline
./bin/rectoverso run inputs/brief.json --dry-run

# 2. Inspect the scaffolded manifest
./bin/rectoverso manifest show

# 3. Inspect the event trail
./bin/rectoverso events tail --limit 50

# 4. Preflight a single shot's router decision and its downstream contracts
./bin/rectoverso router pick --shot sh_005
./bin/rectoverso contracts verify --agent renderer --shot sh_005

# 5. Live run with real LLM (will consume tokens)
./bin/rectoverso run inputs/brief.json
```

---

## Invocation

Three equivalent ways to run:

```bash
# Zero-install wrapper (recommended for local dev)
./bin/rectoverso <command>

# Direct via python (wrapper shorthand)
PYTHONPATH=src python -m rectoverso <command>

# After `pip install -e .` lands (post-hackathon; not required today)
rectoverso <command>
```

The wrapper at [bin/rectoverso](../bin/rectoverso) prepends `src/` to `PYTHONPATH` and picks the project's `.venv/bin/python3` if present. All examples below use the wrapper form; substitute the others freely.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success / allowed |
| `1` | Command ran, produced a refusal: dirty manifest (from `manifest show`), budget refused (from `budget check`) |
| `2` | File not found (manifest, events.db, capabilities.yaml), or a named shot is missing from the manifest |
| `3` | Manifest failed schema validation |
| `4` | Router `RoutingError` — no provider survived hard rules |
| `5` | Contract block — `validate_before_dispatch` raised `ContractViolation` |

Every command accepts `--json` to emit structured output instead of the terse pretty form. Use this when piping into `jq` or a downstream script.

## Command reference

### `manifest show [PATH]`

Pretty-print the current manifest state: project, stage, shot counts by status, edit block, budget progress bar.

```bash
./bin/rectoverso manifest show                       # defaults to state/manifest.json
./bin/rectoverso manifest show site/mock/manifest.json
./bin/rectoverso manifest show --json | jq .budget
```

Exits `1` when `run_state.resumable == false` on disk (interrupted write — Producer must reconcile before accepting new work).

### `manifest validate [PATH]`

Schema-validate the manifest against [schemas/manifest.schema.json](../schemas/manifest.schema.json). Loud failure with the JSON-pointer path and the specific violation when invalid.

```bash
./bin/rectoverso manifest validate
./bin/rectoverso manifest validate state/manifest.json
```

### `budget show [PATH]`

Current budget state — cap, spent, remaining, per-provider breakdown, quota counters. Warns if the sum invariant (`spent_usd == sum(by_provider.*)`) has drifted.

```bash
./bin/rectoverso budget show
./bin/rectoverso budget show --json | jq '.by_provider'
```

### `budget check --provider <id> --cost <usd> [flags] [PATH]`

Project a hypothetical render against the cap. Returns `ALLOW` or `REFUSE` with a rationale; exits `0` (allow) or `1` (refuse). This is the dry-run form of `src.producer.check_before_render`.

```bash
# Cheap Kling iteration — should allow
./bin/rectoverso budget check --provider fal_kling_2_1_pro --cost 1.50

# Veo hero render that would breach the $15 Veo sub-cap
./bin/rectoverso budget check --provider vertex_veo_3_1_fast --cost 20.0
# -> REFUSE  Veo project cap breached: projected $X > cap $15.00

# Creative-driven re-render at 92% spent (soft 95% cap)
./bin/rectoverso budget check --provider fal_kling_2_1_pro --cost 5.0 --creative

# Alibaba quota projection
./bin/rectoverso budget check --provider alibaba_wan_2_7_plus --cost 0.0 --quota 10

# ElevenLabs credits projection
./bin/rectoverso budget check --provider elevenlabs_multilingual_v2 --cost 0.0 --credits 5000
```

Flags:
- `--provider` *(required)*: provider id, e.g. `fal_kling_2_1_pro`, `vertex_veo_3_1_fast`, `alibaba_wan_2_7_plus`, `elevenlabs_multilingual_v2`.
- `--cost`: estimated USD cost. `0.0` for quota-metered providers.
- `--quota`: estimated Alibaba quota cost (only relevant for `alibaba_*` providers).
- `--credits`: estimated ElevenLabs credit cost (only relevant for `elevenlabs*` providers).
- `--creative`: mark the dispatch as creative-driven; applies the 95% soft cap from [prompts/producer.md § Re-render decision rules](../prompts/producer.md).

### `events tail [--shot sh_XXX] [--limit N] [--db PATH]`

Read from `state/events.db`. Defaults to the 30 most recent events across all agents; filter to one shot with `--shot`.

```bash
./bin/rectoverso events tail --limit 50
./bin/rectoverso events tail --shot sh_003
./bin/rectoverso events tail --json | jq '.[] | select(.kind == "contract_block")'
```

Exits `2` if the events.db file doesn't exist (common — the log is created on the first real dispatch).

### `router pick --shot <id> [PATH]`

Dry-run the router for a named shot. Reads the shot's `ShotSpec` from the manifest, builds a `BudgetState`, loads `router/capabilities.yaml`, and calls `src.router.engine.route`.

```bash
./bin/rectoverso router pick --shot sh_005
./bin/rectoverso router pick --shot sh_005 --capabilities router/capabilities.yaml
./bin/rectoverso router pick --shot sh_005 --json
```

Exits `4` (with `RoutingError` details + per-provider exclusions) when no provider survives the hard rules — for example, a hero shot with humans on a project that's already past the Veo spend cap.

### `contracts verify --agent <name> [--shot sh_XXX] [ctx flags] [PATH]`

Run `validate_before_dispatch` against the manifest, as if the Producer were about to dispatch to `--agent`. Reports `ALLOW` (with any warn-severity violations logged) or `BLOCK` (with the ContractViolation details and exit `5`).

```bash
# Film-level Editor precheck (Contract 5 film-level)
./bin/rectoverso contracts verify --agent editor_agent

# Revision PromptSmith dispatch on a shot (Contract 2)
./bin/rectoverso contracts verify --agent prompt_smith --shot sh_001 --revision

# Creative-driven re-render via renderer (Contract 3)
./bin/rectoverso contracts verify --agent renderer --shot sh_001 --creative-driven

# Shot-level editor authority check against a CD feedback priority
./bin/rectoverso contracts verify --agent editor_agent --shot sh_007 --editor-priority high
```

Agent choices: `editor_agent`, `shot_judge`, `audio_agent`, `creative_director`, `prompt_smith`, `renderer`, `screenwriter`. Context flags:
- `--revision`: PromptSmith is being invoked for a revision.
- `--creative-driven`: the dispatch is driven by Creative Director feedback.
- `--editor-priority`: when verifying an Editor action against an unaddressed CD feedback (shot-level Contract 5). One of `critical | high | medium | low`.

### `version`

Print the CLI version.

```bash
./bin/rectoverso version
# rectoverso 0.1.0
```

## Examples — common hackathon workflows

**Pre-flight a shot before letting the renderer spend money:**

```bash
./bin/rectoverso router pick --shot sh_005                                   # see the router's pick + est. cost
./bin/rectoverso budget check --provider vertex_veo_3_1_fast --cost 0.98     # confirm it fits under the cap
./bin/rectoverso contracts verify --agent renderer --shot sh_005             # confirm contracts allow
```

**Audit after a Producer run:**

```bash
./bin/rectoverso manifest show
./bin/rectoverso budget show
./bin/rectoverso events tail --limit 100
```

**Debug a stuck shot:**

```bash
./bin/rectoverso events tail --shot sh_003 --json | jq '.[] | {id: .event_id, kind, ref_event_id, shot_id}'
./bin/rectoverso contracts verify --agent prompt_smith --shot sh_003 --revision
```

## What the CLI deliberately doesn't do

No live API calls. No manifest writes. No event-log writes. No `dispatch()` invocations. The CLI is the *inspection face* of the runtime we've built — it composes `load_manifest`, `check_before_render`, `validate_before_dispatch`, `router.route`, and `EventLog` reads, nothing else.

The orchestration loop that actually runs the pipeline (Brief → Screenwriter → … → Editor → Hyperframes render) is a separate entry point — out of scope for this CLI. When that lands it will use the same atoms, plus real Tool adapters for each agent.
