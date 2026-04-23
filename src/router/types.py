"""Router data contracts — small, frozen, no deps.

These are the boundary objects between the Producer and the router.
The Producer builds a ShotSpec + BudgetState from the manifest; the
router returns a ProviderChoice that the Producer writes back under
shots[].routing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class PriorFailure:
    """One past attempt on this shot that didn't produce an approved render."""

    provider: str
    outcome: str  # "failed" | "rejected"


@dataclass(frozen=True)
class ShotSpec:
    """Minimal projection of a manifest shot for routing.

    Built by the Producer from shots[] + brief. The router reads only
    this — it has no view of the whole manifest.
    """

    shot_id: str
    duration_s: float
    has_humans: bool
    is_hero: bool
    motion_level: str  # "low" | "medium" | "high"
    prior_failures: tuple[PriorFailure, ...] = ()
    # Optional production hints that gate capability-specific rules.
    reference_subject_count: int = 0
    has_end_frame: bool = False
    modality: str = "video"  # "video" | "audio"
    # Audio-only: used by elevenlabs_credits_exhausted rule.
    estimated_credit_cost: int = 0
    # Run mode gate — set by the Producer based on whether this is a final
    # hackathon-submission run (premium US providers only) or a test/iteration
    # run (free-quota + cheap models). Providers self-declare which modes they
    # belong to via `run_modes` in capabilities.yaml. "testing" is the safe
    # default so internal iteration doesn't accidentally burn submission spend.
    run_mode: str = "testing"  # "submission" | "testing"


@dataclass(frozen=True)
class BudgetState:
    """Snapshot of budget counters at decision time."""

    cap_usd: float
    spent_usd: float
    by_provider: Mapping[str, float] = field(default_factory=dict)
    alibaba_quota_remaining: int = 0
    elevenlabs_credits_remaining: int = 0


@dataclass(frozen=True)
class ProviderChoice:
    """Router's decision. Producer writes this to shots[].routing."""

    provider_id: str
    model_id: str
    estimated_cost_usd: float
    rationale: str
    alternates: tuple[str, ...] = ()


@dataclass(frozen=True)
class Capabilities:
    """Parsed capabilities.yaml. Keep the raw dict around for rationale text."""

    raw: Mapping[str, Any]
    providers: Mapping[str, Mapping[str, Any]]
    tiers: Mapping[str, tuple[str, ...]]
    decision_weights: Mapping[str, float]
    hard_rules: tuple[Mapping[str, Any], ...]


class RoutingError(RuntimeError):
    """Raised when no provider can satisfy the ShotSpec under current budget."""

    def __init__(self, message: str, *, exclusions: Optional[Mapping[str, str]] = None) -> None:
        super().__init__(message)
        self.exclusions: Mapping[str, str] = dict(exclusions or {})
