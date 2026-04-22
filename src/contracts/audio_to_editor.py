"""Contract 1 — Audio -> Editor.

See docs/contracts.md § Contract 1.

**Scope narrowed.** The schema already requires `shot_id`, `duration_s`, and
`timing` on every `audio.dialogue[]` entry. Manifest validation runs on every
Producer write, so by the time this contract fires those fields are guaranteed
present. What the schema does NOT require is `compressibility_s` — that is the
pair-specific field the Editor depends on. This contract enforces it.

**Silence handling.** The manifest has no explicit "shot is silent" flag. A
shot with no `audio.dialogue[]` entries is treated as silent by default. If the
caller needs the stricter check (e.g., Producer suspects Audio Agent skipped a
shot), it passes `ctx["require_silent_marker"] = True`; then a shot with no
dialogue must have a `history[]` entry with `event == "shot_silent"` OR appear
in `ctx["silent_shots"]`.
"""

from __future__ import annotations

from typing import Any, Mapping

from .registry import register
from .types import ContractName, Severity, Violation


def _shot_ids_to_check(
    manifest: Mapping[str, Any], shot_id: str | None
) -> list[str]:
    if shot_id is not None:
        return [shot_id]
    return [s["shot_id"] for s in manifest.get("shots", [])]


def _find_shot(manifest: Mapping[str, Any], shot_id: str) -> Mapping[str, Any] | None:
    for s in manifest.get("shots", []):
        if s.get("shot_id") == shot_id:
            return s
    return None


def _dialogue_for_shot(
    manifest: Mapping[str, Any], shot_id: str
) -> list[Mapping[str, Any]]:
    audio = manifest.get("audio", {})
    return [d for d in audio.get("dialogue", []) if d.get("shot_id") == shot_id]


def _has_silent_marker(
    shot: Mapping[str, Any] | None,
    shot_id: str,
    ctx: Mapping[str, Any],
) -> bool:
    if shot_id in set(ctx.get("silent_shots", [])):
        return True
    if shot is None:
        return False
    return any(
        h.get("event") == "shot_silent" for h in shot.get("history", [])
    )


def check(
    manifest: Mapping[str, Any],
    shot_id: str | None,
    ctx: Mapping[str, Any],
) -> list[Violation]:
    violations: list[Violation] = []
    strict_silence = bool(ctx.get("require_silent_marker", False))

    for sid in _shot_ids_to_check(manifest, shot_id):
        shot = _find_shot(manifest, sid)
        if shot is None and shot_id is not None:
            violations.append(
                Violation(
                    contract=ContractName.AUDIO_TO_EDITOR,
                    severity=Severity.BLOCK,
                    reason=f"shot_id {sid!r} not found in manifest",
                    shot_id=sid,
                )
            )
            continue

        dialogue = _dialogue_for_shot(manifest, sid)

        if not dialogue:
            if strict_silence and not _has_silent_marker(shot, sid, ctx):
                violations.append(
                    Violation(
                        contract=ContractName.AUDIO_TO_EDITOR,
                        severity=Severity.BLOCK,
                        reason=(
                            f"shot {sid} has no dialogue entries and no silent marker; "
                            "Editor cannot decide whether Audio is done"
                        ),
                        shot_id=sid,
                    )
                )
            # Non-strict mode: absence of dialogue == implicit silence. No violation.
            continue

        # Shot has dialogue. Every line needs compressibility_s.
        for entry in dialogue:
            if "compressibility_s" not in entry:
                violations.append(
                    Violation(
                        contract=ContractName.AUDIO_TO_EDITOR,
                        severity=Severity.BLOCK,
                        reason=(
                            f"shot {sid} dialogue line {entry.get('line_id')!r}: "
                            "compressibility_s missing — Editor cannot propose timing changes"
                        ),
                        shot_id=sid,
                        detail={"line_id": entry.get("line_id")},
                    )
                )

    return violations


register(ContractName.AUDIO_TO_EDITOR, check)
