"""Reference resolver for the Producer's creative-feedback rules.

Mirrors the rules in `prompts/producer.md` § "Creative feedback integration".
The Producer's actual implementation (TBD) must satisfy these same invariants.
This module is the executable specification; `test_loop_scenarios.py` exercises it.

Scope intentionally narrow: pure functions over dicts, no state, no I/O. The
Producer's runtime embeds these decisions inside a larger orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PRIORITY_ORDER = {"critical": 3, "high": 2, "medium": 1, "low": 0}

AUTHORITY_ORDER = {
    "creative_director": 2,  # aesthetic tie-breaker
    "editor_agent": 1,
    "audio_agent": 1,
    "shot_judge": 1,
}

Action = Literal[
    "rerender",
    "duration_adjust",
    "reorder",
    "merge",
    "style_pivot",
    "escalate",
    "dismiss",
    "defer",
]


@dataclass(frozen=True)
class Decision:
    action: Action
    chosen_index: int | None
    rationale: str


def _priority_key(entry: dict) -> tuple[int, int]:
    """Sort key: (priority rank, authority rank). Higher is better."""
    p = PRIORITY_ORDER.get(entry.get("priority", "low"), 0)
    a = AUTHORITY_ORDER.get(entry.get("from_agent", ""), 0)
    return (p, a)


def rank_unaddressed(feedback_list: list[dict]) -> list[int]:
    """Return indices of unaddressed entries, highest-priority first."""
    unaddressed = [
        (i, e) for i, e in enumerate(feedback_list) if not e.get("addressed", False)
    ]
    unaddressed.sort(key=lambda pair: _priority_key(pair[1]), reverse=True)
    return [i for i, _ in unaddressed]


def budget_allows_rerender(
    budget: dict, estimated_cost_usd: float, cap_ratio: float = 0.95
) -> bool:
    """False when a re-render would push spent_usd past cap_ratio * cap_usd."""
    projected = budget["spent_usd"] + estimated_cost_usd
    return projected <= budget["cap_usd"] * cap_ratio


def is_convergence_failure(feedback_list: list[dict], candidate: dict) -> bool:
    """True if an already-addressed entry substantively matches the candidate.

    Signature for equality: same from_agent + same priority + same suggestion text
    (case-insensitive). Good enough for the hackathon; production can swap in
    an embedding-distance check.
    """
    sig = (
        candidate.get("from_agent"),
        candidate.get("priority"),
        (candidate.get("suggestion") or "").strip().lower(),
    )
    for entry in feedback_list:
        if not entry.get("addressed"):
            continue
        prior_sig = (
            entry.get("from_agent"),
            entry.get("priority"),
            (entry.get("suggestion") or "").strip().lower(),
        )
        if prior_sig == sig:
            return True
    return False


def brief_aligned(suggestion: str, brief: dict) -> bool:
    """Soft check: suggestion references a tone/style term the brief uses.

    Real implementation will be LLM-scored; this keyword check covers the
    common drift-detection cases in tests and acts as a deterministic floor.
    """
    if not suggestion:
        return True
    text = suggestion.lower()
    terms: list[str] = []
    if brief.get("artistic_style"):
        terms.extend(w.strip(",.").lower() for w in brief["artistic_style"].split())
    terms.extend(t.lower() for t in brief.get("tone", []))
    if not terms:
        return True
    anti_style_flags = ["bright colorful", "upbeat", "neon", "saturated"]
    for flag in anti_style_flags:
        if flag in text and any(
            t in {"noir", "moody", "muted", "minimal"} for t in terms
        ):
            return False
    return True


def resolve(
    shot: dict,
    brief: dict,
    budget: dict,
    estimated_rerender_cost_usd: float = 1.50,
    max_creative_rerenders: int = 2,
) -> Decision:
    """Apply the decision gates to one shot. Pure function; caller performs the action.

    Gate order matches prompts/producer.md § "Re-render decision rules".
    """
    feedback = shot.get("creative_feedback", [])
    ranked = rank_unaddressed(feedback)

    if not ranked:
        return Decision(action="dismiss", chosen_index=None, rationale="No unaddressed feedback.")

    top_idx = ranked[0]
    top = feedback[top_idx]
    priority = top.get("priority", "low")

    # Priority gate
    if PRIORITY_ORDER[priority] < PRIORITY_ORDER["high"]:
        return Decision(
            action="defer",
            chosen_index=top_idx,
            rationale=f"Priority '{priority}' does not warrant re-render; note and move on.",
        )

    # Convergence gate
    if is_convergence_failure(feedback, top):
        if brief.get("allow_artistic_experiments"):
            return Decision(
                action="style_pivot",
                chosen_index=top_idx,
                rationale="Convergence hit but artistic experiments enabled — pivot style rather than loop.",
            )
        return Decision(
            action="escalate",
            chosen_index=top_idx,
            rationale="Same feedback after prior address; pipeline cannot improve this shot.",
        )

    # Creative-rerender cap
    creative_rerenders = sum(
        1
        for a in shot.get("attempts", [])
        if a.get("prompt_revision", "").startswith("creative:")
    )
    if creative_rerenders >= max_creative_rerenders:
        return Decision(
            action="escalate",
            chosen_index=top_idx,
            rationale=f"Already executed {creative_rerenders} creative re-renders on this shot.",
        )

    # Budget gate
    if not budget_allows_rerender(budget, estimated_rerender_cost_usd):
        suggestion = (top.get("suggestion") or "").lower()
        if "merge" in suggestion:
            return Decision(
                action="merge",
                chosen_index=top_idx,
                rationale="Budget tight; merging is the suggested cheaper pivot.",
            )
        if "extend" in suggestion or "shorten" in suggestion:
            return Decision(
                action="duration_adjust",
                chosen_index=top_idx,
                rationale="Budget tight; duration change avoids re-render cost.",
            )
        return Decision(
            action="escalate",
            chosen_index=top_idx,
            rationale="Re-render would breach 95% budget cap and no cheaper pivot was suggested.",
        )

    # Brief-alignment soft-deprioritize
    if not brief_aligned(top.get("suggestion", ""), brief):
        return Decision(
            action="defer",
            chosen_index=top_idx,
            rationale="Suggestion drifts from brief.artistic_style; deprioritized one level.",
        )

    # Map suggestion to concrete action
    suggestion = (top.get("suggestion") or "").lower()
    if "reorder" in suggestion or "swap" in suggestion:
        return Decision(
            action="reorder",
            chosen_index=top_idx,
            rationale="Reorder is free; apply the suggested sequence change.",
        )
    if "merge" in suggestion:
        return Decision(
            action="merge",
            chosen_index=top_idx,
            rationale="Merge suggestion authorized; write to creative_decisions.",
        )
    if "extend" in suggestion or "shorten" in suggestion:
        return Decision(
            action="duration_adjust",
            chosen_index=top_idx,
            rationale="Duration change is cheaper than re-render; apply first.",
        )
    return Decision(
        action="rerender",
        chosen_index=top_idx,
        rationale="Passed all gates; re-render with updated artistic_direction.",
    )
