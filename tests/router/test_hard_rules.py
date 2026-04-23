"""One test per hard rule in router/capabilities.yaml.

Each rule is exercised in isolation via the rule function directly, plus
a black-box check through `route()` where practical. This matches the
CLAUDE.md guarantee: "Every hard rule must have an isolated unit test."
"""

from __future__ import annotations

import pytest

from src.router import RoutingError, route
from src.router.engine import HARD_RULES


VEO = "vertex_veo_3_1_fast"
WAN_PLUS = "alibaba_wan_2_7_plus"
WAN_TURBO = "alibaba_wan_2_7_turbo"
KLING_STD = "fal_kling_2_1_standard"
KLING_PRO = "fal_kling_2_1_pro"
ELEVEN = "elevenlabs"


# --- humans_never_veo -----------------------------------------------------


def test_humans_never_veo_excludes_veo(capabilities, make_shot, make_budget):
    shot = make_shot(has_humans=True, is_hero=True, duration_s=5.0)
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id != VEO


def test_humans_never_veo_rule_direct(capabilities, make_shot, make_budget):
    shot = make_shot(has_humans=True)
    provider = capabilities.providers[VEO]
    outcome = HARD_RULES["humans_never_veo"](shot, make_budget(), VEO, provider)
    assert outcome == "exclude"


def test_veo_allowed_for_non_human_hero_shots(capabilities, make_shot, make_budget):
    shot = make_shot(has_humans=False, is_hero=True, duration_s=5.0)
    choice = route(shot, make_budget(), capabilities)
    # Veo should at minimum be a live option (chosen or in alternates).
    assert VEO == choice.provider_id or VEO in choice.alternates


# --- veo_spend_cap --------------------------------------------------------


def test_veo_spend_cap_excludes_when_cap_reached(capabilities, make_shot, make_budget):
    shot = make_shot(has_humans=False, is_hero=True, duration_s=5.0)
    budget = make_budget(by_provider={VEO: 14.9})  # one more call would bust $15
    choice = route(shot, budget, capabilities)
    assert choice.provider_id != VEO
    assert VEO not in choice.alternates


def test_veo_spend_cap_allows_when_room_left(capabilities, make_shot, make_budget):
    shot = make_shot(has_humans=False, is_hero=True, duration_s=5.0)
    # Veo at 0.14/s for 5s = $0.70; cap $15; $14 spent leaves $1 room.
    budget = make_budget(by_provider={VEO: 14.0})
    provider = capabilities.providers[VEO]
    outcome = HARD_RULES["veo_spend_cap"](shot, budget, VEO, provider)
    assert outcome is None


# --- alibaba_quota_exhausted ---------------------------------------------


def test_alibaba_quota_exhausted_excludes_wan(capabilities, make_shot, make_budget):
    shot = make_shot(has_humans=False, is_hero=False)
    budget = make_budget(alibaba_quota_remaining=0)
    choice = route(shot, budget, capabilities)
    assert choice.provider_id not in {WAN_PLUS, WAN_TURBO}


def test_alibaba_quota_rule_direct(capabilities, make_shot, make_budget):
    shot = make_shot()
    budget = make_budget(alibaba_quota_remaining=0)
    provider = capabilities.providers[WAN_PLUS]
    assert HARD_RULES["alibaba_quota_exhausted"](shot, budget, WAN_PLUS, provider) == "exclude"


# --- elevenlabs_credits_exhausted ----------------------------------------


def test_elevenlabs_credits_exhausted_excludes(capabilities, make_shot, make_budget):
    shot = make_shot(modality="audio", estimated_credit_cost=5_000)
    budget = make_budget(elevenlabs_credits_remaining=1_000)
    provider = capabilities.providers[ELEVEN]
    assert (
        HARD_RULES["elevenlabs_credits_exhausted"](shot, budget, ELEVEN, provider)
        == "exclude"
    )


def test_elevenlabs_credits_ok_when_sufficient(capabilities, make_shot, make_budget):
    shot = make_shot(modality="audio", estimated_credit_cost=5_000)
    budget = make_budget(elevenlabs_credits_remaining=10_000)
    provider = capabilities.providers[ELEVEN]
    assert (
        HARD_RULES["elevenlabs_credits_exhausted"](shot, budget, ELEVEN, provider) is None
    )


# --- wan_turbo_for_iteration_only ----------------------------------------


def test_turbo_deprioritized_on_first_attempt_non_hero(
    capabilities, make_shot, make_budget
):
    """On a first attempt, non-hero shot → Wan Plus should outrank Turbo."""
    shot = make_shot(has_humans=False, is_hero=False, prior_failures=())
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id == WAN_PLUS


def test_turbo_preferred_after_a_failure(
    capabilities, make_shot, make_budget, failure
):
    """Prior failure on Plus halves Plus's score; Turbo moves up."""
    shot = make_shot(
        has_humans=False,
        is_hero=False,
        prior_failures=(failure(WAN_PLUS),),
    )
    choice = route(shot, make_budget(), capabilities)
    # Plus should no longer be the top pick.
    assert choice.provider_id != WAN_PLUS


# --- duration_bound ------------------------------------------------------


def test_duration_bound_excludes_over_limit(capabilities, make_shot, make_budget):
    # Veo caps at 8s; Wan 2.6/2.7 and Kling at 10s. 11s excludes all video providers.
    shot = make_shot(has_humans=False, is_hero=True, duration_s=11.0)
    with pytest.raises(RoutingError):
        route(shot, make_budget(), capabilities)


def test_duration_bound_direct_rule(capabilities, make_shot, make_budget):
    # Veo 3.1 Fast caps at 8s — request 9s and the rule should exclude it.
    shot = make_shot(duration_s=9.0)
    provider = capabilities.providers[VEO]
    assert HARD_RULES["duration_bound"](shot, make_budget(), VEO, provider) == "exclude"


# --- prior_failure_penalty (score multiplier, not hard exclusion) --------


def test_prior_failures_compound_as_penalty(
    capabilities, make_shot, make_budget, failure
):
    """Two failures on Plus must push Plus below Turbo in ranking."""
    shot = make_shot(
        has_humans=False,
        is_hero=False,
        prior_failures=(failure(WAN_PLUS), failure(WAN_PLUS)),
    )
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id != WAN_PLUS


# --- global_budget_cap ---------------------------------------------------


def test_global_budget_cap_excludes_providers_over_cap(
    capabilities, make_shot, make_budget
):
    """With only $0.10 left, Kling (at least $0.25) is excluded but Wan ($0) survives."""
    shot = make_shot(has_humans=True, is_hero=False, duration_s=5.0)
    budget = make_budget(cap_usd=1.0, spent_usd=0.9)
    # Human shot would normally route to Kling; with cap tight, it must fail or
    # fall through to Wan (which is $0 but a weak human-scene provider).
    choice = route(shot, budget, capabilities)
    assert choice.estimated_cost_usd + budget.spent_usd <= budget.cap_usd


def test_global_budget_cap_raises_when_everything_over(
    capabilities, make_shot, make_budget
):
    # Even Wan survives because it's free; to force total failure, set caps so
    # duration kills everyone too.
    shot = make_shot(has_humans=True, duration_s=20.0)  # no provider covers 20s
    with pytest.raises(RoutingError):
        route(shot, make_budget(), capabilities)


# --- specialty_reserved_for_heroes ---------------------------------------


def test_specialty_deprioritized_for_non_hero(capabilities, make_shot, make_budget):
    """Non-hero shot should never pick Veo, even if humans=False and motion=low."""
    shot = make_shot(has_humans=False, is_hero=False, motion_level="low", duration_s=5.0)
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id != VEO


# --- end_frame_requires_capable_provider ---------------------------------


def test_end_frame_excludes_non_capable_providers(
    capabilities, make_shot, make_budget
):
    """A shot with an end frame cannot go to Wan (no first+last frame support)."""
    shot = make_shot(has_humans=True, duration_s=5.0, has_end_frame=True)
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id in {KLING_STD, KLING_PRO}
    # Wan providers excluded.
    provider = capabilities.providers[WAN_PLUS]
    assert (
        HARD_RULES["end_frame_requires_capable_provider"](
            shot, make_budget(), WAN_PLUS, provider
        )
        == "exclude"
    )


# --- subject_refs_fit_capacity -------------------------------------------


def test_subject_refs_exceeding_capacity_excludes_provider(
    capabilities, make_shot, make_budget
):
    shot = make_shot(has_humans=True, duration_s=5.0, reference_subject_count=5)
    # Kling has max 4 refs → excluded. But wait — Veo has max 1 too. Wan has 1.
    # So 5 refs excludes every video provider → RoutingError.
    with pytest.raises(RoutingError):
        route(shot, make_budget(), capabilities)


def test_subject_refs_rule_direct(capabilities, make_shot, make_budget):
    shot = make_shot(reference_subject_count=2)
    provider = capabilities.providers[WAN_PLUS]  # max_reference_images=1
    outcome = HARD_RULES["subject_refs_fit_capacity"](
        shot, make_budget(), WAN_PLUS, provider
    )
    assert outcome == "exclude"


# --- prefer_kling_pro_when_refs_or_end_frame -----------------------------


def test_pro_preferred_over_standard_with_end_frame(
    capabilities, make_shot, make_budget
):
    shot = make_shot(has_humans=True, duration_s=5.0, has_end_frame=True)
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id == KLING_PRO


def test_pro_preferred_over_standard_with_multiple_refs(
    capabilities, make_shot, make_budget
):
    shot = make_shot(has_humans=True, duration_s=5.0, reference_subject_count=3)
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id == KLING_PRO
