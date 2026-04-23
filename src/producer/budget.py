"""Budget enforcement — Producer invariant, not a pair contract.

Per [docs/contracts.md § Non-contracts], budget checks are the Producer's
invariant, not an agent-pair contract. They enforce the rules from CLAUDE.md
§ Budget and prompts/producer.md § Invariants (1) before any dispatch that
could spend USD or burn a quota counter.

Two functions, both pure over the manifest dict:

    check_before_render(manifest, *, provider_id, estimated_cost_usd, ...) -> BudgetCheck
    record_spend(manifest, *, provider_id, actual_cost_usd, ...) -> dict

Neither talks to the event log or the schema; callers compose them into the
`dispatch()` flow (or the CLI). The rationale strings are designed to land
verbatim in `history[]` entries and `events.db` `budget_block` payloads.

The rules enforced:

    1. Hard USD cap — never allow projected spend to breach `budget.cap_usd`.
    2. Veo sub-cap — a project-wide $15 ceiling on `vertex_veo*` providers.
    3. Soft 95% cap for creative-driven re-renders only (producer.md § Re-render
       decision rules step 2).
    4. Alibaba Wan quota — refuse when `alibaba_quota_remaining - estimated < 0`.
    5. ElevenLabs credits — refuse when `elevenlabs_credits_remaining - estimated < 0`.

Edge cases:
    - Missing `budget` dict or fields -> safe defaults (0 spend, 0 quota); this
      keeps the check conservative without crashing unseeded manifests.
    - Zero-cost providers (Wan, ElevenLabs) -> USD check passes; quota/credit
      check still runs.
    - `cap_usd <= 0` -> treat as "no cap configured" and refuse every render
      with a clear rationale rather than accidentally allow infinite spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


DEFAULT_SOFT_CAP_RATIO = 0.95      # producer.md § Re-render decision rules step 2
DEFAULT_VEO_PROJECT_CAP_USD = 15.0  # CLAUDE.md § Budget
VEO_PROVIDER_PREFIXES = ("vertex_veo", "veo_")

# Editor session cost estimate — conservative floor. Shape:
#   estimate = EDITOR_BASE_COST_USD + (EDITOR_PER_SHOT_USD * approved_shot_count)
# Ships deliberately-simple; will be tuned with a dialogue/SFX weight after
# the first live Editor run produces real spend data.
EDITOR_BASE_COST_USD = 8.0
EDITOR_PER_SHOT_USD = 0.5


@dataclass(frozen=True)
class BudgetCheck:
    """Result of a pre-dispatch budget projection.

    `allowed` is the single field callers should branch on. `rationale` is the
    human-readable explanation; `detail` carries structured fields for the
    event-log payload.
    """

    allowed: bool
    provider_id: str
    estimated_cost_usd: float
    projected_spent_usd: float
    cap_usd: float
    cap_ratio: float                         # projected / cap, for logging
    rationale: str
    alibaba_quota_projected: int | None = None
    elevenlabs_credits_projected: int | None = None
    veo_spent_projected_usd: float | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


class BudgetExceeded(RuntimeError):
    """Raised by callers that want hard-block semantics. The failing check is
    attached so the event log / history entry has the full context."""

    def __init__(self, check: BudgetCheck) -> None:
        self.check = check
        super().__init__(f"budget refused: {check.rationale}")


# ---------------------------------------------------------------------------
# Pre-dispatch projection
# ---------------------------------------------------------------------------


def check_before_render(
    manifest: Mapping[str, Any],
    *,
    provider_id: str,
    estimated_cost_usd: float = 0.0,
    estimated_quota_cost: int = 0,
    estimated_credit_cost: int = 0,
    creative_driven: bool = False,
    cap_ratio_soft: float = DEFAULT_SOFT_CAP_RATIO,
    veo_project_cap_usd: float = DEFAULT_VEO_PROJECT_CAP_USD,
) -> BudgetCheck:
    """Project whether a dispatch is affordable. Pure; does not mutate."""
    budget = manifest.get("budget", {}) or {}
    cap = float(budget.get("cap_usd", 0.0))
    spent = float(budget.get("spent_usd", 0.0))
    by_provider = budget.get("by_provider", {}) or {}

    projected = spent + float(estimated_cost_usd)
    cap_ratio = projected / cap if cap > 0 else float("inf")

    reasons: list[str] = []
    allowed = True

    # Rule 0 — a zero-or-negative cap means the manifest was never seeded with
    # a real budget. Safer to refuse than to accidentally authorize spending.
    if cap <= 0:
        allowed = False
        reasons.append(f"budget.cap_usd not configured (cap={cap})")

    # Rule 1 — hard USD cap
    elif projected > cap:
        allowed = False
        reasons.append(
            f"USD cap breached: projected ${projected:.4f} > cap ${cap:.2f}"
        )

    # Rule 2 — Veo sub-cap
    veo_projected: float | None = None
    if _is_veo(provider_id):
        veo_spent = float(_sum_matching(by_provider, VEO_PROVIDER_PREFIXES))
        veo_projected = veo_spent + float(estimated_cost_usd)
        if veo_projected > veo_project_cap_usd:
            allowed = False
            reasons.append(
                f"Veo project cap breached: projected ${veo_projected:.4f} "
                f"> cap ${veo_project_cap_usd:.2f}"
            )

    # Rule 3 — soft 95% cap for creative-driven re-renders only
    if allowed and creative_driven and cap > 0 and projected > cap * cap_ratio_soft:
        allowed = False
        reasons.append(
            f"creative-driven re-render would push spend past "
            f"{cap_ratio_soft * 100:.0f}% cap ratio "
            f"(projected ${projected:.4f} / cap ${cap:.2f})"
        )

    # Rule 4 — Alibaba Wan quota
    alibaba_projected: int | None = None
    if estimated_quota_cost > 0 and provider_id.startswith("alibaba_wan"):
        remaining = int(budget.get("alibaba_quota_remaining", 0))
        alibaba_projected = remaining - int(estimated_quota_cost)
        if alibaba_projected < 0:
            allowed = False
            reasons.append(
                f"alibaba quota exhausted: need {estimated_quota_cost}, "
                f"have {remaining}"
            )

    # Rule 5 — ElevenLabs credits
    elevenlabs_projected: int | None = None
    if estimated_credit_cost > 0 and "elevenlabs" in provider_id.lower():
        remaining = int(budget.get("elevenlabs_credits_remaining", 0))
        elevenlabs_projected = remaining - int(estimated_credit_cost)
        if elevenlabs_projected < 0:
            allowed = False
            reasons.append(
                f"elevenlabs credits exhausted: need {estimated_credit_cost}, "
                f"have {remaining}"
            )

    rationale = "ok" if allowed else "; ".join(reasons)

    return BudgetCheck(
        allowed=allowed,
        provider_id=provider_id,
        estimated_cost_usd=float(estimated_cost_usd),
        projected_spent_usd=round(projected, 6),
        cap_usd=cap,
        cap_ratio=round(cap_ratio, 6) if cap > 0 else float("inf"),
        rationale=rationale,
        alibaba_quota_projected=alibaba_projected,
        elevenlabs_credits_projected=elevenlabs_projected,
        veo_spent_projected_usd=(
            round(veo_projected, 6) if veo_projected is not None else None
        ),
        detail={
            "creative_driven": creative_driven,
            "soft_cap_ratio": cap_ratio_soft,
            "veo_project_cap_usd": veo_project_cap_usd,
            "estimated_quota_cost": estimated_quota_cost,
            "estimated_credit_cost": estimated_credit_cost,
        },
    )


# ---------------------------------------------------------------------------
# Post-dispatch accounting
# ---------------------------------------------------------------------------


def record_spend(
    manifest: dict[str, Any],
    *,
    provider_id: str,
    actual_cost_usd: float = 0.0,
    actual_quota: int = 0,
    actual_credits: int = 0,
) -> dict[str, Any]:
    """Apply a successful dispatch's actual cost to the manifest budget.

    Mutates `manifest["budget"]` in place and returns it. Callers then re-save
    the manifest via `save_manifest_atomic` under the usual transaction.

    Preserves the invariant `budget.spent_usd == sum(budget.by_provider.*)`.
    Refuses negative deltas (caller bug — actual costs are always >= 0).
    """
    if actual_cost_usd < 0 or actual_quota < 0 or actual_credits < 0:
        raise ValueError(
            f"actual spend must be non-negative "
            f"(usd={actual_cost_usd}, quota={actual_quota}, credits={actual_credits})"
        )

    budget = manifest.setdefault("budget", {})
    # Spent + by_provider — append to both; round to 6 decimals to avoid
    # float-accumulation drift in the sum invariant.
    by_provider = budget.setdefault("by_provider", {})
    by_provider[provider_id] = round(
        float(by_provider.get(provider_id, 0.0)) + float(actual_cost_usd), 6
    )
    budget["spent_usd"] = round(
        float(budget.get("spent_usd", 0.0)) + float(actual_cost_usd), 6
    )

    # Quota counters — never go below zero (defensive).
    if actual_quota > 0:
        budget["alibaba_quota_remaining"] = max(
            0, int(budget.get("alibaba_quota_remaining", 0)) - int(actual_quota)
        )
    if actual_credits > 0:
        budget["elevenlabs_credits_remaining"] = max(
            0, int(budget.get("elevenlabs_credits_remaining", 0)) - int(actual_credits)
        )

    return budget


# ---------------------------------------------------------------------------
# Editor dispatch budget gate
# ---------------------------------------------------------------------------


def estimate_editor_cost(manifest: Mapping[str, Any]) -> float:
    """Conservative floor estimate of the Editor Managed Agents session cost.

    Counts only approved shots — the Editor touches those (the pre-dispatch
    trigger already ensures every shot is approved, but this is pure, so
    it defensively filters). Shape:

        estimate = EDITOR_BASE_COST_USD + (EDITOR_PER_SHOT_USD * N_approved)

    A 10-shot film → $13. The intent is a pre-flight gate, not an accurate
    prediction — the real Anthropic-side cost is what lands in
    `dispatch_result.cost_usd` post-session.
    """
    shots = manifest.get("shots", []) or []
    n_approved = sum(1 for s in shots if s.get("status") == "approved")
    return round(EDITOR_BASE_COST_USD + EDITOR_PER_SHOT_USD * n_approved, 4)


def ensure_editor_estimate(manifest: dict[str, Any]) -> float:
    """Return `budget.editor_estimate_usd`, computing + caching it on first
    call. Subsequent calls reuse the cached value — cache-determinism
    discipline per CLAUDE.md § Prompt caching rationale (resume must not
    re-estimate to a different number).

    Mutates `manifest["budget"]`. Caller saves the manifest atomically.
    """
    budget = manifest.setdefault("budget", {})
    cached = budget.get("editor_estimate_usd")
    if cached is not None:
        return round(float(cached), 4)
    estimate = estimate_editor_cost(manifest)
    budget["editor_estimate_usd"] = estimate
    return estimate


def check_before_editor(manifest: Mapping[str, Any]) -> BudgetCheck:
    """Project whether the Editor dispatch fits within the USD cap.

    Unlike `check_before_render`, this is Anthropic-session scoped:
    no Veo sub-cap, no quota/credit counters, no creative-driven soft cap.
    Just hard cap vs. projected spend. The `provider_id` is a synthetic
    `"managed_agents_editor"` for attribution in events and accounting.

    Caller should ensure_editor_estimate() BEFORE calling this so the
    estimate is cached; this function does not mutate.
    """
    budget = manifest.get("budget", {}) or {}
    cap = float(budget.get("cap_usd", 0.0))
    spent = float(budget.get("spent_usd", 0.0))
    estimate = float(
        budget.get("editor_estimate_usd")
        if budget.get("editor_estimate_usd") is not None
        else estimate_editor_cost(manifest)
    )

    projected = spent + estimate
    cap_ratio = projected / cap if cap > 0 else float("inf")

    if cap <= 0:
        return BudgetCheck(
            allowed=False,
            provider_id="managed_agents_editor",
            estimated_cost_usd=estimate,
            projected_spent_usd=round(projected, 6),
            cap_usd=cap,
            cap_ratio=cap_ratio,
            rationale=f"budget.cap_usd not configured (cap={cap})",
        )

    if projected > cap:
        return BudgetCheck(
            allowed=False,
            provider_id="managed_agents_editor",
            estimated_cost_usd=estimate,
            projected_spent_usd=round(projected, 6),
            cap_usd=cap,
            cap_ratio=round(cap_ratio, 6),
            rationale=(
                f"Editor dispatch would breach USD cap: "
                f"spent ${spent:.4f} + estimate ${estimate:.4f} "
                f"= projected ${projected:.4f} > cap ${cap:.2f}"
            ),
        )

    return BudgetCheck(
        allowed=True,
        provider_id="managed_agents_editor",
        estimated_cost_usd=estimate,
        projected_spent_usd=round(projected, 6),
        cap_usd=cap,
        cap_ratio=round(cap_ratio, 6),
        rationale="ok",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_veo(provider_id: str) -> bool:
    return any(provider_id.startswith(p) for p in VEO_PROVIDER_PREFIXES)


def _sum_matching(by_provider: Mapping[str, Any], prefixes: tuple[str, ...]) -> float:
    total = 0.0
    for k, v in by_provider.items():
        if any(k.startswith(p) for p in prefixes):
            try:
                total += float(v)
            except (TypeError, ValueError):
                continue
    return total
