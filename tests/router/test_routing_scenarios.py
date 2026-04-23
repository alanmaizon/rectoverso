"""End-to-end routing scenarios.

These tests describe the expected provider choice for realistic shot types
across the rectoverso pipeline. They exercise rule interactions rather than
individual rules. If one of these breaks, check test_hard_rules.py to see
which rule regressed.
"""

from __future__ import annotations

import pytest

from src.router import RoutingError, route

VEO = "vertex_veo_3_1_fast"
WAN_PLUS = "alibaba_wan_2_7_plus"
WAN_TURBO = "alibaba_wan_2_7_turbo"
KLING_STD = "fal_kling_2_1_standard"
KLING_PRO = "fal_kling_2_1_pro"
ELEVEN = "elevenlabs"


# --- Canonical scenarios --------------------------------------------------


def test_human_shot_routes_to_kling(capabilities, make_shot, make_budget):
    shot = make_shot(has_humans=True, is_hero=False, duration_s=5.0)
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id in {KLING_STD, KLING_PRO}


def test_non_human_hero_shot_routes_to_veo(capabilities, make_shot, make_budget):
    """Hero + non-human + low motion → Veo, the whole point of the specialty tier.
    Run mode must be "submission" — Veo is submission-tier only."""
    shot = make_shot(
        has_humans=False,
        is_hero=True,
        motion_level="low",
        duration_s=5.0,
        run_mode="submission",
    )
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id == VEO


def test_non_human_workhorse_shot_routes_to_wan_plus(
    capabilities, make_shot, make_budget
):
    shot = make_shot(has_humans=False, is_hero=False, motion_level="low", duration_s=5.0)
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id == WAN_PLUS


def test_audio_modality_routes_to_elevenlabs(capabilities, make_shot, make_budget):
    shot = make_shot(
        modality="audio",
        duration_s=3.0,
        estimated_credit_cost=500,
    )
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id == ELEVEN


def test_retry_after_wan_plus_failure_goes_elsewhere(
    capabilities, make_shot, make_budget, failure
):
    """A rejected shot must not route to the same provider that just failed."""
    shot = make_shot(
        has_humans=False,
        is_hero=False,
        prior_failures=(failure(WAN_PLUS),),
    )
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id != WAN_PLUS


def test_rationale_is_populated(capabilities, make_shot, make_budget):
    shot = make_shot(has_humans=True, duration_s=5.0)
    choice = route(shot, make_budget(), capabilities)
    assert shot.shot_id not in choice.rationale  # shot id need not appear
    assert choice.provider_id in choice.rationale
    assert "tier=" in choice.rationale


def test_model_id_matches_capabilities(capabilities, make_shot, make_budget):
    shot = make_shot(has_humans=False, is_hero=False, duration_s=5.0)
    choice = route(shot, make_budget(), capabilities)
    expected = capabilities.providers[choice.provider_id].get("model_id")
    assert choice.model_id == expected


def test_alternates_exclude_winner(capabilities, make_shot, make_budget):
    shot = make_shot(has_humans=True, duration_s=5.0)
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id not in choice.alternates


# --- Budget interactions --------------------------------------------------


def test_exhausted_fal_budget_falls_back_to_wan_for_human_shot(
    capabilities, make_shot, make_budget
):
    """When Kling is priced out by the global budget cap, a human shot falls
    back to Wan even though Wan scores lower on human_scenes."""
    shot = make_shot(has_humans=True, is_hero=False, duration_s=5.0)
    # Tight cap: Kling Standard costs $0.25 for 5s. Leave only $0.10.
    budget = make_budget(cap_usd=150.10, spent_usd=150.0)
    choice = route(shot, budget, capabilities)
    assert choice.provider_id in {WAN_PLUS, WAN_TURBO}


def test_exhausted_everything_raises(capabilities, make_shot, make_budget):
    """All video providers exhausted → RoutingError."""
    shot = make_shot(has_humans=True, is_hero=False, duration_s=5.0)
    budget = make_budget(
        cap_usd=1.0,
        spent_usd=1.0,  # no room for anything paid
        alibaba_quota_remaining=0,  # no Wan
    )
    with pytest.raises(RoutingError) as exc_info:
        route(shot, budget, capabilities)
    # The error should identify at least one exclusion.
    assert exc_info.value.exclusions


def test_unsupported_duration_raises(capabilities, make_shot, make_budget):
    shot = make_shot(has_humans=False, is_hero=True, duration_s=30.0)  # nobody does 30s
    with pytest.raises(RoutingError):
        route(shot, make_budget(), capabilities)
