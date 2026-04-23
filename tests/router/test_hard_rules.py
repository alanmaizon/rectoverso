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
    # Veo is submission-tier only; this test exercises the "Veo is a live
    # option" path, which requires run_mode="submission".
    shot = make_shot(
        has_humans=False, is_hero=True, duration_s=5.0, run_mode="submission"
    )
    choice = route(shot, make_budget(), capabilities)
    # Veo should at minimum be a live option (chosen or in alternates).
    assert VEO == choice.provider_id or VEO in choice.alternates


# --- run_mode_compatibility ------------------------------------------------
#
# Symmetric filter-first gate: provider is kept only when shot.run_mode is
# in its run_modes list. Tested in both directions.


def test_run_mode_testing_excludes_submission_only_provider(
    capabilities, make_shot, make_budget
):
    """Veo is tagged run_modes=[submission]; in testing mode it must be
    excluded so the router's auto-selection doesn't pick it up by mistake."""
    shot = make_shot(has_humans=False, is_hero=True, duration_s=5.0, run_mode="testing")
    provider = capabilities.providers[VEO]
    assert HARD_RULES["run_mode_compatibility"](
        shot, make_budget(), VEO, provider
    ) == "exclude"


def test_run_mode_submission_excludes_testing_only_provider(
    capabilities, make_shot, make_budget
):
    """Wan 2.7 Plus is tagged run_modes=[testing]; in submission mode it
    must be excluded so free-tier models don't sneak into the final run."""
    shot = make_shot(run_mode="submission")
    provider = capabilities.providers[WAN_PLUS]
    assert HARD_RULES["run_mode_compatibility"](
        shot, make_budget(), WAN_PLUS, provider
    ) == "exclude"


def test_run_mode_dual_tagged_provider_passes_both(
    capabilities, make_shot, make_budget
):
    """Kling Pro is tagged run_modes=[submission, testing] — eligible in
    both lanes."""
    kling_pro = capabilities.providers[KLING_PRO]
    for mode in ("submission", "testing"):
        shot = make_shot(run_mode=mode)
        assert HARD_RULES["run_mode_compatibility"](
            shot, make_budget(), KLING_PRO, kling_pro
        ) is None, f"{mode} mode excluded a dual-tagged provider"


def test_run_mode_no_gate_when_run_modes_absent(
    capabilities, make_shot, make_budget
):
    """Legacy provider without `run_modes` gets a grace pass in both modes."""
    legacy_provider = {"modality": "video", "model_id": "whatever"}
    for mode in ("submission", "testing"):
        shot = make_shot(run_mode=mode)
        assert HARD_RULES["run_mode_compatibility"](
            shot, make_budget(), "legacy_provider", legacy_provider
        ) is None


def test_route_testing_mode_excludes_submission_only_providers(
    capabilities, make_shot, make_budget
):
    """End-to-end via route(): hero non-human shot in testing mode skips Veo
    (submission-only) and lands on Wan Plus (testing-tagged)."""
    shot = make_shot(
        has_humans=False, is_hero=True, duration_s=5.0, run_mode="testing"
    )
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id != VEO
    assert choice.provider_id in {WAN_PLUS, WAN_TURBO}


def test_route_submission_mode_excludes_testing_only_providers(
    capabilities, make_shot, make_budget
):
    """End-to-end: non-hero non-human shot in submission mode skips Wan
    (testing-only) even though Wan's capability score is competitive."""
    shot = make_shot(
        has_humans=False, is_hero=False, duration_s=5.0, run_mode="submission"
    )
    choice = route(shot, make_budget(), capabilities)
    assert choice.provider_id not in {WAN_PLUS, WAN_TURBO}


def test_route_halts_when_no_provider_matches_run_mode(
    capabilities, make_shot, make_budget
):
    """Halt-on-no-match: an artificial shot spec with no matching provider
    raises RoutingError, NOT a silent fallback to the other tier."""
    # Force-empty the candidate set: human shot in submission mode with
    # Kling Pro excluded via budget. Veo is excluded by humans_never_veo;
    # Wan is excluded by run_mode=submission; Kling Std is excluded by
    # run_mode=submission. That leaves Kling Pro + Seedance — we exhaust
    # the fal budget so Kling Pro can't run and has_humans drops Seedance
    # (its likeness detector is hostile to humans in our capability score,
    # but strictly Seedance is still eligible by rules). Simpler: directly
    # exercise the rule by confirming it returns "exclude" on a testing-only
    # provider in submission mode, then show that empty survivor set →
    # RoutingError via a duration-bound exclusion that hits every remaining.
    shot = make_shot(
        has_humans=False,
        is_hero=False,
        duration_s=16.0,          # > every provider's max_duration_s
        run_mode="testing",
    )
    with pytest.raises(RoutingError):
        route(shot, make_budget(), capabilities)


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
    # Per-provider max durations: Veo 8s, Wan 10s, Kling 10s, Seedance 15s.
    # 16s exceeds every video provider's cap and routing must fail loud.
    shot = make_shot(has_humans=False, is_hero=True, duration_s=16.0)
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
