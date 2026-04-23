# Day 3 Research: Artistic Pipeline, Deterministic Default

## Confirmed Design Shifts

### 1. Adaptive Orchestration (artistic, not deterministic)
- Producer reasons about **narrative arc, pacing, tone consistency** across all shots
- Can **re-sequence shots mid-pipeline** based on creative feedback
- Can **adjust shot duration** if pacing feels off
- Can **suggest reframing** (wider/tighter shot) if continuity or composition fails

**New role**: Creative Director agent (Tier 1.5)
- Reads all shot feedback (Judge notes, Editor timing, Audio duration)
- Outputs: "shots should be reordered as [4,2,1,3,5]" or "sh_003 needs 1.2s longer"
- Producer executes or escalates to user

### 2. Technical Feedback (Judge as analyst, not scorekeeper)
Shot Judge evolves from binary pass/fail to:
- "Composition is strong, but lighting is flat — suggest 40% brighter"
- "Continuity break: this shot's horizon is 20px higher than sh_002"
- "Timing concern: this 4s shot feels rushed given the previous 2s cut"

**Data model**: `shot.judge_feedback[]` (append-only)
- suggestion_type: `composition | lighting | timing | continuity | artifact`
- severity: `critical | warn | note`
- actionable_suggestion: text (e.g., "reduce saturation, increase contrast")

### 3. Cross-shot Creative Loops (agents collaborate through manifest)
Example loop:
1. Editor Agent reads all shots → suggests: "shots 2 & 3 back-to-back feel rushed, extend one of them"
2. Producer reads suggestion → asks Audio Agent: "can you compress dialogue in sh_003 by 0.5s?"
3. Audio Agent responds: "yes, but it'll sound hurried; recommend extending sh_003 video by 0.3s instead"
4. Producer reads decision → asks PromptSmith: "re-author sh_003 prompt with slower pacing"
5. PromptSmith updates prompt with notes like "slow, deliberate motion"
6. Producer re-renders sh_003

**Manifest**: `shot.creative_feedback[]` (agent-to-agent suggestions)
```json
{
  "from_agent": "editor_agent",
  "timestamp": "2026-04-22T14:30:00Z",
  "feedback": "Shots 2–3 pacing feels rushed",
  "suggestion": "extend_one_shot",
  "addressed_by": "producer",
  "action_taken": "re-render sh_003 with slower motion"
}
```

### 4. Budget-aware Creative Pivots (constraint = creative decision)
When budget pressure hits (Veo quota low, USD cap approached):
- Producer doesn't just "downgrade provider"
- Producer reasons: "How can I tell this story with fewer, higher-quality shots?"
- Options:
  - Merge two adjacent shots into one longer hero shot
  - Reduce shot count, increase remaining shot duration
  - Shift from action-heavy to dialogue-heavy (cheaper to render)
  - Switch from Veo to Kling but frame as "stylistic choice"

**Manifest**: `shot.budget_decision`
```json
{
  "original_plan": "12 shots, $140 budget",
  "constraint_hit": "USD $120 spent, only $31 remaining, 3 hero shots unseen",
  "creative_pivot": "merge shots 8-9 into extended hero, reduce total to 11 shots",
  "rationale": "longer, more cinematic single shot > two rushed ones under budget pressure"
}
```

### 5. Failure Recovery as Artistic Experimentation (TBD)
**Pros**:
- Retry Veo hero with "film noir" style instead of naturalism → happy accident
- Fallback to Kling but frame as "stylistic shift" in brief
- Could discover unexpected creative directions

**Cons**:
- Diverges from original brief intent
- Risk of incoherent final film if style shifts mid-production
- User loses control over "what failure means"
- Could be seen as a hack, not a feature

**Proposal**: Make this opt-in via brief parameter `allow_artistic_experiments: true/false`
- If true: Producer can reinterpret failed shots creatively
- If false: deterministic fallback (current behavior)
- Document in `shot.history[]` what happened

---

## Agent Roles (revised architecture)

| Tier | Agent | New/Changed | Responsibility |
|------|-------|-----------|-----------------|
| 1 | Producer | ← UNCHANGED | Orchestrates, reads all feedback, makes creative + budget decisions |
| 1.5 | **Creative Director** | NEW | Reasons about narrative arc, pacing, tone; suggests reordering/retiming |
| 2 | Shot Judge | EVOLVED | Provides analytical feedback, not just scores |
| 2 | Audio Agent | EVOLVED | Responds to Editor's timing feedback |
| 2 | Editor Agent | EVOLVED | Suggests shot reordering/pacing adjustments |
| 3 | PromptSmith | EVOLVED | Takes "artistic direction" (e.g., "slower motion") and crafts prompt |
| 3 | Screenwriter | UNCHANGED | Initial script + brief analysis |
| 4 | Renderer | UNCHANGED | API calls |

---

## Critical Questions (research phase)

1. **Creative Director scope**: Does it live as a separate agent session, or reasoning inside Producer?
   - Separate: cleaner, more parallelizable
   - Inside Producer: simpler, faster feedback loops

2. **Feedback aggregation**: How does Producer weigh conflicting feedback?
   - "Editor says extend sh_003" vs "Audio says compress it"
   - Who has final say? (Producer needs decision rules)

3. **Rerender trigger**: When does Producer commit to re-rendering?
   - After every feedback loop? (slow, expensive)
   - After feedback stabilizes? (need convergence detection)
   - After human approval? (kills automation)

4. **Style consistency**: If we're doing artistic pivots, how do we prevent divergence?
   - "Artistic direction" must be baked into every prompt
   - Need `brief.artistic_style` as anchor

5. **Demo narrative**: What's the *story* of the film production?
   - Show original brief → show iterative improvements → show final film
   - Show Producer's creative decisions in action
   - More compelling than "here's a film"
