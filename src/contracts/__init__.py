"""Producer-side pair-contract enforcement.

Public API:

    from src.contracts import validate_before_dispatch, ContractViolation, Violation

    violations = validate_before_dispatch(
        agent="editor_agent",
        shot_id=None,
        manifest=manifest,
        ctx={},
    )

`validate_before_dispatch` returns a list of non-blocking (warn) violations;
blocking violations raise `ContractViolation` instead. The Producer logs warns
to `history[]` and continues; blocks halt the dispatch.

See docs/contracts.md for the contract spec. Every registered contract lives
in a module under this package and registers itself at import time.

Producer responsibilities around each call (contracts are event-free by design):

    # 1. Write the dispatch intent event BEFORE validation. Events are truth;
    #    the manifest is a projection.
    event_id = events.write(kind="dispatch_intent", agent=..., shot_id=..., ctx=...)
    try:
        warns = validate_before_dispatch(agent, shot_id, manifest, ctx)
    except ContractViolation as e:
        events.write(kind="contract_block", ref=event_id, violations=e.violations)
        raise
    for v in warns:
        producer.log_history(shot_id=v.shot_id, event="contract_warn", detail=v.reason)
    # 2. Proceed with the dispatch.
    events.write(kind="dispatch_start", ref=event_id)
    ...

Contracts do no I/O. A fresh harness can re-run `validate_before_dispatch`
against a recovered manifest and reach the same verdict — this is the
"session-as-durable-truth" discipline from scaling_managed_agents.md applied locally.
"""

from __future__ import annotations

from typing import Any, Mapping

from .registry import contracts_for_dispatch, get
from .types import (
    ContractName,
    ContractViolation,
    DispatchContext,
    Severity,
    Violation,
)

# Import modules so they register with the registry.
# Order is irrelevant; each contract owns one ContractName.
from . import audio_to_editor  # noqa: F401
from . import cd_editor_authority  # noqa: F401
from . import cd_reads_approved_judge_feedback  # noqa: F401
from . import cd_to_prompt_smith  # noqa: F401
from . import normalize_to_editor  # noqa: F401
from . import shot_judge_to_prompt_smith  # noqa: F401


def validate_before_dispatch(
    agent: str,
    shot_id: str | None,
    manifest: Mapping[str, Any],
    ctx: Mapping[str, Any] | None = None,
) -> list[Violation]:
    """Run all contracts applicable to (agent, ctx); raise on any blocking violation.

    Returns the list of warn-severity violations (empty if clean). Callers log
    these to history[] and proceed. Blocking violations raise ContractViolation;
    callers must not catch-and-continue.
    """
    ctx = dict(ctx or {})
    checks = contracts_for_dispatch(agent, ctx)
    all_violations: list[Violation] = []
    for name in checks:
        fn = get(name)
        all_violations.extend(fn(manifest, shot_id, ctx))

    blocking = [v for v in all_violations if v.severity == Severity.BLOCK]
    if blocking:
        raise ContractViolation(blocking)
    return [v for v in all_violations if v.severity == Severity.WARN]


__all__ = [
    "ContractName",
    "ContractViolation",
    "DispatchContext",
    "Severity",
    "Violation",
    "validate_before_dispatch",
]
