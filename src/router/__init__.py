"""Deterministic provider router (Tier 4 worker).

Given a ShotSpec and a BudgetState, pick a ProviderChoice from the
capability matrix (router/capabilities.yaml), enforcing hard rules
before scoring.

Public API:
    load_capabilities(path=None) -> Capabilities
    route(shot, budget, capabilities, *, now=None) -> ProviderChoice

See CLAUDE.md § Provider priority for the contract.
"""

from .types import (
    BudgetState,
    Capabilities,
    PriorFailure,
    ProviderChoice,
    RoutingError,
    ShotSpec,
)
from .engine import load_capabilities, route

__all__ = [
    "BudgetState",
    "Capabilities",
    "PriorFailure",
    "ProviderChoice",
    "RoutingError",
    "ShotSpec",
    "load_capabilities",
    "route",
]
