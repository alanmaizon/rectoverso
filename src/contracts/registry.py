"""Registry mapping (agent, ctx) -> list of contract checks to run.

See docs/contracts.md § Registry integration for the table this encodes.

The registry is the only place that decides which contracts apply to a given
dispatch. Adding a contract means adding a row here AND a test. Changes to this
table are changes to the enforcement surface and require review.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .types import ContractName, Violation

# A check function takes (manifest, shot_id, ctx) and returns a list of violations.
# shot_id is None for film-level checks.
CheckFn = Callable[[Mapping[str, Any], str | None, Mapping[str, Any]], list[Violation]]


# Populated by individual contract modules at import time via register().
_CHECKS: dict[ContractName, CheckFn] = {}


def register(name: ContractName, fn: CheckFn) -> None:
    """Register a contract check. Called once per contract module at import."""
    if name in _CHECKS:
        raise RuntimeError(f"Contract {name.value} already registered")
    _CHECKS[name] = fn


def get(name: ContractName) -> CheckFn:
    if name not in _CHECKS:
        raise RuntimeError(f"Contract {name.value} not registered; import its module")
    return _CHECKS[name]


def contracts_for_dispatch(
    agent: str, ctx: Mapping[str, Any]
) -> list[ContractName]:
    """Which contracts apply to a (agent, ctx) dispatch.

    Mirrors docs/contracts.md § Registry integration table.
    """
    revision = bool(ctx.get("revision", False))
    creative_driven = bool(ctx.get("creative_driven", False))

    if agent == "prompt_smith":
        out = []
        if revision:
            out.append(ContractName.SHOT_JUDGE_TO_PROMPT_SMITH)
        if creative_driven:
            out.append(ContractName.CD_TO_PROMPT_SMITH)
        return out

    if agent == "editor_agent":
        return [
            ContractName.AUDIO_TO_EDITOR,
            ContractName.CD_EDITOR_AUTHORITY,
        ]

    if agent == "creative_director":
        return [ContractName.CD_READS_APPROVED_JUDGE_FEEDBACK]

    if agent == "renderer":
        if creative_driven:
            return [ContractName.CD_TO_PROMPT_SMITH]
        return []

    # shot_judge, audio_agent, screenwriter: no pair-contract preconditions.
    return []
