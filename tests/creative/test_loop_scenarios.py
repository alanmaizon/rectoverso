"""Creative-loop scenarios — answers "what does a successful creative loop look like?"

Each test is one scenario from `RESEARCH_DAY2.md` turned into an assertion about
the Producer's expected behavior. The reference resolver in `resolver.py` is the
spec; Producer's runtime must produce the same decisions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tests.creative.resolver import (
    brief_aligned,
    budget_allows_rerender,
    is_convergence_failure,
    rank_unaddressed,
    resolve,
)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _feedback(
    from_agent: str,
    priority: str,
    suggestion: str,
    feedback: str = "",
    addressed: bool = False,
) -> dict:
    return {
        "ts": _ts(),
        "from_agent": from_agent,
        "feedback": feedback or suggestion,
        "suggestion": suggestion,
        "priority": priority,
        "addressed": addressed,
    }


def _shot(
    shot_id: str = "sh_003",
    creative_feedback: list | None = None,
    attempts: list | None = None,
    **overrides,
) -> dict:
    base = {
        "shot_id": shot_id,
        "scene": 1,
        "order": int(shot_id.split("_")[1]),
        "description": "test",
        "duration_s": 3.0,
        "has_humans": False,
        "is_hero": False,
        "motion_level": "low",
        "continuity_refs": [],
        "prompt": {"authored_by": "prompt_smith", "primary": "test"},
        "routing": {
            "chosen_provider": "alibaba_wan_2_7_plus",
            "chosen_model": "wan-2.7-plus",
            "rationale": "test",
            "decided_by": "router",
            "decided_at": _ts(),
            "alternates": [],
        },
        "attempts": attempts or [],
        "status": "approved",
        "history": [],
        "judge_feedback": [],
        "creative_feedback": creative_feedback or [],
    }
    base.update(overrides)
    return base


def _brief(**overrides) -> dict:
    base = {
        "logline": "test",
        "target_duration_s": 30.0,
        "tone": ["moody", "minimal"],
        "genre": "thriller",
        "source_path": "inputs/brief.md",
        "artistic_style": "coastal noir, muted palette, handheld",
    }
    base.update(overrides)
    return base


def _budget(spent: float = 10.0, cap: float = 151.0) -> dict:
    return {
        "cap_usd": cap,
        "spent_usd": spent,
        "by_provider": {},
        "alibaba_quota_remaining": 50,
        "elevenlabs_credits_remaining": 100000,
    }


# ---------------------------------------------------------------------------
# Scenario 1 — single critical feedback triggers re-render
# ---------------------------------------------------------------------------


def test_critical_feedback_passes_all_gates_and_rerenders():
    """Creative Director flags a critical tonal break; budget is healthy; re-render."""
    shot = _shot(
        creative_feedback=[
            _feedback(
                "creative_director",
                "critical",
                "re-render with slower handheld motion",
                "sh_003 motion breaks the quiet tone established by sh_002",
            )
        ]
    )
    decision = resolve(shot, _brief(), _budget(spent=10.0))
    assert decision.action == "rerender"
    assert decision.chosen_index == 0


# ---------------------------------------------------------------------------
# Scenario 2 — conflicting high-priority: Editor vs Audio → CD breaks tie
# ---------------------------------------------------------------------------


def test_creative_director_breaks_tie_at_same_priority():
    """Editor says extend, Audio says compress. CD's high-priority suggestion wins at the top."""
    shot = _shot(
        creative_feedback=[
            _feedback("editor_agent", "high", "extend sh_003 by 0.5s"),
            _feedback("audio_agent", "high", "shorten sh_003 by 0.3s to match dialogue"),
            _feedback(
                "creative_director",
                "high",
                "extend sh_003 by 0.5s — dialogue can breathe longer in the noir tone",
            ),
        ]
    )
    ranked = rank_unaddressed(shot["creative_feedback"])
    # CD should rank ahead of Editor/Audio at the same priority (higher authority).
    assert ranked[0] == 2
    decision = resolve(shot, _brief(), _budget(spent=5.0))
    assert decision.action == "duration_adjust"
    assert decision.chosen_index == 2


# ---------------------------------------------------------------------------
# Scenario 3 — budget gate: >95% cap forces cheaper pivot
# ---------------------------------------------------------------------------


def test_budget_gate_blocks_rerender_near_cap():
    """Re-render that would push spent past 95% of cap must not fire."""
    budget = _budget(spent=145.0)  # 151 cap, 95% = 143.45
    assert not budget_allows_rerender(budget, estimated_cost_usd=1.50)


def test_budget_gate_routes_extend_to_duration_adjust():
    """When budget is tight but the suggestion is an extend, do the free pivot."""
    shot = _shot(
        creative_feedback=[
            _feedback("creative_director", "high", "extend sh_003 by 0.4s to add breathing room")
        ]
    )
    decision = resolve(shot, _brief(), _budget(spent=148.0), estimated_rerender_cost_usd=1.5)
    assert decision.action == "duration_adjust"


def test_budget_gate_escalates_when_no_cheap_pivot():
    """Budget tight and suggestion requires a re-render → escalate."""
    shot = _shot(
        creative_feedback=[
            _feedback(
                "creative_director",
                "high",
                "re-render sh_003 with slower handheld motion",
            )
        ]
    )
    decision = resolve(shot, _brief(), _budget(spent=148.0), estimated_rerender_cost_usd=1.5)
    assert decision.action == "escalate"


# ---------------------------------------------------------------------------
# Scenario 4 — convergence: same feedback after prior address → escalate
# ---------------------------------------------------------------------------


def test_convergence_detected_on_repeated_suggestion():
    prior_addressed = _feedback(
        "creative_director",
        "high",
        "re-render with slower handheld motion",
        addressed=True,
    )
    candidate = _feedback(
        "creative_director",
        "high",
        "re-render with slower handheld motion",
    )
    assert is_convergence_failure([prior_addressed], candidate)


def test_convergence_escalates_when_experiments_disabled():
    addressed = _feedback("creative_director", "high", "re-render slower", addressed=True)
    new_same = _feedback("creative_director", "high", "re-render slower")
    shot = _shot(creative_feedback=[addressed, new_same])
    decision = resolve(shot, _brief(allow_artistic_experiments=False), _budget(spent=10.0))
    assert decision.action == "escalate"


def test_convergence_style_pivots_when_experiments_enabled():
    addressed = _feedback("creative_director", "high", "re-render slower", addressed=True)
    new_same = _feedback("creative_director", "high", "re-render slower")
    shot = _shot(creative_feedback=[addressed, new_same])
    decision = resolve(shot, _brief(allow_artistic_experiments=True), _budget(spent=10.0))
    assert decision.action == "style_pivot"


# ---------------------------------------------------------------------------
# Scenario 5 — priority gate: low/medium feedback is deferred, not actioned
# ---------------------------------------------------------------------------


def test_medium_priority_is_deferred_not_rerendered():
    shot = _shot(
        creative_feedback=[
            _feedback("editor_agent", "medium", "consider slightly longer hold at start")
        ]
    )
    decision = resolve(shot, _brief(), _budget(spent=10.0))
    assert decision.action == "defer"


def test_empty_feedback_list_is_noop_dismiss():
    decision = resolve(_shot(creative_feedback=[]), _brief(), _budget())
    assert decision.action == "dismiss"
    assert decision.chosen_index is None


# ---------------------------------------------------------------------------
# Scenario 6 — brief alignment: drift from artistic_style → deprioritize
# ---------------------------------------------------------------------------


def test_brief_alignment_passes_for_style_consistent_suggestion():
    brief = _brief(artistic_style="coastal noir, muted palette")
    assert brief_aligned("re-render with muted handheld framing", brief)


def test_brief_alignment_flags_style_drift():
    brief = _brief(artistic_style="coastal noir, muted palette", tone=["moody", "minimal"])
    assert not brief_aligned("re-render with bright colorful saturated lighting", brief)


def test_style_drifting_suggestion_is_deferred():
    shot = _shot(
        creative_feedback=[
            _feedback(
                "editor_agent",
                "high",
                "re-render with bright colorful saturated lighting to pop against mid-section",
            )
        ]
    )
    decision = resolve(shot, _brief(), _budget(spent=10.0))
    assert decision.action == "defer"
    assert "drifts from brief" in decision.rationale


# ---------------------------------------------------------------------------
# Scenario 7 — film-level pivot: reorder suggestion produces reorder action
# ---------------------------------------------------------------------------


def test_reorder_suggestion_maps_to_reorder_action():
    shot = _shot(
        creative_feedback=[
            _feedback(
                "creative_director",
                "high",
                "swap sh_004 and sh_005 — current order front-loads action",
            )
        ]
    )
    decision = resolve(shot, _brief(), _budget(spent=10.0))
    assert decision.action == "reorder"


def test_merge_suggestion_maps_to_merge_action():
    shot = _shot(
        creative_feedback=[
            _feedback(
                "creative_director",
                "high",
                "merge sh_008 and sh_009 into one extended hero",
            )
        ]
    )
    decision = resolve(shot, _brief(), _budget(spent=10.0))
    assert decision.action == "merge"


# ---------------------------------------------------------------------------
# Scenario 8 — creative-rerender cap: 2 prior creative re-renders → escalate
# ---------------------------------------------------------------------------


def test_creative_rerender_cap_escalates_on_third_try():
    shot = _shot(
        creative_feedback=[
            _feedback("creative_director", "high", "re-render with different motion")
        ],
        attempts=[
            {
                "attempt_id": 1,
                "provider": "alibaba_wan_2_7_plus",
                "started_at": _ts(),
                "outcome": "rejected",
                "rejection_reason": "auto_judge",
                "prompt_revision": "creative: first pivot",
            },
            {
                "attempt_id": 2,
                "provider": "alibaba_wan_2_7_plus",
                "started_at": _ts(),
                "outcome": "rejected",
                "rejection_reason": "auto_judge",
                "prompt_revision": "creative: second pivot",
            },
        ],
    )
    decision = resolve(shot, _brief(), _budget(spent=10.0))
    assert decision.action == "escalate"
    assert "creative re-renders" in decision.rationale
