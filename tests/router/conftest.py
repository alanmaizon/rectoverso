"""Fixtures for router tests.

The router reads its capability matrix from router/capabilities.yaml.
Tests load it once (session-scoped) and construct ShotSpec/BudgetState
objects per-test to exercise each hard rule in isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.router import (  # noqa: E402  (import after sys.path tweak)
    BudgetState,
    Capabilities,
    PriorFailure,
    ShotSpec,
    load_capabilities,
)


@pytest.fixture(scope="session")
def capabilities() -> Capabilities:
    return load_capabilities()


@pytest.fixture
def make_shot() -> Callable[..., ShotSpec]:
    def _factory(**overrides: Any) -> ShotSpec:
        defaults: dict[str, Any] = {
            "shot_id": "sh_001",
            "duration_s": 3.0,
            "has_humans": False,
            "is_hero": False,
            "motion_level": "low",
            "prior_failures": (),
            "reference_subject_count": 0,
            "has_end_frame": False,
            "modality": "video",
            "estimated_credit_cost": 0,
        }
        defaults.update(overrides)
        return ShotSpec(**defaults)

    return _factory


@pytest.fixture
def make_budget() -> Callable[..., BudgetState]:
    def _factory(**overrides: Any) -> BudgetState:
        defaults: dict[str, Any] = {
            "cap_usd": 151.0,
            "spent_usd": 0.0,
            "by_provider": {},
            "alibaba_quota_remaining": 72,
            "elevenlabs_credits_remaining": 117_999,
        }
        defaults.update(overrides)
        return BudgetState(**defaults)

    return _factory


@pytest.fixture
def failure() -> Callable[..., PriorFailure]:
    def _factory(provider: str, outcome: str = "failed") -> PriorFailure:
        return PriorFailure(provider=provider, outcome=outcome)

    return _factory
