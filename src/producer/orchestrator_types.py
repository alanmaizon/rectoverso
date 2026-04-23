"""Typed state carriers for the FilmOrchestrator.

Small frozen dataclasses. Kept separate from orchestrator.py so tests and CLI
surfaces can import the types without pulling in the runtime subprocess
machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetryPolicy:
    """Per-shot retry configuration.

    Settings the orchestrator reads on every shot:
        max_attempts_per_shot    — hard cap on (render + judge) iterations.
                                   At this count, next rejected attempt becomes
                                   escalated/max_attempts_exhausted.
        escalate_below_score     — floor for below_threshold escalation.
                                   Matches shot_judge.ESCALATE_THRESHOLD; kept
                                   here so tests can override without touching
                                   the judge's internals.
        stop_film_on_budget_halt — when True, a budget_halt on shot N aborts
                                   shots N+1..8 entirely. When False, the
                                   orchestrator marks the halted shot
                                   escalated and continues with cheaper
                                   providers on the rest (not implemented
                                   for v1 — always True).
    """

    max_attempts_per_shot: int = 3
    escalate_below_score: float = 0.40
    stop_film_on_budget_halt: bool = True


@dataclass(frozen=True)
class ShotSummary:
    """Per-shot row in the FilmResult. One of these for every shot the
    orchestrator touched (approved, rejected-then-retried, escalated, or
    budget-halted before starting).

    Audio fields are tracked independently from the render/judge pipeline —
    a shot can be final_status=approved but audio_status=failed and still
    ship (Editor layers silence). audio failures do NOT escalate the shot.
    """

    shot_id: str
    final_status: str   # "approved" | "escalated" | "budget_halted" | "failed"
    attempts: int
    best_judge_score: float | None
    final_render_path: str | None
    total_cost_usd: float
    escalation_reason: str | None = None   # populated when final_status == "escalated"
    latency_s: float = 0.0
    # Audio phase — independent of final_status. Values: pending | ok |
    # partial | failed | skipped. See shot.audio_status in the schema.
    audio_status: str = "pending"
    audio_cues_total: int = 0
    audio_cues_ok: int = 0
    audio_credits_used: int = 0
    # Normalize phase — post-approval pre-pass that homogenizes the render
    # for Hyperframes. Presence of normalized_render_path indicates success;
    # None means either didn't run (non-approved shot) or failed
    # (approved shot stays approved, Editor falls back to raw render).
    normalized_render_path: str | None = None


@dataclass(frozen=True)
class FilmResult:
    """Returned by FilmOrchestrator.run(). Read by film_cmd.py to render
    the CLI summary and set the exit code."""

    project_id: str
    shot_count: int
    approved_count: int
    escalated_count: int
    budget_halted_count: int
    failed_count: int
    total_spent_usd: float
    total_latency_s: float
    manifest_path: str
    shots: tuple[ShotSummary, ...] = field(default_factory=tuple)
    # Grouped escalation counts so the CLI summary distinguishes the three
    # human-review categories (see shot_judge.md § Decision thresholds).
    escalations_by_reason: dict[str, int] = field(default_factory=dict)
    # Audio phase rollup (orthogonal to shot final_status).
    audio_status_breakdown: dict[str, int] = field(default_factory=dict)
    total_audio_credits_used: int = 0
    # Set when the film halted mid-run (budget, dispatch error, etc.)
    halted_reason: str | None = None

    @property
    def all_landed(self) -> bool:
        """Every shot resolved to approved or escalated — no budget halts
        or hard failures. A 'clean' film even if some shots are in the
        human-review pile."""
        return self.budget_halted_count == 0 and self.failed_count == 0
