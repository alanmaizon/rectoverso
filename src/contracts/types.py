"""Contract data types — frozen, no deps.

See docs/contracts.md for the motivation and the full registry.

A Contract in this package is a pure function over a manifest (plain dict)
that returns zero or more Violations. Contracts never mutate. Block-severity
violations halt the Producer's dispatch; warn-severity violations are logged
to history but do not halt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, TypedDict


class DispatchContext(TypedDict, total=False):
    """Context flags the Producer passes to validate_before_dispatch.

    Every field is optional (total=False). Contracts check for presence rather
    than expect all keys. Adding a new flag means: (1) add it here with a
    docstring-style comment, (2) update registry.contracts_for_dispatch if the
    flag changes routing, (3) document in docs/contracts.md § Registry integration.

    Runtime is still a plain dict; this TypedDict exists for static checkers
    and discoverability. Permissive at the boundary (Mapping[str, Any]), typed
    at the contract layer.
    """

    # PromptSmith: True when this call is a revision of an earlier attempt.
    # Triggers Contract 2 (shot_judge_to_prompt_smith). Registry: prompt_smith.
    revision: bool

    # PromptSmith or Renderer: True when the re-render is motivated by a
    # creative_director suggestion (not a technical Judge rejection).
    # Triggers Contract 3 (cd_to_prompt_smith). Registry: prompt_smith | renderer.
    creative_driven: bool

    # Editor: the priority at which an Editor-authored creative_feedback entry
    # is being acted upon. Compared against unaddressed CD feedback on the same
    # shot (Contract 5, shot-level). One of "critical" | "high" | "medium" | "low".
    editor_priority: str

    # Editor: shots the caller is explicitly asserting are silent (no dialogue
    # expected). Used by Contract 1 in strict-silence mode.
    silent_shots: list[str]

    # Editor: when True, a shot with zero dialogue entries must have either a
    # shot_silent history event or appear in silent_shots. Default False =
    # absence of dialogue is treated as implicit silence (Contract 1).
    require_silent_marker: bool


class ContractName(str, Enum):
    AUDIO_TO_EDITOR = "audio_to_editor"
    SHOT_JUDGE_TO_PROMPT_SMITH = "shot_judge_to_prompt_smith"
    CD_TO_PROMPT_SMITH = "cd_to_prompt_smith"
    CD_READS_APPROVED_JUDGE_FEEDBACK = "cd_reads_approved_judge_feedback"
    CD_EDITOR_AUTHORITY = "cd_editor_authority"
    NORMALIZE_TO_EDITOR = "normalize_to_editor"


class Severity(str, Enum):
    BLOCK = "block"
    WARN = "warn"


@dataclass(frozen=True)
class Violation:
    """One precondition failure or sanitization finding.

    block -> dispatch must not proceed.
    warn  -> dispatch proceeds; Producer writes the reason to history[].
    """

    contract: ContractName
    severity: Severity
    reason: str
    shot_id: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


class ContractViolation(RuntimeError):
    """Raised by validate_before_dispatch when any violation has severity=block.

    Carries the list of blocking violations. Non-blocking violations are returned
    separately by validate_before_dispatch and never raise.
    """

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        summary = "; ".join(
            f"{v.contract.value}[{v.shot_id or 'film'}]: {v.reason}" for v in violations
        )
        super().__init__(f"Contract violations blocked dispatch: {summary}")
