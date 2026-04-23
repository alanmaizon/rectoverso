# Agent pair contracts — enforcement spec

This document specifies the **Producer-side preconditions** for each agent pair contract listed in [docs/agents.md § Agent pair contracts](agents.md#agent-pair-contracts). Every contract here is enforced by `src/contracts/` before the Producer dispatches a tool call. Failing a contract halts the dispatch with a loud error — the Producer never dispatches and hopes.

The prose version of the contracts (motivation, silent-breakage scenarios) lives in `docs/agents.md`. This document is the *executable* layer: predicates, violations, tests.

---

## Design rules

1. **Predicates are pure.** Every contract is a function `check(manifest, ctx) -> list[Violation]`. No I/O, no network, no mutation.
2. **Violations are structured.** A `Violation` carries: `contract` name, `severity` (`block` / `warn`), `shot_id` (if applicable), `reason` (human string), `detail` (dict for logging). `block` halts dispatch; `warn` writes to `history[]` but continues.
3. **Fail fast, fail loud.** A blocking violation raises `ContractViolation`. The Producer's event log captures it; the dispatch does not occur.
4. **Contracts are idempotent checks, not mutations.** If sanitization is required (e.g., filtering stale judge feedback into the CD context), the contract exposes a pure helper that returns a filtered view. The caller applies it explicitly.
5. **No schema changes.** Every precondition expresses against the current `manifest.schema.json`. If a contract feels like it needs a new schema field, revisit the design before adding one.
6. **Event-log is the Producer's job, not the contract's.** Contracts are event-free by design — a fresh harness must be able to re-run `validate_before_dispatch` against a recovered manifest and reach the same verdict (matches [scaling_managed_agents.md § Session Is Not Claude's Context Window](../scaling_managed_agents.md)). The Producer wraps each call: write event → validate → (on block) write failure event + halt; (on warn) append to `history[]`; (on clean) proceed and write dispatch event.

---

## Design decision — block vs. warn

There is a real tension between two pulls on this design:

- [artistic_pipeline.md](../artistic_pipeline.md) pushes toward **adaptive**: constraint becomes a creative decision; Producer reinterprets rather than halts.
- [prompts/producer.md § Style](../prompts/producer.md) pushes toward **fail loud**: "silent drift is the enemy."

Resolution encoded here: **block only when the silent-breakage scenario produces wrong output without any observable error.** That is, block when downstream agents would succeed superficially but ship a bad result. Use `warn` (log to `history[]`, continue) when the caller has the information needed to recover — typically a mis-set context flag or a sanitizable input.

Applied:
- Contracts 1, 2, 3, 5 (film-level), 5 (shot-level higher-priority CD) → `block`. These are the plausible-but-wrong scenarios described in [docs/agents.md § Agent pair contracts](agents.md#agent-pair-contracts).
- Contracts 4, 5 (shot-level equal-priority CD), 2 (timeout reason), 3 (creative_driven dispatch with no unaddressed CD feedback) → `warn`. These are recoverable by the caller without halting.

Block aligns with the [scaling_managed_agents.md](../scaling_managed_agents.md) cattle-not-pets discipline: a blocking contract produces a deterministic, recoverable error state (Producer retries with corrected inputs or escalates); a silent success on bad inputs produces a pet — a hand-tended failure that's hard to diagnose because nothing observable went wrong.

---

## Contract registry

| # | Name | Trigger (dispatch point) | Invariant (short) |
|---|---|---|---|
| 1 | `audio_to_editor` | Before Editor receives a timing suggestion on shot `i` | Shot `i` has at least one `audio.dialogue[]` entry with `duration_s` + `timing` + `compressibility_s`, OR shot `i` is marked silent. |
| 2 | `shot_judge_to_prompt_smith` | Before PromptSmith is invoked to **revise** shot `i` | `shots[i].attempts[-1].outcome == "rejected"` AND `attempts[-1].judge_notes` non-empty. |
| 3 | `cd_to_prompt_smith` | Before a **creative-driven** re-render dispatch for shot `i` | Since the most recent unaddressed `creative_director` feedback on shot `i`, there exists a `history[]` entry with `event == "artistic_direction_updated"` AND `shots[i].artistic_direction` non-empty. |
| 4 | `cd_reads_approved_judge_feedback` | Before CD is invoked (any trigger) | Sanitization: CD's context is filtered to only `judge_feedback[]` entries whose `ts` falls within the time window of the shot's **approved** attempt. Pure helper, no raise — emits `warn` if stale entries would have been visible. |
| 5 | `cd_editor_authority` | Before Editor is invoked, and before the Producer acts on Editor feedback | No unaddressed `creative_director` `creative_feedback[]` entry at priority ≥ `high` exists for any shot in the film. CD wins at equal priority; if CD hasn't weighed in, invoke CD first. |

Each contract is implemented as one module in `src/contracts/`, named the same as the registry row.

---

## Contract 1 — `audio_to_editor`

**Purpose.** Editor must never propose `extend sh_i by +0.3s` or `shorten sh_i by −0.3s` without knowing the audio timing and compressibility on that shot. Without this precondition, Editor can propose a duration change that audio cannot actually deliver (dialogue is already at floor pace), producing a silent loop.

**Research anchor.** Implements the "agents collaborate through the manifest, not via chat" pattern from [artistic_pipeline.md § 3 — Cross-shot Creative Loops](../artistic_pipeline.md) example steps 2–3. Instead of Editor asking Audio "can you compress sh_003 by 0.5s?" and waiting for a reply, Audio writes `compressibility_s` proactively when it produces a take, and Editor reads it. No round-trip; bounded latency.

**Dispatch point.** Producer calls `invoke_editor_agent()`. Additionally, any Producer action that applies an Editor-authored `duration_adjust` on shot `i` checks this contract.

**Precondition (predicate).**
```
for each shot i where the Producer is about to apply or solicit a timing suggestion:
    dialogue_for_i = [d for d in manifest.audio.dialogue if d.shot_id == i]
    if dialogue_for_i is empty:
        shot i must be marked silent (ctx flag or explicit history entry "shot_silent")
    else:
        every d in dialogue_for_i has: duration_s > 0, timing.in_s/out_s present, compressibility_s present (may be 0.0)
```

**Violation.** `contract: audio_to_editor, severity: block, shot_id: sh_XXX, reason: "dialogue entries present but compressibility_s missing on line X"`.

**Silent breakage case.** Editor proposes "shorten sh_005 by 0.3s" on a shot whose dialogue is 2.8s with `compressibility_s == 0.0`. Audio cannot compress. Re-render of the dialogue take cycles; never converges.

**Tests.** `tests/contracts/test_audio_to_editor.py` covers:
- Happy path: dialogue entry complete, no violation.
- Missing `compressibility_s`: violation raised.
- Zero dialogue entries, shot flagged silent: no violation.
- Zero dialogue entries, shot not flagged silent: violation raised.

---

## Contract 2 — `shot_judge_to_prompt_smith`

**Purpose.** PromptSmith revisions must be grounded in Shot Judge's concrete rejection notes. Without this, PromptSmith rewrites prose but produces a prompt functionally identical to the one Judge just rejected — next attempt fails the same way.

**Dispatch point.** Producer calls `call_prompt_smith(shot_spec, provider, revision=True)`.

**Precondition (predicate).**
```
last_attempt = manifest.shots[i].attempts[-1]
require last_attempt.outcome == "rejected"
require last_attempt.rejection_reason ∈ {"auto_judge", "continuity", "artifact"}
require last_attempt.judge_notes is non-empty (len > 0, not whitespace)
```

**Violation.** `contract: shot_judge_to_prompt_smith, severity: block, shot_id: sh_XXX, reason: "revision requested but attempts[-1].judge_notes is empty"`.

**Silent breakage case.** Attempt 1 is rejected with empty `judge_notes`. Producer calls PromptSmith for a revision. PromptSmith has no signal, returns a paraphrase of the original prompt. Attempt 2 fails identically. Shot hits the attempt cap and escalates — the escalation is unnecessary because Judge never wrote notes.

**Tests.** `tests/contracts/test_shot_judge_to_prompt_smith.py` covers:
- Revision requested with populated judge_notes: no violation.
- Revision requested, attempt rejected, judge_notes empty: violation raised.
- Revision requested on a shot whose last attempt is `approved`: violation raised (shouldn't revise an approved take).
- Initial prompt call (not a revision, `revision=False`): contract skipped.

---

## Contract 3 — `cd_to_prompt_smith`

**Purpose.** Creative Director writes `creative_feedback[].suggestion` in natural language. PromptSmith does not read `creative_feedback[]` directly — it reads `shots[i].artistic_direction`, which is Producer-authored. The Producer must translate CD's suggestion into a specific `artistic_direction` string before re-dispatching. Otherwise CD's guidance never lands in the render and the re-render looks identical to the one that triggered the complaint.

**Research anchor.** Encodes the loop from [artistic_pipeline.md § 3 — Cross-shot Creative Loops](../artistic_pipeline.md) steps 4–6: "Producer reads decision → asks PromptSmith: 're-author sh_003 prompt with slower pacing'. PromptSmith updates prompt with notes like 'slow, deliberate motion'." This contract makes the "Producer translates" step enforceable; without it the loop visibly runs but silently produces the same output. Also addresses [artistic_pipeline.md § Q4](../artistic_pipeline.md) ("Artistic direction must be baked into every prompt") — binds it to a measurable predicate rather than a prompt-engineering hope.

**Dispatch point.** Producer issues a re-render with `prompt_revision` starting with the prefix `"creative:"` (matches the creative resolver's convention in `tests/creative/resolver.py:164`).

**Precondition (predicate).**
```
unaddressed_cd = [f for f in shots[i].creative_feedback
                  if f.from_agent == "creative_director" and not f.addressed]
if unaddressed_cd is empty:
    contract passes (this dispatch isn't creative-driven after all; warn)
else:
    latest_cd_ts = max(f.ts for f in unaddressed_cd)
    require exists h in shots[i].history where
        h.event == "artistic_direction_updated" AND h.ts >= latest_cd_ts
    require shots[i].artistic_direction is non-empty
```

**Violation.** `contract: cd_to_prompt_smith, severity: block, shot_id: sh_XXX, reason: "creative-driven re-render requested but artistic_direction was not updated since CD feedback at <ts>"`.

**Silent breakage case.** CD writes `suggestion: "re-render sh_007 with slower, handheld camera — current take breaks the quiet tone"`. Producer dispatches a re-render without updating `artistic_direction`. PromptSmith regenerates from the original brief + shot description; the new prompt is substantively identical to the previous one; re-render fails in the same way.

**Tests.** `tests/contracts/test_cd_to_prompt_smith.py` covers:
- `artistic_direction` updated after CD feedback, history entry present: no violation.
- Re-render dispatched, no CD feedback on shot: passes with `warn` (caller should not have labeled this creative).
- Re-render dispatched, CD feedback present, no history entry for update: violation.
- Re-render dispatched, history entry exists but predates CD feedback: violation.
- `artistic_direction` set to empty string: violation.

---

## Contract 4 — `cd_reads_approved_judge_feedback`

**Purpose.** When CD is invoked, it must not reason on judge feedback from rejected attempts. Otherwise CD flags "tonal drift" based on stale notes that no longer apply to the current approved take. This is a sanitization rule, not a dispatch block.

**Dispatch point.** Before Producer calls `invoke_creative_director(trigger)`, for each shot in `approved` status with a non-empty `judge_feedback[]`.

**Precondition & helper.**
```
for each shot i with status == "approved":
    approved_attempt = shots[i].attempts[j] where attempts[j].attempt_id == shots[i].final.attempt_id
    window_start = approved_attempt.started_at
    window_end = approved_attempt.completed_at (or now if absent)
    fresh_judge_feedback(shot i) = [f for f in shots[i].judge_feedback
                                     if window_start <= f.ts <= window_end]
    stale_count = len(shots[i].judge_feedback) - len(fresh_judge_feedback(shot i))
    if stale_count > 0: emit warn (not block)
```

**Violation.** `contract: cd_reads_approved_judge_feedback, severity: warn, shot_id: sh_XXX, reason: "N stale judge_feedback entries filtered from CD context"`. Also returns the filtered list via helper `filter_judge_feedback_for_cd(shot)` — the caller injects it into CD's context.

**Silent breakage case.** Shot 3 had 3 attempts: attempt 1 rejected (Judge wrote "too cool, too slow"), attempt 2 rejected, attempt 3 approved. CD is invoked mid-production and sees the stale "too cool, too slow" feedback still sitting in `judge_feedback[]`. CD echoes "tonal drift on sh_003" — but attempt 3 actually resolved that. CD produces bad suggestion.

**Tests.** `tests/contracts/test_cd_reads_approved_judge_feedback.py` covers:
- All judge_feedback timestamps within approved attempt window: no stale, no warn.
- Some judge_feedback predates approved attempt: filtered out, warn emitted, helper returns only fresh.
- Shot not yet approved: contract skipped.
- Approved attempt missing `completed_at`: falls back to filtering by `started_at` only.

---

## Contract 5 — `cd_editor_authority`

**Purpose.** CD and Editor both write to `shots[].creative_feedback[]`. They have different scopes (narrative arc vs. mechanical timing) but at the same priority level they can contradict. CD has authority — `AUTHORITY_ORDER["creative_director"] = 2` in `tests/creative/resolver.py:18-23`. This contract keeps the Producer from dispatching Editor while CD has unresolved same-or-higher-priority objections pending film-wide.

**Research anchor.** Answers [artistic_pipeline.md § Q2](../artistic_pipeline.md) ("How does Producer weigh conflicting feedback? Editor says extend sh_003 vs Audio says compress it — who has final say?"). The resolver answer (priority → authority → cheaper intervention → brief anchor) lives in `tests/creative/resolver.py` and `prompts/producer.md § Aggregating feedback`. This contract pre-empts the conflict: by forcing CD's pre-Editor review to be resolved first, the Editor-CD ping-pong described in the research doc never occurs.

**Dispatch point.** Before Producer calls `invoke_editor_agent()` at the end of the pipeline. Also before Producer acts on Editor-authored feedback that has a matching-priority CD feedback on the same shot.

**Precondition (predicate).**
```
# Film-level check (before Editor invocation)
unaddressed_cd_high = [f for s in manifest.shots
                         for f in s.creative_feedback
                         if f.from_agent == "creative_director"
                         and not f.addressed
                         and f.priority in ("critical", "high")]
require unaddressed_cd_high is empty
    # Producer must resolve CD first, either by invoking_creative_director again
    # with trigger="pre_editor", or by acting on each entry.

# Shot-level check (per shot, before applying Editor feedback)
for shot i with Editor feedback f_editor at priority p:
    conflicting_cd = [f for f in shots[i].creative_feedback
                       if f.from_agent == "creative_director"
                       and not f.addressed
                       and f.priority == p]
    if conflicting_cd: CD's suggestion wins; Editor's is deferred or dismissed.
```

**Violation.**
- Film-level: `contract: cd_editor_authority, severity: block, reason: "Editor invocation blocked — N unaddressed CD feedback entries at priority >= high pending"`.
- Shot-level: `contract: cd_editor_authority, severity: warn, shot_id: sh_XXX, reason: "Editor feedback deferred; same-priority CD feedback takes precedence"`.

**Silent breakage case.** Editor extends sh_008 by 0.4s to balance audio spill. On the next pass CD flags "sh_008 now drags — tonal sag". Producer applies CD's shorten. Editor re-flags "audio spill on sh_008". Ping-pong. The convergence gate in `tests/creative/resolver.py:is_convergence_failure` (resolver.py:68-90) catches the loop, but only after the second pass. Contract 5 pre-empts by ensuring CD's pre-Editor full-film review runs first and its outputs are resolved before Editor dispatches — no loop to catch.

**Tests.** `tests/contracts/test_cd_editor_authority.py` covers:
- Editor invocation, no unaddressed CD feedback: no violation.
- Editor invocation, 1 unaddressed CD `high`: block violation.
- Editor invocation, unaddressed CD `medium` only: no block (priority gate in producer.md § "Re-render decision rules" handles medium/low elsewhere).
- Shot-level: Editor + CD at same `high` priority on same shot: warn, Editor deferred.
- Shot-level: Editor at `high`, CD at `critical` on same shot: block — wrong authority resolution attempt.

---

## Registry integration

The five contracts are exposed through one entry point:

```python
from src.contracts import validate_before_dispatch, ContractViolation

violations = validate_before_dispatch(
    agent="editor_agent",            # or "prompt_smith" | "creative_director" | ...
    shot_id="sh_005",                # None for film-level dispatches
    manifest=manifest,
    ctx={"revision": True, "creative_driven": False, ...},
)
# Each violation: .contract, .severity, .shot_id, .reason, .detail
blocking = [v for v in violations if v.severity == "block"]
if blocking:
    raise ContractViolation(blocking)
for v in violations:
    if v.severity == "warn":
        producer.log_history(shot_id=v.shot_id, event="contract_warn", detail=v.reason)
```

The dispatcher maps `(agent, ctx)` to the contracts that apply:

| Agent | Context flags | Contracts checked |
|---|---|---|
| `prompt_smith` | `revision=True` | 2 (and 3 if `creative_driven=True`) |
| `editor_agent` | — | 1 (per shot with audio), 5 (film-level) |
| `creative_director` | — | 4 (sanitization helper invoked) |
| `shot_judge` | — | none (Judge only reads its own inputs) |
| `audio_agent` | — | none (Audio has no pair-contract preconditions — Audio WRITES; it reads script, which is external) |
| `renderer` | `creative_driven=True` | 3 (artistic_direction must be in place before creative re-render) |

Additions to this table are changes to the contract surface and require a new test.

---

## Non-contracts (things that look like contracts but aren't)

- **Screenwriter → anyone** — one-shot. No feedback loop. Screenwriter completion is a stage boundary, not a pair contract.
- **Shot Judge ↔ Audio Agent** — no shared manifest fields.
- **Renderer ↔ anyone** — Renderer is invoked with explicit arguments, not manifest-mediated. Pre/post conditions are internal to the renderer.
- **Budget / cost accounting** — enforced in Producer invariants (see [docs/agents.md § Tier 1 — Producer](agents.md#tier-1--producer)), not in a pair contract.
