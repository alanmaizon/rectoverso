"""Contract 6 — Normalize -> Editor.

**Silent breakage case.** An approved shot with `status == "approved"` but no
`final.normalized_path` would otherwise let the Editor feed a raw provider
render directly into the Hyperframes composition. Raw renders are codec-
heterogeneous across providers (Wan 1280x720 24/30fps h264 High; Kling
1928x1072 24fps h264 Main; Seedance 1280x720 24fps h264 High), and feeding
that mix to Hyperframes' internal ffmpeg produces "non monotonically
increasing dts" errors on the concat muxer — the exact failure mode the
NormalizeTool pre-pass was engineered to eliminate. Silent fallback to
render_path re-introduces the corruption.

**Block severity, not warn.** The Editor cannot safely degrade. A missing
normalized artifact is a hard precondition failure. Operator remediation:
re-run `rectoverso film --resume` to backfill (the orchestrator's
`_maybe_normalize` resume hook is the intended recovery path). If normalize
failed for intrinsic reasons (corrupt source render), the shot must be
escalated out of the Editor's scope first — not quietly substituted.

**Scope.** Every shot with `status == "approved"` in the manifest that the
Editor will touch. V1 Editor assembles the full film from the approved
shots — scope is "all approved shots in the manifest." If a future Editor
tool operates on a subset, it MUST pass `ctx["editor_shot_scope"]` as a
list of shot_ids and this contract will filter to that subset.

Only `approved` shots are checked. `rejected`, `escalated`, `failed`, and
any other non-approved status is out of scope — those aren't shipping in
the composition.
"""

from __future__ import annotations

from typing import Any, Mapping

from .registry import register
from .types import ContractName, Severity, Violation


def _shot_scope(
    manifest: Mapping[str, Any],
    shot_id: str | None,
    ctx: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Resolve which shots the Editor will ingest.

    Priority order:
    1. If shot_id is passed (per-shot contract invocation), use just that shot.
    2. If ctx["editor_shot_scope"] is a non-empty list, filter to those ids.
    3. Otherwise full-manifest scope — every shot in shots[].
    """
    all_shots = list(manifest.get("shots", []))
    if shot_id is not None:
        return [s for s in all_shots if s.get("shot_id") == shot_id]
    scope = ctx.get("editor_shot_scope")
    if isinstance(scope, list) and scope:
        wanted = set(scope)
        return [s for s in all_shots if s.get("shot_id") in wanted]
    return all_shots


def check(
    manifest: Mapping[str, Any],
    shot_id: str | None,
    ctx: Mapping[str, Any],
) -> list[Violation]:
    violations: list[Violation] = []

    for shot in _shot_scope(manifest, shot_id, ctx):
        if shot.get("status") != "approved":
            continue  # out of scope: non-approved shots do not ship

        sid = shot.get("shot_id")
        final = shot.get("final") or {}
        normalized_path = final.get("normalized_path")

        # Missing OR empty string both fail the precondition.
        if not normalized_path:
            violations.append(
                Violation(
                    contract=ContractName.NORMALIZE_TO_EDITOR,
                    severity=Severity.BLOCK,
                    reason=(
                        f"shot {sid} is approved but final.normalized_path is "
                        "missing; Editor cannot ingest a raw render into "
                        "Hyperframes without reintroducing codec heterogeneity. "
                        "Re-run `rectoverso film --resume` to backfill, or "
                        "escalate the shot out of Editor scope."
                    ),
                    shot_id=sid,
                    detail={"render_path": final.get("render_path")},
                )
            )
            continue

        # Paired md5 — schema's dependentRequired enforces this on write, but
        # we belt+braces check it here too. An md5-less path cannot serve as
        # a regression-test snapshot, and its absence means the write somehow
        # skipped the md5 capture (shouldn't happen but is cheap to assert).
        if not final.get("normalized_md5"):
            violations.append(
                Violation(
                    contract=ContractName.NORMALIZE_TO_EDITOR,
                    severity=Severity.BLOCK,
                    reason=(
                        f"shot {sid} has final.normalized_path but "
                        "normalized_md5 is missing; audit chain broken"
                    ),
                    shot_id=sid,
                    detail={"normalized_path": normalized_path},
                )
            )

    return violations


register(ContractName.NORMALIZE_TO_EDITOR, check)
