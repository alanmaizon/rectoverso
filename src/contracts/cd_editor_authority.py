"""Contract 5 — CD <-> Editor authority.

See docs/contracts.md § Contract 5.

Two check modes:

1. **Film-level** (shot_id is None) — fires before `invoke_editor_agent()`.
   Blocks if any shot has an unaddressed `creative_director` `creative_feedback[]`
   entry at priority `high` or `critical`. Producer must run the pre-Editor CD
   review and address each such entry before Editor dispatches. This prevents
   the CD ↔ Editor ping-pong the convergence gate would otherwise have to catch
   on the second pass.

2. **Shot-level** (shot_id set, ctx['editor_priority'] set) — fires when the
   Producer is about to act on an Editor-authored `creative_feedback[]` entry
   at the named shot. Compares that entry's priority against any unaddressed
   CD feedback on the same shot:
     - CD strictly higher: block (wrong authority resolution attempt)
     - CD equal priority: warn (CD wins; Editor's suggestion deferred)
     - CD lower or absent: no violation

Authority order for equal-priority conflicts mirrors
`tests/creative/resolver.py` AUTHORITY_ORDER (creative_director=2, editor_agent=1).
"""

from __future__ import annotations

from typing import Any, Mapping

from .registry import register
from .types import ContractName, Severity, Violation


_PRIORITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}
_BLOCKING_PRIORITIES = frozenset({"critical", "high"})


def _find_shot(manifest: Mapping[str, Any], shot_id: str) -> Mapping[str, Any] | None:
    for s in manifest.get("shots", []):
        if s.get("shot_id") == shot_id:
            return s
    return None


def _unaddressed_cd(shot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        f
        for f in shot.get("creative_feedback", [])
        if f.get("from_agent") == "creative_director"
        and not f.get("addressed", False)
    ]


def _film_level_check(manifest: Mapping[str, Any]) -> list[Violation]:
    pending: list[tuple[str, Mapping[str, Any]]] = []
    for shot in manifest.get("shots", []):
        for f in _unaddressed_cd(shot):
            if f.get("priority") in _BLOCKING_PRIORITIES:
                pending.append((shot.get("shot_id", "?"), f))
    if not pending:
        return []
    return [
        Violation(
            contract=ContractName.CD_EDITOR_AUTHORITY,
            severity=Severity.BLOCK,
            reason=(
                f"Editor invocation blocked — {len(pending)} unaddressed CD feedback "
                f"entries at priority >= high pending across the film"
            ),
            detail={
                "entries": [
                    {
                        "shot_id": sid,
                        "priority": f.get("priority"),
                        "ts": f.get("ts"),
                    }
                    for sid, f in pending
                ]
            },
        )
    ]


def _shot_level_check(
    manifest: Mapping[str, Any], shot_id: str, ctx: Mapping[str, Any]
) -> list[Violation]:
    editor_priority = ctx.get("editor_priority")
    if editor_priority is None:
        # No priority supplied — nothing to compare against. Skip.
        return []

    shot = _find_shot(manifest, shot_id)
    if shot is None:
        return [
            Violation(
                contract=ContractName.CD_EDITOR_AUTHORITY,
                severity=Severity.BLOCK,
                reason=f"shot_id {shot_id!r} not found in manifest",
                shot_id=shot_id,
            )
        ]

    ep_rank = _PRIORITY_RANK.get(editor_priority, 0)
    cd_entries = _unaddressed_cd(shot)
    if not cd_entries:
        return []

    higher = [
        f for f in cd_entries if _PRIORITY_RANK.get(f.get("priority", ""), 0) > ep_rank
    ]
    equal = [
        f for f in cd_entries if _PRIORITY_RANK.get(f.get("priority", ""), 0) == ep_rank
    ]

    if higher:
        return [
            Violation(
                contract=ContractName.CD_EDITOR_AUTHORITY,
                severity=Severity.BLOCK,
                reason=(
                    f"Editor action on {shot_id} at priority={editor_priority!r} "
                    "blocked — CD feedback at strictly higher priority is unaddressed"
                ),
                shot_id=shot_id,
                detail={
                    "editor_priority": editor_priority,
                    "cd_priorities": [f.get("priority") for f in higher],
                },
            )
        ]
    if equal:
        return [
            Violation(
                contract=ContractName.CD_EDITOR_AUTHORITY,
                severity=Severity.WARN,
                reason=(
                    f"Editor action on {shot_id} at priority={editor_priority!r} "
                    "deferred — same-priority CD feedback takes precedence"
                ),
                shot_id=shot_id,
                detail={"editor_priority": editor_priority},
            )
        ]
    return []


def check(
    manifest: Mapping[str, Any],
    shot_id: str | None,
    ctx: Mapping[str, Any],
) -> list[Violation]:
    if shot_id is None:
        return _film_level_check(manifest)
    return _shot_level_check(manifest, shot_id, ctx)


register(ContractName.CD_EDITOR_AUTHORITY, check)
