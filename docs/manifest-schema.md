# Shot Manifest — Schema Specification

Single source of truth for pipeline state. Lives at `state/manifest.json`. Every agent reads inputs from and writes outputs to this file. Validated against `schemas/manifest.schema.json` on every write.

## Design principles

1. **Append-only history.** `attempts[]` and `history[]` are never mutated. New entries only. This is what makes the pipeline resumable.
2. **Status is a state machine, not free text.** Enum only. Invalid transitions log to `run_state` and fail the write.
3. **User-authored vs. agent-authored fields are separate.** Prevents accidental clobbering.
4. **Paths are relative to repo root.** Portable across machines.
5. **Cost, latency, provider recorded per attempt.** Feeds router learning and budget enforcement.
6. **Schema version is a hard gate.** On version mismatch, halt — never silently coerce.

## Top-level structure

```json
{
  "manifest_version": "1.0",
  "project_id": "proj_2026_04_21_coastal_thriller",
  "created_at": "2026-04-21T14:02:11Z",
  "updated_at": "2026-04-21T18:44:03Z",
  "brief": { ... },
  "script": { ... },
  "shots": [ ... ],
  "audio": { ... },
  "edit": { ... },
  "budget": { ... },
  "run_state": { ... },
  "creative_decisions": [ ... ]
}
```

### `brief` (user-authored)

```json
{
  "logline": "A lighthouse keeper discovers a letter that shouldn't exist.",
  "target_duration_s": 60,
  "tone": ["moody", "minimal dialogue", "coastal noir"],
  "genre": "thriller",
  "source_path": "inputs/brief.md",
  "artistic_style": "film noir, low-key lighting, handheld camera, muted palette",
  "allow_artistic_experiments": false
}
```

- **`artistic_style`** (optional): tonal/visual anchor baked into every PromptSmith prompt and evaluated by Creative Director. If absent, `tone` + `genre` carry the anchor alone. Fixed at project start — the Producer does not rewrite it.
- **`allow_artistic_experiments`** (optional, default `false`): gates whether the Producer may reinterpret failed shots creatively (e.g., a Veo failure retried as "film noir" style via Kling) rather than deterministic fallback. Off by default so retries don't drift from brief intent.

### `script` (screenwriter-authored, user-approved)

```json
{
  "status": "approved",
  "version": 2,
  "path": "artifacts/script/v2.fountain",
  "approved_by": "user",
  "approved_at": "2026-04-21T15:10:44Z"
}
```

Status: `draft | approved`.

### `shots` (see Shot object below)

### `audio` (audio_agent-authored)

```json
{
  "dialogue": [
    {
      "shot_id": "sh_003",
      "line_id": "d_003_01",
      "text": "The letter wasn't there yesterday.",
      "voice_id": "elevenlabs_voice_xyz",
      "audio_path": "artifacts/audio/d_003_01.wav",
      "duration_s": 2.1,
      "timing": { "in_s": 0.4, "out_s": 2.5 }
    }
  ],
  "music_path": "artifacts/audio/music.wav",
  "sfx": [
    {
      "shot_id": "sh_003",
      "sfx_id": "sfx_003_wind",
      "description": "ocean wind, low rumble",
      "audio_path": "artifacts/audio/sfx_003_wind.wav"
    }
  ]
}
```

### `edit` (editor_agent-authored)

```json
{
  "fcpxml_path": "artifacts/edit/final.fcpxml",
  "fcpxml_version": "1.13",
  "render_path": "artifacts/edit/final.mp4",
  "total_duration_s": 58.4,
  "status": "approved"
}
```

Status: `pending | rendering | approved | failed`.

### `budget` (router-maintained)

```json
{
  "cap_usd": 151.00,
  "spent_usd": 52.97,
  "by_provider": {
    "vertex_veo_3_1_fast": 14.80,
    "fal_kling_2_1": 38.17,
    "elevenlabs": 0.0,
    "alibaba_wan_2_7_plus": 0.0,
    "alibaba_wan_2_7_turbo": 0.0
  },
  "alibaba_quota_remaining": 72,
  "elevenlabs_credits_remaining": 94310
}
```

- Router MUST refuse provider choices that would exceed `cap_usd`.
- Router MUST refuse quota-metered providers when their counter is exhausted:
  - Alibaba Wan: `alibaba_quota_remaining <= 0`
  - ElevenLabs: `elevenlabs_credits_remaining < estimated_credit_cost`
  USD cost for these providers is always `0.0` in `by_provider`.
- Veo has a per-provider hard cap (`$15`) enforced separately — see `router/capabilities.yaml` hard rules.
- Producer logs a warning when any quota counter drops below 20% of its starting value.

Enforcement is first-class, not advisory.

### `run_state` (producer-maintained)

```json
{
  "current_stage": "make",
  "last_event_id": 247,
  "resumable": true
}
```

- `current_stage`: `script | make | judge | audio | edit | done`.
- `last_event_id`: latest `id` in `state/events.db`. Used for reconciliation on restart.
- `resumable`: `false` during non-atomic operations. On restart, if `false`, Producer runs reconciliation before accepting new work.

## Shot object

```json
{
  "shot_id": "sh_003",
  "scene": 1,
  "order": 3,
  "description": "Wide establishing — coastal cliffs at dawn, lighthouse silhouette.",
  "duration_s": 4.2,
  "has_humans": false,
  "is_hero": true,
  "motion_level": "low",
  "continuity_refs": ["sh_002", "sh_004"],

  "prompt": {
    "authored_by": "prompt_smith",
    "primary": "A wide establishing shot of rugged coastal cliffs at dawn...",
    "negative": "no modern buildings, no people",
    "reference_image_paths": ["artifacts/refs/dawn_lighthouse.png"]
  },

  "routing": {
    "chosen_provider": "vertex_veo_3_1_fast",
    "chosen_model": "veo-3.1-fast",
    "rationale": "No humans, low motion, low cost preference",
    "decided_by": "router",
    "decided_at": "2026-04-21T16:02:11Z",
    "alternates": ["fal_kling_2_1", "fal_runway_gen4"]
  },

  "attempts": [
    {
      "attempt_id": 1,
      "provider": "vertex_veo_3_1_fast",
      "started_at": "2026-04-21T16:02:30Z",
      "completed_at": "2026-04-21T16:03:47Z",
      "cost_usd": 0.42,
      "latency_s": 77,
      "render_path": "artifacts/renders/sh_003/v1.mp4",
      "judge_score": 0.58,
      "judge_notes": "Composition good, but sky over-exposed; lighthouse clipped.",
      "outcome": "rejected",
      "rejection_reason": "auto_judge"
    }
  ],

  "final": {
    "render_path": "artifacts/renders/sh_003/v2.mp4",
    "attempt_id": 2
  },

  "status": "approved",
  "history": [
    {"ts": "2026-04-21T15:58:00Z", "event": "created", "by": "screenwriter"}
  ]
}
```

### Field notes

- **`motion_level`**: `low | medium | high`. Informs router (some providers handle high motion poorly).
- **`has_humans`**: boolean. Gates provider choice. Veo is forbidden for humans (EU restriction); Kling owns all human shots.
- **`is_hero`**: boolean. Set by Screenwriter to mark establishing/cinematic moments. 3–5 per film. Unlocks access to specialty-tier providers (Veo). Non-hero shots stay on workhorse tier.
- **`continuity_refs`**: adjacent shot IDs the Shot Judge should check continuity against. Without this the Judge has no cheap way to know what shot 4 should look continuous with.
- **`routing.alternates`**: pre-computed fallback list. If the chosen provider fails catastrophically, Producer already has next-choice without re-invoking the router.
- **`prompt.reference_image_paths`**: optional, used by providers that support image conditioning (Kling, Runway).
- **`attempts[].prompt_revision`** (optional): delta from prior attempt's prompt. Absent on the first attempt.
- **`final`**: set only when `status == approved`. Points to the winning attempt.

### `creative_decisions` (producer-authored, append-only)

Film-level creative pivots. Distinct from shot-level `budget_decision`, which captures a pivot scoped to one shot. These entries document reorders, merges, scope changes, and style shifts that affect multiple shots or the project as a whole.

```json
[
  {
    "ts": "2026-04-22T17:12:00Z",
    "decision_type": "merge",
    "trigger": "USD $120 spent with 3 shots remaining; Creative Director flagged 'two rushed beats read worse than one held moment'",
    "action": "merged sh_008 + sh_009 into sh_008 at 6.5s; removed sh_009; reordered trailing shots",
    "affected_shots": ["sh_008", "sh_009"],
    "rationale": "Longer hero shot better serves 'coastal noir' tone under budget pressure than two 3s cuts on cheaper provider.",
    "decided_by": "producer",
    "source_feedback_refs": [
      { "shot_id": "sh_008", "feedback_index": 2 }
    ]
  }
]
```

`decision_type` enum: `reorder | merge | split | scope_change | style_pivot | duration_adjust`.

`source_feedback_refs` points back to the `creative_feedback[]` entries that drove the decision — this is the audit trail from observation → suggestion → action, and it's what lets the demo video show "Creative Director said X, Producer did Y."

Append-only. Producer writes; Creative Director and other specialists do not.

## Status state machine (per shot)

```
created ──► prompted ──► routed ──► rendering ──► judging ──► approved
                                         │           │
                                         │           └─► rejected ──► rendering (retry)
                                         │                       └─► escalated (user decision)
                                         └─► failed ──► routed (fallback provider)
```

**Terminal states**: `approved`, `escalated`, `failed` (after exhausting `alternates`).

**Transition rules**:
- `rejected → rendering` requires a new `attempts[]` entry with `prompt_revision` or `provider` change.
- `failed → routed` consumes one entry from `routing.alternates`.
- `escalated` pauses pipeline; requires human decision written to `history[]` before resumption.
- Invalid transitions MUST be rejected by the validation layer, not silently accepted.

## Invariants (validator must enforce)

1. `shot_id` format: `^sh_\d{3}$`. Unique within `shots[]`.
2. `attempts[].attempt_id` starts at 1, monotonically increments per shot.
3. If `status == approved`, `final.attempt_id` MUST reference an existing attempt with `outcome == approved`.
4. `sum(shots[].duration_s)` SHOULD be within ±10% of `brief.target_duration_s`. Soft warning, not a hard fail.
5. `budget.spent_usd == sum(budget.by_provider.*)`.
6. `budget.spent_usd <= budget.cap_usd` before any new render is authorized.
7. All `*_path` fields: must be relative (no leading `/` or `~`).
8. Timestamps are ISO 8601 UTC with `Z` suffix.
9. Append-only fields (`attempts[]`, `history[]`, `run_state.last_event_id`): new writes MAY extend, MUST NOT shrink or modify prior entries.

## Relationship to SQLite event log

`state/events.db` is the append-only event log (every provider call, cost, latency, tool invocation). The JSON manifest is derivable from the event stream. If they disagree, SQLite wins — rebuild the manifest from events.

Producer's responsibility: after every agent action, write (event_id, event_type, payload) to SQLite THEN update the manifest. `run_state.last_event_id` anchors the manifest to a point in the event stream.
