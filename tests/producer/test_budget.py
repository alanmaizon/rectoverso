"""Unit tests for src.producer.budget — pure-function budget enforcement.

Intent coverage:   each rule in the module docstring has at least one test
Architecture:      pure functions over the manifest dict; no I/O
Edge cases:        missing budget, zero cap, float drift, negative spend refused
"""

from __future__ import annotations

import pytest

from src.producer.budget import (
    BudgetCheck,
    BudgetExceeded,
    DEFAULT_VEO_PROJECT_CAP_USD,
    check_before_render,
    record_spend,
)


def _budget(**overrides):
    base = {
        "cap_usd": 151.0,
        "spent_usd": 0.0,
        "by_provider": {},
        "alibaba_quota_remaining": 50,
        "elevenlabs_credits_remaining": 117999,
    }
    base.update(overrides)
    return {"budget": base}


# -- rule 0: unconfigured cap ----------------------------------------------


def test_zero_cap_refuses_everything() -> None:
    m = _budget(cap_usd=0.0)
    check = check_before_render(m, provider_id="fal_kling_2_1_pro", estimated_cost_usd=1.50)
    assert check.allowed is False
    assert "not configured" in check.rationale


def test_negative_cap_refuses_everything() -> None:
    m = _budget(cap_usd=-5.0)
    check = check_before_render(m, provider_id="fal_kling_2_1_pro", estimated_cost_usd=1.00)
    assert check.allowed is False


# -- rule 1: hard USD cap --------------------------------------------------


def test_under_cap_allowed() -> None:
    m = _budget(cap_usd=151.0, spent_usd=100.0)
    check = check_before_render(m, provider_id="fal_kling_2_1_pro", estimated_cost_usd=1.50)
    assert check.allowed is True
    assert check.rationale == "ok"
    assert check.projected_spent_usd == 101.50
    assert round(check.cap_ratio, 3) == round(101.50 / 151.0, 3)


def test_hard_cap_breach_refused() -> None:
    m = _budget(cap_usd=151.0, spent_usd=150.80)
    check = check_before_render(m, provider_id="fal_kling_2_1_pro", estimated_cost_usd=1.00)
    assert check.allowed is False
    assert "USD cap breached" in check.rationale


def test_at_exact_cap_allowed() -> None:
    m = _budget(cap_usd=151.0, spent_usd=151.0)
    check = check_before_render(m, provider_id="wan", estimated_cost_usd=0.0)
    assert check.allowed is True  # zero-cost render at cap is fine (Wan)


# -- rule 2: Veo project sub-cap ------------------------------------------


def test_veo_sub_cap_breach_refused_even_when_global_under_cap() -> None:
    m = _budget(cap_usd=151.0, spent_usd=50.0, by_provider={"vertex_veo_3_1_fast": 14.80})
    check = check_before_render(
        m, provider_id="vertex_veo_3_1_fast", estimated_cost_usd=1.00
    )
    assert check.allowed is False
    assert "Veo project cap" in check.rationale
    assert check.veo_spent_projected_usd == 15.80


def test_veo_sub_cap_at_ceiling_allowed() -> None:
    m = _budget(cap_usd=151.0, spent_usd=50.0, by_provider={"vertex_veo_3_1_fast": 14.50})
    check = check_before_render(
        m, provider_id="vertex_veo_3_1_fast", estimated_cost_usd=0.50
    )
    assert check.allowed is True
    assert check.veo_spent_projected_usd == 15.0


def test_non_veo_provider_not_gated_by_veo_cap() -> None:
    m = _budget(cap_usd=151.0, spent_usd=50.0, by_provider={"vertex_veo_3_1_fast": 14.90})
    check = check_before_render(
        m, provider_id="fal_kling_2_1_pro", estimated_cost_usd=5.00
    )
    assert check.allowed is True
    # No veo projection reported when the provider isn't Veo.
    assert check.veo_spent_projected_usd is None


def test_veo_prefix_variants_matched() -> None:
    m = _budget(cap_usd=151.0, spent_usd=10.0, by_provider={"veo_experimental": 14.00})
    check = check_before_render(m, provider_id="veo_experimental", estimated_cost_usd=2.00)
    assert check.allowed is False
    assert check.veo_spent_projected_usd == 16.00


# -- rule 3: soft 95% cap for creative-driven re-renders -------------------


def test_creative_driven_soft_cap_refused_above_95pct() -> None:
    m = _budget(cap_usd=100.0, spent_usd=90.0)
    check = check_before_render(
        m,
        provider_id="fal_kling_2_1_pro",
        estimated_cost_usd=6.0,
        creative_driven=True,
    )
    assert check.allowed is False
    assert "95%" in check.rationale or "cap ratio" in check.rationale


def test_creative_driven_soft_cap_ok_below_95pct() -> None:
    m = _budget(cap_usd=100.0, spent_usd=80.0)
    check = check_before_render(
        m,
        provider_id="fal_kling_2_1_pro",
        estimated_cost_usd=10.0,
        creative_driven=True,
    )
    assert check.allowed is True


def test_technical_rerender_not_gated_by_soft_cap() -> None:
    """Technical re-render at 93% is fine; same scenario for creative fails."""
    m = _budget(cap_usd=100.0, spent_usd=90.0)
    tech = check_before_render(
        m, provider_id="fal_kling_2_1_pro", estimated_cost_usd=3.0
    )
    assert tech.allowed is True
    creative = check_before_render(
        m,
        provider_id="fal_kling_2_1_pro",
        estimated_cost_usd=6.0,
        creative_driven=True,
    )
    assert creative.allowed is False


def test_soft_cap_custom_ratio() -> None:
    m = _budget(cap_usd=100.0, spent_usd=80.0)
    # Tighter 70% soft cap — 85 projected exceeds 70.
    check = check_before_render(
        m,
        provider_id="fal_kling_2_1_pro",
        estimated_cost_usd=5.0,
        creative_driven=True,
        cap_ratio_soft=0.70,
    )
    assert check.allowed is False


# -- rule 4: Alibaba Wan quota --------------------------------------------


def test_alibaba_quota_exhaustion_refused() -> None:
    m = _budget(alibaba_quota_remaining=5)
    check = check_before_render(
        m,
        provider_id="alibaba_wan_2_7_plus",
        estimated_cost_usd=0.0,
        estimated_quota_cost=10,
    )
    assert check.allowed is False
    assert "alibaba quota" in check.rationale
    assert check.alibaba_quota_projected == -5


def test_alibaba_quota_sufficient_allowed() -> None:
    m = _budget(alibaba_quota_remaining=20)
    check = check_before_render(
        m,
        provider_id="alibaba_wan_2_7_plus",
        estimated_cost_usd=0.0,
        estimated_quota_cost=5,
    )
    assert check.allowed is True
    assert check.alibaba_quota_projected == 15


def test_alibaba_quota_not_checked_for_other_providers() -> None:
    m = _budget(alibaba_quota_remaining=0)  # would fail if applied
    check = check_before_render(
        m,
        provider_id="fal_kling_2_1_pro",
        estimated_cost_usd=1.50,
        estimated_quota_cost=1,  # caller passed it but not an Alibaba provider
    )
    assert check.allowed is True
    assert check.alibaba_quota_projected is None


# -- rule 5: ElevenLabs credits -------------------------------------------


def test_elevenlabs_credit_exhaustion_refused() -> None:
    m = _budget(elevenlabs_credits_remaining=100)
    check = check_before_render(
        m,
        provider_id="elevenlabs_multilingual_v2",
        estimated_cost_usd=0.0,
        estimated_credit_cost=500,
    )
    assert check.allowed is False
    assert "elevenlabs credits" in check.rationale
    assert check.elevenlabs_credits_projected == -400


def test_elevenlabs_credits_sufficient_allowed() -> None:
    m = _budget(elevenlabs_credits_remaining=10000)
    check = check_before_render(
        m,
        provider_id="elevenlabs_multilingual_v2",
        estimated_cost_usd=0.0,
        estimated_credit_cost=5000,
    )
    assert check.allowed is True
    assert check.elevenlabs_credits_projected == 5000


# -- missing-fields resilience --------------------------------------------


def test_manifest_without_budget_refused_cleanly() -> None:
    check = check_before_render({}, provider_id="fal_kling_2_1_pro", estimated_cost_usd=1.0)
    assert check.allowed is False
    assert "not configured" in check.rationale


def test_missing_by_provider_defaults_to_empty() -> None:
    m = {"budget": {"cap_usd": 151.0, "spent_usd": 50.0}}
    check = check_before_render(m, provider_id="fal_kling_2_1_pro", estimated_cost_usd=1.0)
    assert check.allowed is True


# -- BudgetExceeded error type --------------------------------------------


def test_budget_exceeded_carries_check() -> None:
    check = BudgetCheck(
        allowed=False,
        provider_id="fal_kling_2_1_pro",
        estimated_cost_usd=5.0,
        projected_spent_usd=200.0,
        cap_usd=151.0,
        cap_ratio=1.32,
        rationale="USD cap breached",
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        raise BudgetExceeded(check)
    assert excinfo.value.check is check
    assert "USD cap breached" in str(excinfo.value)


# -- record_spend ---------------------------------------------------------


def test_record_spend_additive() -> None:
    m = _budget(spent_usd=50.0)
    record_spend(m, provider_id="fal_kling_2_1_pro", actual_cost_usd=1.42)
    assert m["budget"]["spent_usd"] == 51.42
    assert m["budget"]["by_provider"]["fal_kling_2_1_pro"] == 1.42


def test_record_spend_sum_invariant_preserved() -> None:
    m = _budget(
        spent_usd=0.0,
        by_provider={"fal_kling_2_1_pro": 10.0, "vertex_veo_3_1_fast": 5.0},
    )
    # Reset to known-good starting state (sum invariant already holds at start).
    m["budget"]["spent_usd"] = 15.0

    record_spend(m, provider_id="fal_kling_2_1_pro", actual_cost_usd=2.50)
    record_spend(m, provider_id="elevenlabs_multilingual_v2", actual_cost_usd=0.0, actual_credits=1000)

    by_sum = sum(m["budget"]["by_provider"].values())
    assert m["budget"]["spent_usd"] == round(by_sum, 6)


def test_record_spend_decrements_quota() -> None:
    m = _budget(alibaba_quota_remaining=50)
    record_spend(
        m,
        provider_id="alibaba_wan_2_7_plus",
        actual_cost_usd=0.0,
        actual_quota=3,
    )
    assert m["budget"]["alibaba_quota_remaining"] == 47
    # USD spent unchanged for a quota-metered provider.
    assert m["budget"]["spent_usd"] == 0.0


def test_record_spend_decrements_credits() -> None:
    m = _budget(elevenlabs_credits_remaining=117999)
    record_spend(
        m,
        provider_id="elevenlabs_multilingual_v2",
        actual_cost_usd=0.0,
        actual_credits=250,
    )
    assert m["budget"]["elevenlabs_credits_remaining"] == 117749


def test_record_spend_quota_never_negative() -> None:
    """Defensive: caller passes more than remaining, clamp to 0."""
    m = _budget(alibaba_quota_remaining=3)
    record_spend(m, provider_id="alibaba_wan_2_7_plus", actual_cost_usd=0.0, actual_quota=10)
    assert m["budget"]["alibaba_quota_remaining"] == 0


def test_record_spend_rejects_negative_inputs() -> None:
    m = _budget()
    with pytest.raises(ValueError):
        record_spend(m, provider_id="fal_kling_2_1_pro", actual_cost_usd=-1.0)
    with pytest.raises(ValueError):
        record_spend(m, provider_id="alibaba_wan_2_7_plus", actual_cost_usd=0.0, actual_quota=-1)


def test_record_spend_seeds_missing_budget() -> None:
    """An unseeded manifest should still accept spend records (caller bug to
    catch, but shouldn't crash the write)."""
    m: dict = {}
    record_spend(m, provider_id="fal_kling_2_1_pro", actual_cost_usd=1.50)
    assert m["budget"]["spent_usd"] == 1.50
    assert m["budget"]["by_provider"]["fal_kling_2_1_pro"] == 1.50


# -- detail payload sanity ------------------------------------------------


def test_check_detail_captures_context() -> None:
    m = _budget(spent_usd=10.0)
    check = check_before_render(
        m,
        provider_id="fal_kling_2_1_pro",
        estimated_cost_usd=1.50,
        estimated_quota_cost=5,
        estimated_credit_cost=200,
        creative_driven=True,
        cap_ratio_soft=0.80,
        veo_project_cap_usd=20.0,
    )
    assert check.detail["creative_driven"] is True
    assert check.detail["soft_cap_ratio"] == 0.80
    assert check.detail["veo_project_cap_usd"] == 20.0
    assert check.detail["estimated_quota_cost"] == 5
    assert check.detail["estimated_credit_cost"] == 200
