"""Tool dispatch wrapper — combines contracts, events, and the tool call.

`dispatch(agent, shot_id, manifest, ctx, tool, events)` is the only entry
point in the Producer runtime that invokes a specialist or worker. It is the
ONE place where the discipline from CLAUDE.md § Agent pair contracts and
docs/contracts.md § 6 lives:

    1. Write the dispatch intent event.
    2. Validate contracts (raises on block — after writing a block event).
    3. Log each warn to the event log.
    4. Call the tool. On exception, write a failure event and raise
       DispatchFailure (which wraps the cause and carries the event IDs).
    5. On success, write a dispatch_result event and return DispatchResult.

This module does NOT save the manifest. The caller composes save_manifest_atomic
after dispatch when the tool's result mutates manifest state. That split keeps
the dispatch pure w.r.t. disk — dispatch writes to events.db only, and the
caller decides when to project results into the manifest.

Intent:   what is the dispatch trying to do? → dispatch_intent event + contracts
Architecture: does the pair invariant hold?  → validate_before_dispatch
Edge cases:   silent-breakage failures        → block/warn Violations
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from src.contracts import (
    ContractViolation,
    validate_before_dispatch,
)

from .events import EventLog
from .types import DispatchFailure, DispatchResult, Tool


def dispatch(
    agent: str,
    shot_id: str | None,
    manifest: Mapping[str, Any],
    ctx: Mapping[str, Any],
    tool: Tool,
    events: EventLog,
) -> DispatchResult:
    """Validate, call, log. Returns DispatchResult on success; raises on error.

    Args:
        agent: canonical agent name; must match the contract registry.
        shot_id: target shot, or None for film-level dispatches.
        manifest: current manifest (read-only — this function does not mutate).
        ctx: DispatchContext-shaped dict (revision, creative_driven, etc.).
        tool: injected Tool adapter. Its `name` must equal `agent`.
        events: EventLog to write intent/result/failure/contract events to.

    Raises:
        ValueError: if tool.name != agent (caller bug).
        ContractViolation: when validate_before_dispatch finds any block. A
            `contract_block` event is written first; the exception carries the
            full violations list.
        DispatchFailure: when the tool raises. A `dispatch_failure` event is
            written first; the exception carries the cause and event IDs.
    """
    if tool.name != agent:
        raise ValueError(
            f"tool.name={tool.name!r} does not match agent={agent!r}; "
            "wrong adapter injected?"
        )

    ctx_dict = dict(ctx or {})

    # --- step 1: intent event -------------------------------------------
    intent_event_id = events.write(
        "dispatch_intent",
        agent=agent,
        shot_id=shot_id,
        payload={"ctx": ctx_dict},
    )

    # --- step 2: contracts ----------------------------------------------
    try:
        warns = validate_before_dispatch(agent, shot_id, manifest, ctx_dict)
    except ContractViolation as exc:
        events.write(
            "contract_block",
            agent=agent,
            shot_id=shot_id,
            ref_event_id=intent_event_id,
            payload={
                "violations": [_violation_to_dict(v) for v in exc.violations],
            },
        )
        raise

    # --- step 3: log warns ----------------------------------------------
    for w in warns:
        events.write(
            "contract_warn",
            agent=agent,
            shot_id=w.shot_id,
            ref_event_id=intent_event_id,
            payload=_violation_to_dict(w),
        )

    # --- step 4: call the tool ------------------------------------------
    try:
        result = tool(shot_id, ctx_dict)
    except BaseException as exc:
        failure_event_id = events.write(
            "dispatch_failure",
            agent=agent,
            shot_id=shot_id,
            ref_event_id=intent_event_id,
            payload={"error_type": type(exc).__name__, "error_str": str(exc)},
        )
        raise DispatchFailure(
            agent=agent,
            shot_id=shot_id,
            intent_event_id=intent_event_id,
            failure_event_id=failure_event_id,
            cause=exc,
        ) from exc

    # --- step 5: result event -------------------------------------------
    result_event_id = events.write(
        "dispatch_result",
        agent=agent,
        shot_id=shot_id,
        ref_event_id=intent_event_id,
        payload={"result": dict(result)},
    )

    return DispatchResult(
        agent=agent,
        shot_id=shot_id,
        result=dict(result),
        warns=tuple(warns),
        intent_event_id=intent_event_id,
        result_event_id=result_event_id,
    )


def _violation_to_dict(v: Any) -> dict[str, Any]:
    """Serialize a Violation for JSON payload storage."""
    # Violation is a frozen dataclass; asdict is faithful. We convert the
    # Enum fields to their string values so the payload round-trips cleanly
    # through json.dumps.
    d = asdict(v)
    if hasattr(v.contract, "value"):
        d["contract"] = v.contract.value
    if hasattr(v.severity, "value"):
        d["severity"] = v.severity.value
    return d
