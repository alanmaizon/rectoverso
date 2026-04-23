# Day 2 Research: Opus 4.7 Prompting Guide (10 High-ROI Moves)

A concise set of tactics to extract maximum performance from **Claude Opus 4.7**, especially for engineering, agent workflows, and long-horizon tasks.

---

## 1. Front-load Context

Store core project goals, constraints, and invariants in a persistent file:

```

CLAUDE.md

```

This acts as a **stable context anchor**, reducing repeated prompt overhead and minimizing drift across long sessions.

**Why it matters:**  
Opus 4.7 leverages persistent context more effectively than prior versions, so early structure compounds over time.

---

## 2. Default to `xhigh` Effort

Use:

```

effort: xhigh

```

for most coding and technical reasoning tasks.

Reserve:

```

effort: max

````

only for:

- deeply nested logic
- novel algorithm design
- ambiguous problem spaces

**Tradeoff:**  
`max` increases latency and cost without proportional gains in standard workflows.

---

## 3. Toggle Effort Dynamically

Do not lock effort level globally.

Instead:

- **Increase effort** for:
  - architecture decisions
  - debugging complex failures
- **خفض effort** (drop to medium/high) for:
  - refactors
  - formatting
  - boilerplate generation

**Principle:** Treat effort as a **per-phase control knob**, not a static config.

---

## 4. Regression Test Prompts

Before upgrading from **Opus 4.6 → 4.7**:

- re-run critical prompts
- validate outputs against expected behavior

**Reason:**  
Model improvements can invalidate previously tuned prompts (especially those relying on brittle scaffolding).

---

## 5. Batch Questions

Prefer:

```text
Ask 5 related questions in one turn
````

instead of:

```text
5 sequential turns
```

**Benefits:**

* better global reasoning
* fewer context fragmentation issues
* improved consistency across answers

---

## 6. Use Examples (Show, Don’t Tell)

Instead of:

```text
Don't write verbose code
```

Use:

```text
Good example:
<concise implementation>
```

**Why it works:**
Opus 4.7 is strongly **pattern-aligned**—positive demonstrations outperform negative constraints.

---

## 7. Delete Old Scaffolding

Remove legacy instructions like:

* “Summarize your plan”
* “Explain step-by-step before coding”

**Reason:**
Opus 4.7 natively emits:

* progress updates
* structured reasoning
* intermediate plans

Redundant scaffolding can **degrade output quality**.

---

## 8. Fan Out Explicitly

If you want parallelism, say so:

```text
Spawn 3 subagents:
- one for backend
- one for frontend
- one for testing
```

**Default behavior in 4.7:**
More conservative agent spawning compared to earlier versions.

---

## 9. Review Plans, Not Diffs

Use **Plan Mode** before execution:

* `/ultraplan`
* or `Shift + Tab` (twice)

Focus on:

* intent
* architecture
* edge cases

**Why:**
Catching errors at the plan level is significantly cheaper than reviewing diffs after code is written.

---

## 10. Use Adaptive Thinking

Switch to:

```
thinking: adaptive
```

And remove:

```
budget_tokens
```

**What changes:**

* model dynamically allocates reasoning depth
* avoids over/under-thinking
* improves efficiency across mixed workloads

---

# Key Meta-Principle

Opus 4.7 shifts from:

> **Heavily scaffolded prompting**

to:

> **Lightweight, example-driven, dynamically controlled workflows**

---

## Practical Summary

| Area              | Old Approach      | 4.7 Optimized Approach    |
| ----------------- | ----------------- | ------------------------- |
| Context           | Inline prompts    | Persistent `CLAUDE.md`    |
| Effort            | Static            | Dynamic per task phase    |
| Instructions      | Rules-heavy       | Example-driven            |
| Planning          | After coding      | Before coding (Plan Mode) |
| Agents            | Implicit spawning | Explicit fan-out          |
| Reasoning Control | Token budgets     | Adaptive thinking         |

---

## Bottom Line

Opus 4.7 rewards:

* **clear structure upfront**
* **explicit orchestration**
* **minimal but high-signal prompts**

And penalizes:

* over-instruction
* legacy scaffolding
* rigid workflows

Treat it less like a script executor and more like a **self-directed system you guide at the boundaries**.

```
