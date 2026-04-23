"""Film-level compose state machine.

Schema defines the enum at the manifest root (`film_status`); this module
enforces the transition rules that the schema can't (JSON Schema has no
notion of prior state).

State graph:

                      dispatch
    pending ────────────────────────────> assembling
       ^                                    │
       │                                    │ success
       │       clear_edit + transition      ├──────────> composed (terminal)
       │ <───────────────────────────────── │
       │   recovery (dead session)          │ failure
       │                                    └──────────> compose_failed
       │                                                     │
       │                                                     │
       │                   clear_edit + transition           │
       └─────────────────────────────────────────────────────┘

Transition table (authoritative):

    None (migration) -> pending
    pending          -> assembling
    assembling       -> composed | compose_failed | pending
    compose_failed   -> pending
    composed         -> {}                # terminal; unset only via operator

**Invariant (option-a):** every transition INTO `pending` clears
`artifacts/edit/`. That means `pending` always implies a clean workspace;
the next dispatch cannot inherit poisoned lint state from a dead session.

The recovery step (`recover_on_startup`) runs at orchestrator construction
before any work begins. If it finds `film_status == "assembling"`, the
prior session died mid-compose — transition to pending (which clears edit)
and log the recovery so operators can see it happened. Recovery is
**idempotent**: running it on a manifest already in `pending` is a no-op.

Events vs manifest audit: the schema's root has no `history[]` list, so
film-level transitions are audited via events.db (event kind
`film_status_transition`) rather than an in-manifest log. Callers that
want a persistent record in the manifest itself can extend
`creative_decisions[]` separately; that's out of scope here.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .events import EventLog


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


PENDING = "pending"
ASSEMBLING = "assembling"
COMPOSED = "composed"
COMPOSE_FAILED = "compose_failed"

FILM_STATUSES = frozenset({PENDING, ASSEMBLING, COMPOSED, COMPOSE_FAILED})

# Map of (current_status) -> set of allowed next statuses. `None` represents
# the pre-migration absent state (legacy manifests predating this field).
VALID_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({PENDING}),
    PENDING: frozenset({ASSEMBLING}),
    ASSEMBLING: frozenset({COMPOSED, COMPOSE_FAILED, PENDING}),
    COMPOSE_FAILED: frozenset({PENDING}),
    COMPOSED: frozenset(),   # terminal — operator intervention required
}

# Path relative to project_root where the Editor writes its composition +
# render artifacts. Clearing this is the load-bearing invariant on every
# transition into `pending`.
EDIT_ARTIFACTS_RELPATH = Path("artifacts") / "edit"


# ---------------------------------------------------------------------------
# Errors + result types
# ---------------------------------------------------------------------------


class InvalidTransition(RuntimeError):
    """Raised by `transition()` when (current, next) is not in
    VALID_TRANSITIONS. Callers should catch this only at boundaries where
    operator-facing diagnostics make sense."""

    def __init__(self, current: str | None, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"invalid film_status transition: {current!r} -> {target!r}. "
            f"allowed next from {current!r}: "
            f"{sorted(VALID_TRANSITIONS.get(current, frozenset()))}"
        )


@dataclass(frozen=True)
class RecoveryReport:
    """What recover_on_startup did. Consumed by the orchestrator for logging
    and by tests for invariants."""

    ran: bool                    # True if a transition happened
    prior_status: str | None
    new_status: str
    cleared_count: int           # number of top-level entries cleared from artifacts/edit/
    cleared_paths: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def current_status(manifest: Mapping[str, Any]) -> str | None:
    """Return the current film_status or None if absent (legacy manifest).

    Callers should prefer this over `manifest.get("film_status")` directly
    so the type boundary (None vs str) is explicit.
    """
    value = manifest.get("film_status")
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"film_status must be str or absent, got {type(value).__name__}")
    return value


def migrate(manifest: dict[str, Any]) -> bool:
    """Inject `film_status: 'pending'` on legacy manifests missing the
    field. Returns True if the manifest was mutated.

    Called by load_manifest on every read so the rest of the pipeline can
    assume the field is present.
    """
    if current_status(manifest) is None:
        manifest["film_status"] = PENDING
        return True
    return False


def clear_edit_artifacts(project_root: Path) -> tuple[int, tuple[str, ...]]:
    """Remove everything under `project_root/artifacts/edit/`. Safe if the
    directory doesn't exist.

    Returns (count, relative_paths_cleared) — the top-level entries that
    were removed. Nested contents count as one entry (their parent dir).

    Why rmtree the whole dir: half-authored index.html + half-copied assets
    are exactly the state Hyperframes' lint will misreport on. A clean
    slate is the only safe recovery.
    """
    edit_dir = project_root / EDIT_ARTIFACTS_RELPATH
    if not edit_dir.exists():
        return 0, ()

    entries = sorted(edit_dir.iterdir())
    relpaths = tuple(str(e.relative_to(project_root)) for e in entries)

    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()

    return len(entries), relpaths


def transition(
    manifest: dict[str, Any],
    target: str,
    *,
    project_root: Path,
) -> tuple[str | None, int, tuple[str, ...]]:
    """Advance film_status to `target`, enforcing VALID_TRANSITIONS.

    Returns (prior_status, cleared_count, cleared_paths). `cleared_*` are
    non-zero only when transitioning INTO `pending` (the option-a
    invariant). Every transition into pending clears artifacts/edit/;
    other transitions leave the filesystem alone.

    Raises:
        InvalidTransition: when (current, target) is not allowed.
        ValueError: when target is not a known status.
    """
    if target not in FILM_STATUSES:
        raise ValueError(
            f"unknown film_status {target!r}; must be one of {sorted(FILM_STATUSES)}"
        )

    current = current_status(manifest)
    allowed = VALID_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidTransition(current, target)

    cleared_count = 0
    cleared_paths: tuple[str, ...] = ()
    if target == PENDING:
        cleared_count, cleared_paths = clear_edit_artifacts(project_root)

    manifest["film_status"] = target
    return current, cleared_count, cleared_paths


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------


_RECOVERABLE_STATUSES = frozenset({ASSEMBLING, COMPOSE_FAILED})

_RECOVERY_REASON_BY_PRIOR = {
    ASSEMBLING: "dead_session_recovery",
    COMPOSE_FAILED: "compose_failed_resume",
}


def recover_on_startup(
    manifest: dict[str, Any],
    *,
    project_root: Path,
    events: EventLog | None = None,
) -> RecoveryReport:
    """Run once at orchestrator construction, before any shot work.

    Behavior (two recovery paths, same downstream effect):
        - film_status == "assembling": dead session (prior run crashed
          mid-compose). Transition to pending with reason
          `dead_session_recovery`.
        - film_status == "compose_failed": operator re-invoked
          `rectoverso film --resume` on a failed manifest to retry.
          Transition to pending with reason `compose_failed_resume`.
          Same dispatch path picks up from there — if upstream is still
          broken, it fails again; if it was fixed, it succeeds.
        - Any other state (pending, composed, absent/legacy): no-op.

    In both recovery cases the option-a invariant fires: the transition
    INTO pending clears artifacts/edit/ so the next Editor session
    starts from a clean workspace. No special "retry editor" flag —
    compose_failed and assembling both funnel into the same trigger path.

    Does NOT:
        - Auto-re-dispatch the Editor. That's the orchestrator's trigger
          hook's job on the next run loop.
        - Mutate any other manifest fields (attempts, history[], etc.).
        - Save the manifest to disk — caller bundles the save with its
          own atomic-save cadence.

    Idempotent: calling this twice on the same manifest (when no
    external writer flipped film_status back) is a clean no-op the second
    time through.
    """
    status = current_status(manifest)
    if status not in _RECOVERABLE_STATUSES:
        return RecoveryReport(
            ran=False,
            prior_status=status,
            new_status=status or PENDING,
            cleared_count=0,
            cleared_paths=(),
        )

    prior, cleared_count, cleared_paths = transition(
        manifest, PENDING, project_root=project_root
    )

    if events is not None:
        events.write(
            "film_status_transition",
            agent="orchestrator",
            shot_id=None,
            payload={
                "from": prior,
                "to": PENDING,
                "reason": _RECOVERY_REASON_BY_PRIOR.get(prior, "unspecified"),
                "cleared_count": cleared_count,
                "cleared_paths": list(cleared_paths),
            },
        )

    return RecoveryReport(
        ran=True,
        prior_status=prior,
        new_status=PENDING,
        cleared_count=cleared_count,
        cleared_paths=cleared_paths,
    )
