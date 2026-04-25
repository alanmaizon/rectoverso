# Data contract for the Recto Verso Productions site

The site reads two JSON files. This document describes their shape in the minimum detail the front end needs. The canonical sources of truth are [../schemas/manifest.schema.json](../schemas/manifest.schema.json) and [../docs/architecture.md § 6a](../docs/architecture.md) — consult those if a field here looks incomplete.

---

## `data/manifest.json`

One film. One manifest. The pipeline writes this; the site only reads it.

### Top-level shape

```jsonc
{
  "manifest_version": "1.0",
  "project_id": "proj_earth_day_doors_2026",
  "created_at": "2026-04-22T10:00:00Z",
  "updated_at": "2026-04-26T15:30:00Z",

  "brief": { ... },          // what the human wrote
  "script": { ... },         // pointer to the shot list the Screenwriter produced
  "shots":   [ ... ],        // the shots themselves — THIS is your main data
  "audio":   { ... },        // dialogue + sfx + optional music
  "edit":    { ... },        // final assembly metadata (paths, duration)
  "budget":  { ... },        // spend + quotas
  "run_state": { ... },      // pipeline health
  "creative_decisions": [ ... ] // film-level pivots the Producer made
}
```

### `brief`

```jsonc
{
  "logline": "Magic doors appear in cities worldwide. Each leads somewhere surprising.",
  "target_duration_s": 60,
  "tone": ["magical realism", "dreamlike", "grounded"],
  "genre": "short film",
  "source_path": "inputs/brief.md",
  "artistic_style": "golden-hour photography, shallow depth of field, handheld but calm",
  "allow_artistic_experiments": false
}
```

Render: the logline is the film's tagline on the site. The tone array becomes small-caps metadata chips. `artistic_style` is subtle — mention in the colophon, not the hero.

### `script`

```jsonc
{
  "status": "approved",
  "version": 2,
  "path": "artifacts/script/v2.md",
  "approved_by": "producer",
  "approved_at": "2026-04-22T11:12:00Z"
}
```

You don't need to fetch `path` — the shots array already contains what the page shows. `version` and `approved_at` are colophon material.

### `shots[]` — the payload

```jsonc
{
  "shot_id": "sh_003",
  "scene": 1,
  "order": 3,
  "description": "Protagonist walks toward the wooden door at Shibuya crossing, rain.",
  "duration_s": 6.0,
  "has_humans": true,
  "is_hero": false,
  "motion_level": "medium",            // "low" | "medium" | "high"
  "continuity_refs": ["sh_001", "sh_002"],

  "prompt": {
    "authored_by": "prompt_smith",
    "primary": "Medium shot, protagonist in beige coat walks forward through...",
    "negative": "warped faces, distorted hands",
    "reference_image_paths": [
      "inputs/refs/protagonist/front.jpg",
      "inputs/refs/doors/wooden_door.jpg"
    ]
  },

  "routing": {
    "chosen_provider": "fal_kling_2_1",
    "chosen_model": "kling-video/v2.1/pro/image-to-video",
    "rationale": "Human subject present; Kling 2.1 Pro selected. Two reference images available — subject consistency path.",
    "decided_by": "router",
    "decided_at": "2026-04-22T11:40:12Z",
    "alternates": ["alibaba_wan_2_7_plus"]
  },

  "attempts": [
    {
      "attempt_id": 1,
      "provider": "fal_kling_2_1",
      "started_at": "2026-04-22T11:41:00Z",
      "completed_at": "2026-04-22T11:46:17Z",
      "cost_usd": 0.54,
      "latency_s": 317,
      "render_path": "artifacts/renders/sh_003_a1.mp4",
      "judge_score": 0.82,
      "judge_notes": "Face consistent with ref. Slight coat warping at 4s.",
      "outcome": "approved",
      "approved_by": "shot_judge"
    }
  ],

  "final": {
    "render_path": "artifacts/renders/sh_003_a1.mp4",
    "attempt_id": 1
  },

  "status": "approved",         // see status enum below
  "history": [ ... ],           // append-only; show last 3 in the shot drawer
  "judge_feedback": [ ... ],    // show in shot drawer
  "creative_feedback": [ ... ], // show in shot drawer, tagged by agent
  "artistic_direction": "slow, deliberate motion; keep protagonist small in frame",
  "budget_decision": null       // usually null; present if the Producer made a constrained pivot
}
```

**Status enum** (drives visual state of the shot card):

| status | meaning | visual hint |
|---|---|---|
| `created` | just emitted by Screenwriter | placeholder, faint |
| `prompted` | PromptSmith wrote the prompt | placeholder |
| `routed` | Router picked provider | placeholder |
| `rendering` | waiting on provider | spinner/shimmer |
| `judging` | render done, Shot Judge evaluating | subdued |
| `approved` | final frame — this is what makes the film | full color thumbnail |
| `rejected` | failed QC, will retry | warning stripe |
| `escalated` | exhausted retries, human needed | red stripe |
| `failed` | provider error | red stripe |

For the v1 submission all shots should end up `approved` or `escalated`. The `attempts[]` array shows the journey (how many retries, which providers).

### `audio`

```jsonc
{
  "dialogue": [
    {
      "shot_id": "sh_008",
      "line_id": "vo_01",
      "text": "Earth. Closer than you think.",
      "voice_id": "el_voice_21df",
      "audio_path": "artifacts/audio/vo_01.mp3",
      "duration_s": 3.2,
      "timing": { "in_s": 52.5, "out_s": 55.7 },
      "compressibility_s": 0.3
    }
  ],
  "music_path": null,                    // v1: no music; leave null
  "sfx": [
    { "shot_id": "sh_005", "sfx_id": "sfx_wind_glacier", "description": "wind over ice", "audio_path": "artifacts/audio/sfx_05.mp3" }
  ]
}
```

Render as a section only if dialogue or sfx is non-empty. VO lines can be shown inline under the relevant shot in the shot strip.

### `edit`

```jsonc
{
  "final_mp4_path": "artifacts/edit/final.mp4",
  "fcpxml_path": "artifacts/edit/final.fcpxml",
  "duration_s": 60.0,
  "assembled_at": "2026-04-26T15:20:00Z"
}
```

These are the paths to the **Hero** video and the **Download FCPXML** button.

### `budget`

```jsonc
{
  "cap_usd": 151.0,
  "spent_usd": 7.82,
  "by_provider": {
    "vertex_veo_3_1_fast": 3.20,
    "fal_kling_2_1": 4.62,
    "alibaba_wan_2_7_plus": 0.0
  },
  "alibaba_quota_remaining": 42,
  "elevenlabs_credits_remaining": 117423
}
```

Render as the **Production ledger** section. Monospace numerals. Sort providers by spend desc.

### `run_state`

```jsonc
{
  "resumable": true,
  "last_checkpoint_at": "2026-04-26T15:30:00Z",
  "active_agent": null
}
```

Not shown on the public page. Optional small "Pipeline state: stable" chip in the footer.

### `creative_decisions`

```jsonc
[
  {
    "ts": "2026-04-24T14:05:00Z",
    "decision_type": "reorder",
    "trigger": "Creative Director flagged pacing sag between shots 4 and 5",
    "action": "swapped sh_005 and sh_006",
    "affected_shots": ["sh_005", "sh_006"],
    "rationale": "Delaying the glacier reveal builds anticipation; aligns with 'dreamlike' tone anchor.",
    "decided_by": "producer",
    "source_feedback_refs": [{"shot_id": "sh_005", "feedback_index": 0}]
  }
]
```

Render in a small "Director's decisions" subsection inside the agent trace, or as annotations on the shot strip.

---

## `data/events.json`

Flattened export of the SQLite event log. One array of events. Each event is one LLM call or provider call.

### Event shape

```jsonc
{
  "id": 412,
  "ts": "2026-04-22T11:41:00.123Z",
  "run_id": "run_20260422_0001",
  "agent": "producer",                    // producer | screenwriter | prompt_smith | router | renderer
                                           // shot_judge | audio_agent | editor_agent | creative_director
  "provider": "anthropic",                // anthropic | fal | alibaba | vertex | elevenlabs | null
  "model": "claude-opus-4-7",             // model id (Anthropic model for LLM calls; provider model for renders)
  "event_type": "llm_call",               // llm_call | route_decision | render_submit | render_poll
                                           // tts_call | sfx_call | assembly | error
  "shot_id": "sh_003",                    // nullable
  "attempt_id": 1,                        // nullable
  "cost_usd": 0.0284,
  "latency_s": 1.8,
  "input_tokens": 4120,                   // Anthropic only; 0 otherwise
  "output_tokens": 180,                   // Anthropic only
  "cache_read_tokens": 3900,              // Anthropic prompt cache hits
  "cache_creation_tokens": 0,
  "status": "ok",                         // ok | error | retry | cache_hit
  "error": null
}
```

### How to render

- **Agent trace** section: vertical timeline grouped by agent, ordered by `ts`. Show `event_type`, one-line detail, `latency_s`, `cost_usd`.
- **Production ledger**: aggregate:
  - Total USD by agent (sum `cost_usd` grouped by `agent`).
  - Total tokens by agent (sum `input_tokens + output_tokens`, show cache_hits separately).
  - Count of events by `event_type` (calls, retries, errors).
  - Total latency (sum `latency_s` for the whole run).

### What you do NOT need to show

- Individual raw prompts or completions. The page is a report, not a transcript.
- Stack traces. If `status === "error"`, show the one-line `error` field; no more.
- Every cache hit as a separate row. Collapse consecutive cache-only turns.

---

## Mock data

During development, use:

- [mock/manifest.json](mock/manifest.json) — a hand-crafted example with 4 shots (one approved on first try, one with rejected→approved, one hero via Veo, one approved human shot via Kling).
- [mock/events.json](mock/events.json) — ~40 events across those 4 shots.
- Any placeholder video file for `media/final.mp4` and `media/shots/sh_00X.mp4`.

When the real pipeline finishes a run, `state/manifest.json` drops into `site/data/manifest.json` and `artifacts/` drops into `site/media/` — **no code changes on the site**. That's the contract.
