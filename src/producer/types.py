"""Producer runtime data types — frozen, no deps beyond stdlib.

The runtime is deliberately thin:
    dispatch(agent, shot_id, manifest, ctx, tool, events) -> DispatchResult

The `Tool` Protocol is the stable adapter interface between the Producer and
whatever is on the other end — a Managed Agent session, a subprocess-wrapped
Messages API call, or a deterministic worker. Per scaling_managed_agents.md § The
Harness Leaves the Container, the Producer doesn't care which; it only speaks
`execute(name, input) -> output`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from src.contracts import Violation


@runtime_checkable
class Tool(Protocol):
    """Adapter to a specialist agent (Managed or Messages) or a worker.

    Implementations are injected into `dispatch`. Tests inject fakes; production
    code injects real adapters. The Protocol itself is deliberately minimal —
    pass a shot_id (None for film-level dispatches) and an arbitrary payload
    dict; receive an arbitrary result dict. Shape of payload/result is agreed
    between Producer and the specific tool, not encoded here.

    `name` must match an agent name known to `src.contracts.registry.contracts_for_dispatch`.
    """

    name: str

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class DispatchResult:
    """Successful dispatch outcome. Failures raise — they don't return."""

    agent: str
    shot_id: str | None
    result: Mapping[str, Any]
    warns: tuple[Violation, ...]
    intent_event_id: int
    result_event_id: int


class DispatchFailure(RuntimeError):
    """Raised when a tool raises during dispatch. The failure event is already
    written to the event log before this is raised — the caller decides whether
    to retry, reroute, or escalate.

    NOT a frozen dataclass: Python's exception propagation sets `__traceback__`
    on the raised instance, which a frozen dataclass rejects. Plain class with
    keyword-only __init__ keeps the field contract explicit.
    """

    def __init__(
        self,
        *,
        agent: str,
        shot_id: str | None,
        intent_event_id: int,
        failure_event_id: int,
        cause: BaseException,
    ) -> None:
        self.agent = agent
        self.shot_id = shot_id
        self.intent_event_id = intent_event_id
        self.failure_event_id = failure_event_id
        self.cause = cause
        who = f"{agent}[{shot_id or 'film'}]"
        super().__init__(
            f"dispatch_failure {who}: {cause!r} "
            f"(events {intent_event_id}->{failure_event_id})"
        )
