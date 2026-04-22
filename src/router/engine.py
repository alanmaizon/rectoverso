"""Router engine — loads capabilities.yaml, applies hard rules, scores, picks.

Decision pipeline (see CLAUDE.md § Provider priority):
  1. Filter by modality (audio shots only consider audio providers, etc.).
  2. Apply hard rules: EXCLUDE wins over DEPRIORITIZE.
  3. Score survivors on (capability_match, cost, prior_failures, tier_preference).
  4. Break ties deterministically by provider_id.

Every hard rule is a named callable in HARD_RULES — tests/router/ exercises
each one in isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

from .types import (
    BudgetState,
    Capabilities,
    ProviderChoice,
    RoutingError,
    ShotSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAPABILITIES_PATH = REPO_ROOT / "router" / "capabilities.yaml"

# Score multiplier applied when a DEPRIORITIZE rule fires. Heavy deprioritization
# (e.g. specialty provider on non-hero shot) stacks below regular.
DEPRIORITIZE_FACTOR = 0.4
DEPRIORITIZE_HEAVY_FACTOR = 0.05
PRIOR_FAILURE_FACTOR = 0.5


# --- Capability loading ---------------------------------------------------


def load_capabilities(path: str | Path | None = None) -> Capabilities:
    """Parse router/capabilities.yaml."""
    p = Path(path) if path is not None else DEFAULT_CAPABILITIES_PATH
    raw = yaml.safe_load(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"capabilities.yaml root must be a mapping, got {type(raw)}")

    providers = raw.get("providers", {})
    tiers_raw = raw.get("tiers", {})
    tiers = {name: tuple(members or ()) for name, members in tiers_raw.items()}
    weights = raw.get("decision_weights", {})
    hard_rules = tuple(raw.get("hard_rules", ()) or ())

    if not providers:
        raise ValueError("capabilities.yaml has no providers")

    return Capabilities(
        raw=raw,
        providers=providers,
        tiers=tiers,
        decision_weights=weights,
        hard_rules=hard_rules,
    )


# --- Hard rules -----------------------------------------------------------
#
# Each rule takes (shot, budget, provider_id, provider_def) and returns:
#   "exclude"              → remove this provider from consideration
#   "deprioritize"         → multiply score by DEPRIORITIZE_FACTOR
#   "deprioritize_heavy"   → multiply score by DEPRIORITIZE_HEAVY_FACTOR
#   None                   → rule doesn't apply
#
# Rule IDs match capabilities.yaml `hard_rules[].id` for traceability.

RuleOutcome = str | None  # "exclude" | "deprioritize" | "deprioritize_heavy" | None
HardRule = Callable[[ShotSpec, BudgetState, str, Mapping[str, Any]], RuleOutcome]


def _rule_humans_never_veo(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    if shot.has_humans and provider_id == "vertex_veo_3_1_fast":
        return "exclude"
    return None


def _rule_veo_spend_cap(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    if provider_id != "vertex_veo_3_1_fast":
        return None
    cap = provider.get("spend_cap_usd")
    if cap is None:
        return None
    already_spent = budget.by_provider.get(provider_id, 0.0)
    est_cost = _estimate_cost(shot, provider)
    # Exclude if we can't even fit one more call under the cap.
    if already_spent + est_cost > cap:
        return "exclude"
    return None


def _rule_alibaba_quota_exhausted(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    if provider.get("access") == "alibaba_cloud" and budget.alibaba_quota_remaining <= 0:
        return "exclude"
    return None


def _rule_elevenlabs_credits_exhausted(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    if provider_id != "elevenlabs":
        return None
    if budget.elevenlabs_credits_remaining < shot.estimated_credit_cost:
        return "exclude"
    return None


def _rule_wan_turbo_for_iteration_only(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    # First attempt on a non-hero shot → deprioritize Turbo so Plus wins ties.
    if (
        provider_id == "alibaba_wan_2_7_turbo"
        and len(shot.prior_failures) == 0
        and not shot.is_hero
    ):
        return "deprioritize"
    return None


def _rule_duration_bound(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    max_dur = provider.get("max_duration_s")
    if max_dur is None:
        return None
    # Audio (elevenlabs) has no max_duration_s on capabilities.yaml, so None is fine.
    if shot.duration_s > float(max_dur):
        return "exclude"
    return None


def _rule_prior_failure_penalty(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    # Score multiplier is applied separately (see _apply_prior_failure_penalty);
    # this rule exists in the registry for traceability and is a no-op here.
    return None


def _rule_global_budget_cap(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    est_cost = _estimate_cost(shot, provider)
    if budget.spent_usd + est_cost > budget.cap_usd:
        return "exclude"
    return None


def _rule_specialty_reserved_for_heroes(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    if provider.get("tier") == "specialty" and not shot.is_hero:
        return "deprioritize_heavy"
    return None


def _rule_end_frame_requires_capable_provider(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    if shot.has_end_frame and not provider.get("supports_first_last_frame", False):
        return "exclude"
    return None


def _rule_subject_refs_fit_capacity(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    cap = provider.get("max_reference_images", 0) or 0
    if shot.reference_subject_count > int(cap):
        return "exclude"
    return None


def _rule_prefer_kling_pro_when_refs_or_end_frame(
    shot: ShotSpec, budget: BudgetState, provider_id: str, provider: Mapping[str, Any]
) -> RuleOutcome:
    if provider_id != "fal_kling_2_1_standard":
        return None
    if shot.reference_subject_count > 1 or shot.has_end_frame:
        return "deprioritize"
    return None


HARD_RULES: dict[str, HardRule] = {
    "humans_never_veo": _rule_humans_never_veo,
    "veo_spend_cap": _rule_veo_spend_cap,
    "alibaba_quota_exhausted": _rule_alibaba_quota_exhausted,
    "elevenlabs_credits_exhausted": _rule_elevenlabs_credits_exhausted,
    "wan_turbo_for_iteration_only": _rule_wan_turbo_for_iteration_only,
    "duration_bound": _rule_duration_bound,
    "prior_failure_penalty": _rule_prior_failure_penalty,
    "global_budget_cap": _rule_global_budget_cap,
    "specialty_reserved_for_heroes": _rule_specialty_reserved_for_heroes,
    "end_frame_requires_capable_provider": _rule_end_frame_requires_capable_provider,
    "subject_refs_fit_capacity": _rule_subject_refs_fit_capacity,
    "prefer_kling_pro_when_refs_or_end_frame": _rule_prefer_kling_pro_when_refs_or_end_frame,
}


# --- Cost estimation ------------------------------------------------------


def _estimate_cost(shot: ShotSpec, provider: Mapping[str, Any]) -> float:
    """Estimate USD cost of rendering `shot` with `provider`.

    Kling providers carry a base_cost_5s + per-additional-second rate.
    Quota-metered providers (Wan, ElevenLabs) report $0.
    """
    if provider.get("quota_metered"):
        return 0.0
    base = float(provider.get("base_cost_5s", 0.0))
    cps = float(provider.get("cost_per_second_usd", 0.0))
    if base > 0.0:
        extra_s = max(0.0, shot.duration_s - 5.0)
        return base + extra_s * cps
    return shot.duration_s * cps


# --- Scoring --------------------------------------------------------------


def _capability_match(shot: ShotSpec, provider: Mapping[str, Any]) -> float:
    """Score how well the provider's capabilities match the shot."""
    caps = provider.get("capabilities", {}) or {}
    # Primary axis: human vs. non-human scenes.
    if shot.has_humans:
        base = float(caps.get("human_scenes", 0.0))
    else:
        base = float(caps.get("non_human_scenes", 0.0))
    # Secondary axis: motion fit.
    motion_key = {
        "high": "high_motion",
        "medium": None,
        "low": "low_motion",
    }.get(shot.motion_level)
    if motion_key:
        base = 0.6 * base + 0.4 * float(caps.get(motion_key, 0.5))
    return base


def _cost_score(est_cost: float, cap_usd: float) -> float:
    """Cheaper is better. Normalized to [0, 1]; $0 → 1.0."""
    if cap_usd <= 0:
        return 1.0
    # Linear: $0 → 1.0, $cap → 0.0, beyond-cap clamped at 0.
    return max(0.0, 1.0 - (est_cost / cap_usd))


def _tier_preference_score(provider: Mapping[str, Any], shot: ShotSpec) -> float:
    tier = provider.get("tier")
    if tier == "workhorse":
        return 1.0
    if tier == "specialty":
        return 1.0 if shot.is_hero else 0.3
    return 0.5


def _prior_failure_multiplier(shot: ShotSpec, provider_id: str) -> float:
    """Halve the score for each prior failure on this provider (compounding)."""
    failures = sum(1 for f in shot.prior_failures if f.provider == provider_id)
    if failures == 0:
        return 1.0
    return PRIOR_FAILURE_FACTOR ** failures


# --- Public API -----------------------------------------------------------


def route(
    shot: ShotSpec,
    budget: BudgetState,
    capabilities: Capabilities,
) -> ProviderChoice:
    """Pick a provider for `shot` under `budget`.

    Raises RoutingError if no provider survives hard-rule filtering.
    """
    exclusions: dict[str, str] = {}
    survivors: list[tuple[str, Mapping[str, Any], float, list[str]]] = []

    weights = capabilities.decision_weights
    w_cap = float(weights.get("capability_match", 0.5))
    w_cost = float(weights.get("cost", 0.2))
    w_fail = float(weights.get("prior_failures", 0.2))
    w_tier = float(weights.get("tier_preference", 0.1))

    for provider_id, provider in capabilities.providers.items():
        if provider.get("modality") != shot.modality:
            exclusions[provider_id] = f"modality mismatch ({provider.get('modality')})"
            continue

        excluded_reason: str | None = None
        deprio_rules: list[str] = []
        heavy_deprio = False
        regular_deprio = False

        for rule_id, fn in HARD_RULES.items():
            outcome = fn(shot, budget, provider_id, provider)
            if outcome == "exclude":
                excluded_reason = rule_id
                break
            if outcome == "deprioritize":
                regular_deprio = True
                deprio_rules.append(rule_id)
            elif outcome == "deprioritize_heavy":
                heavy_deprio = True
                deprio_rules.append(rule_id)

        if excluded_reason is not None:
            exclusions[provider_id] = excluded_reason
            continue

        # Score
        est_cost = _estimate_cost(shot, provider)
        score = (
            w_cap * _capability_match(shot, provider)
            + w_cost * _cost_score(est_cost, budget.cap_usd)
            + w_fail * _prior_failure_multiplier(shot, provider_id)
            + w_tier * _tier_preference_score(provider, shot)
        )
        score *= _prior_failure_multiplier(shot, provider_id)
        if heavy_deprio:
            score *= DEPRIORITIZE_HEAVY_FACTOR
        elif regular_deprio:
            score *= DEPRIORITIZE_FACTOR

        survivors.append((provider_id, provider, score, deprio_rules))

    if not survivors:
        raise RoutingError(
            f"No provider can satisfy shot {shot.shot_id!r}",
            exclusions=exclusions,
        )

    # Sort by score desc, provider_id asc for deterministic tie-break.
    survivors.sort(key=lambda t: (-t[2], t[0]))
    winner_id, winner, winner_score, winner_deprio = survivors[0]

    rationale = _build_rationale(shot, winner_id, winner, winner_score, winner_deprio)
    alternates = tuple(pid for pid, _, _, _ in survivors[1:4])

    return ProviderChoice(
        provider_id=winner_id,
        model_id=str(winner.get("model_id", winner_id)),
        estimated_cost_usd=_estimate_cost(shot, winner),
        rationale=rationale,
        alternates=alternates,
    )


def _build_rationale(
    shot: ShotSpec,
    provider_id: str,
    provider: Mapping[str, Any],
    score: float,
    deprio_rules: Iterable[str],
) -> str:
    bits = [
        f"{provider_id} (tier={provider.get('tier')}, score={score:.3f})",
        f"shot has_humans={shot.has_humans} is_hero={shot.is_hero} motion={shot.motion_level}",
    ]
    deprio_list = list(deprio_rules)
    if deprio_list:
        bits.append("deprioritized by: " + ", ".join(deprio_list))
    return "; ".join(bits)
