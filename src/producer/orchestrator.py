"""FilmOrchestrator — the `rectoverso film` driver.

Chains the existing per-shot commands (render → judge → revise → re-render
→ re-judge) into a sequential, resumable, budget-aware walk across all
shots in a manifest. Produces a FilmResult the CLI surfaces as exit code
and summary table.

State synchronization: the underlying CLI commands (cmd_render, cmd_judge,
cmd_revise, cmd_generate_ref) each load the manifest from disk, mutate it,
and save atomically. They do NOT mutate the orchestrator's in-memory manifest
dict. So after every tool call the orchestrator reloads the shot from disk
via `_reload_shot_from_disk` — keeps the shared view consistent with the
authoritative on-disk state. Attempt id, status, attempts list, judge score
all need this refresh; reading them off the stale in-memory shot silently
breaks the state machine.

Design principles:

1. **Sequential, not parallel.** Simpler to debug. No rate-limit thrash.
   Parallel is a Day-6 optimization.

2. **Resumable.** The existing commands save the manifest atomically after
   every event; restarting `rectoverso film` on a partially-rendered
   manifest skips already-approved shots and picks up where it left off.

3. **Tool-agnostic.** The orchestrator doesn't import any concrete tool
   classes — it receives a ToolSet at construction. Tests pass stubs;
   film_cmd.py wires in the real adapters. Matches how the rest of the
   pipeline (dispatch, contracts) stays decoupled.

4. **One budget halt policy.** On a budget refusal, the orchestrator stops
   launching new shots — it does NOT fall back to cheaper providers
   silently. Halt + surface is the contract. Operator can resume after
   topping up or switching run_mode.

5. **Escalation is terminal.** A shot that escalated (any reason) is NOT
   retried. The retry loop only fires on `rejected` outcomes. This keeps
   the volatility and max-attempts-exhausted signals meaningful.

6. **No Editor auto-assembly.** The orchestrator stops when every shot
   has a terminal status. A separate command (or the hand-written
   Hyperframes path already validated) ties the approved shots together.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .budget import BudgetCheck, BudgetExceeded, check_before_render, record_spend
from .events import EventLog
from .manifest_io import save_manifest_atomic
from .orchestrator_types import FilmResult, RetryPolicy, ShotSummary


# ---------------------------------------------------------------------------
# Tool Protocol hooks
# ---------------------------------------------------------------------------


class RenderFn(Protocol):
    """Callable signature the orchestrator uses to render one shot attempt.

    Matches the shape render_cmd.py's per-attempt block produces: takes the
    shot + current attempt_id, returns the tool result dict.
    """

    def __call__(
        self, *, shot: dict, attempt_id: int, run_mode: str
    ) -> dict: ...


class JudgeFn(Protocol):
    """Callable signature for judging one rendered attempt."""

    def __call__(self, *, shot: dict, attempt_id: int) -> dict: ...


class ReviseFn(Protocol):
    """Callable for dispatching PromptSmith revision after a rejection."""

    def __call__(self, *, shot: dict) -> dict: ...


class GenerateRefFn(Protocol):
    """Callable for generating a reference image when an I2V shot needs one."""

    def __call__(self, *, shot: dict, brief: Mapping[str, Any]) -> dict: ...


class AudioFn(Protocol):
    """Callable for dispatching one audio cue (SFX or TTS) after a shot
    has been approved. Receives the shot, the cue dict from
    `shot.audio_cues[i]`, and a 1-indexed attempt_id. Returns the
    adapter's result dict (shape: ElevenLabsAudioTool's __call__ return).
    """

    def __call__(self, *, shot: dict, cue: dict, attempt_id: int) -> dict: ...


@dataclass
class ToolSet:
    """Bundle of callables the orchestrator invokes. The real film_cmd.py
    wires these to the Producer dispatch commands; tests pass stubs."""

    render: RenderFn
    judge: JudgeFn
    revise: ReviseFn
    generate_ref: GenerateRefFn | None = None    # optional; only used for I2V shots w/o ref
    # Audio is optional for the orchestrator — when None, the audio phase
    # is skipped entirely (tests that don't care about audio pass None).
    audio: AudioFn | None = None


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


class FilmOrchestrator:
    """Walks every shot in a manifest through render → judge (→ revise loop)
    until each lands approved or escalated.

    Not thread-safe. Do not share an instance across concurrent film runs.
    """

    def __init__(
        self,
        *,
        manifest: dict,
        manifest_path: Path,
        events: EventLog,
        tools: ToolSet,
        retry_policy: RetryPolicy | None = None,
        run_mode: str = "testing",
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.manifest = manifest
        self.manifest_path = manifest_path
        self.events = events
        self.tools = tools
        self.retry_policy = retry_policy or RetryPolicy()
        self.run_mode = run_mode
        self._now = now_fn
        # Per-shot audio stats tracked here (not on the shot, to keep the
        # schema clean). Populated by _maybe_process_audio, read by the
        # summary builders. Keyed by shot_id.
        self._audio_stats: dict[str, dict[str, int]] = {}

    # -- entry point -------------------------------------------------------

    def run(self) -> FilmResult:
        """Walk every shot. Returns FilmResult with per-shot breakdown."""
        started = self._now()
        shots = self.manifest.get("shots", [])
        summaries: list[ShotSummary] = []
        halted_reason: str | None = None

        for shot in shots:
            if halted_reason is not None:
                # Budget halt or dispatch error already fired earlier in the
                # film — record every remaining shot as budget_halted so the
                # summary tells the full story.
                summaries.append(
                    ShotSummary(
                        shot_id=shot["shot_id"],
                        final_status="budget_halted",
                        attempts=len(shot.get("attempts", [])),
                        best_judge_score=_best_judge_score(shot),
                        final_render_path=None,
                        total_cost_usd=0.0,
                    )
                )
                continue

            summary = self._run_shot(shot)
            summaries.append(summary)

            if summary.final_status == "budget_halted":
                halted_reason = f"budget refused on {summary.shot_id}"
                if not self.retry_policy.stop_film_on_budget_halt:
                    halted_reason = None   # continue per policy (not implemented for v1)

        total_latency = round(self._now() - started, 3)
        return _summarize(
            manifest=self.manifest,
            manifest_path=self.manifest_path,
            summaries=summaries,
            total_latency_s=total_latency,
            halted_reason=halted_reason,
        )

    # -- per-shot inner loop ----------------------------------------------

    def _run_shot(self, shot: dict) -> ShotSummary:
        """Run one shot through the render → judge → revise loop until it
        lands approved, escalated, or budget-halts."""
        shot_started = self._now()

        # Skip shots already in a terminal state. This makes the orchestrator
        # resumable: a partially-rendered manifest re-run picks up where it
        # left off without touching approved or escalated shots.
        if shot.get("status") in ("approved", "escalated"):
            # Resumability hook for audio: an approved shot with
            # audio_status="pending" still gets its audio phase run. Operator
            # can edit audio_cues between runs and resume to backfill.
            self._maybe_process_audio(shot)
            return _summary_from_existing(
                shot, shot_started, self._now(),
                audio_stats=self._audio_stats.get(shot["shot_id"]),
            )

        # If the shot is mid-flight (rendering/judging) from a crashed run,
        # treat it like prompted — the commands are idempotent enough to
        # re-attempt. A fully stuck shot shows up in run_state.resumable.
        if shot.get("status") not in ("prompted", "routed", "rejected", "rendering", "judging"):
            return ShotSummary(
                shot_id=shot["shot_id"],
                final_status="failed",
                attempts=len(shot.get("attempts", [])),
                best_judge_score=_best_judge_score(shot),
                final_render_path=None,
                total_cost_usd=_shot_cost(shot),
                latency_s=round(self._now() - shot_started, 3),
            )

        # I2V guard: Kling, Seedance I2V, etc. need an image_url. If the
        # shot is routed to such a provider without a reference, auto-call
        # generate-ref before the first render. "Only when missing" — we
        # never overwrite an existing reference (per user policy: refs may
        # be intentional continuity choices).
        needs_ref = _shot_needs_reference_image(shot)
        if needs_ref and self.tools.generate_ref is not None:
            refs = (shot.get("prompt") or {}).get("reference_subject_paths") or []
            if not refs:
                self.tools.generate_ref(shot=shot, brief=self.manifest.get("brief") or {})
                # cmd_generate_ref wrote the new ref path into the on-disk
                # manifest; sync so subsequent render calls see the ref.
                self._reload_shot_from_disk(shot)

        # Main loop.
        while True:
            attempts_count = len(shot.get("attempts", []))
            if attempts_count >= self.retry_policy.max_attempts_per_shot:
                # Shouldn't be reachable under normal flow — the judge
                # escalates on max_attempts_exhausted BEFORE we loop back here
                # — but belt+braces in case the outcome path changes.
                return _escalated_summary(
                    shot, shot_started, self._now(), "max_attempts_exhausted"
                )

            next_attempt_id = attempts_count + 1

            # Pre-flight budget check.
            budget_check = self._pre_flight_budget(shot)
            if not budget_check.allowed:
                return ShotSummary(
                    shot_id=shot["shot_id"],
                    final_status="budget_halted",
                    attempts=attempts_count,
                    best_judge_score=_best_judge_score(shot),
                    final_render_path=None,
                    total_cost_usd=_shot_cost(shot),
                    latency_s=round(self._now() - shot_started, 3),
                )

            # Render. cmd_render writes to disk; we reload after so the
            # orchestrator sees the new status/attempts.
            try:
                self.tools.render(
                    shot=shot, attempt_id=next_attempt_id, run_mode=self.run_mode
                )
            except BudgetExceeded:
                # Race: budget was fine at pre-flight but got hit by a
                # parallel write. Same halt semantics.
                return ShotSummary(
                    shot_id=shot["shot_id"],
                    final_status="budget_halted",
                    attempts=attempts_count,
                    best_judge_score=_best_judge_score(shot),
                    final_render_path=None,
                    total_cost_usd=_shot_cost(shot),
                    latency_s=round(self._now() - shot_started, 3),
                )
            self._reload_shot_from_disk(shot)

            # After render: shot status should be "judging" if render OK,
            # or "failed" otherwise. Judge fires on judging state.
            if shot.get("status") == "judging":
                self.tools.judge(shot=shot, attempt_id=next_attempt_id)
                self._reload_shot_from_disk(shot)

            # Re-read attempts after render + judge projected their state.
            new_status = shot.get("status")
            if new_status == "approved":
                # Audio dispatch happens per-shot, immediately after approval.
                # Failures here DO NOT demote the shot — audio_status is
                # tracked separately (see shot.audio_status in the schema).
                self._maybe_process_audio(shot)
                return _summary_from_existing(
                    shot, shot_started, self._now(),
                    audio_stats=self._audio_stats.get(shot["shot_id"]),
                )
            if new_status == "escalated":
                esc_reason = _latest_escalation_reason(shot)
                return _escalated_summary(
                    shot, shot_started, self._now(), esc_reason,
                    audio_stats=self._audio_stats.get(shot["shot_id"]),
                )
            if new_status == "failed":
                return ShotSummary(
                    shot_id=shot["shot_id"],
                    final_status="failed",
                    attempts=len(shot.get("attempts", [])),
                    best_judge_score=_best_judge_score(shot),
                    final_render_path=None,
                    total_cost_usd=_shot_cost(shot),
                    latency_s=round(self._now() - shot_started, 3),
                )
            if new_status == "rejected":
                # Retry loop: revise the prompt with judge notes, then
                # the while() continues and renders again with the new prompt.
                # Contract 2 fires inside revise — if judge_notes is empty,
                # revise raises and the shot escalates on the next iteration.
                self.tools.revise(shot=shot)
                self._reload_shot_from_disk(shot)
                # revise_cmd flips status back to "rejected" with a new
                # prompt. Next while() iteration will render attempt+1.
                continue

            # Defensive: unexpected status after render/judge.
            return ShotSummary(
                shot_id=shot["shot_id"],
                final_status="failed",
                attempts=len(shot.get("attempts", [])),
                best_judge_score=_best_judge_score(shot),
                final_render_path=None,
                total_cost_usd=_shot_cost(shot),
                latency_s=round(self._now() - shot_started, 3),
            )

    # -- helpers -----------------------------------------------------------

    def _pre_flight_budget(self, shot: dict) -> BudgetCheck:
        """Project the next attempt's cost against the budget. Uses the same
        estimate the render_cmd does, so the orchestrator and the individual
        render call agree on whether a shot fits."""
        from src.rectoverso.render_cmd import _estimate_render_cost  # lazy: break circular
        provider_id = shot["routing"]["chosen_provider"]
        duration = int(round(float(shot["duration_s"])))
        est_cost, est_quota = _estimate_render_cost(
            provider_id, duration, model=shot["routing"]["chosen_model"]
        )
        return check_before_render(
            self.manifest,
            provider_id=provider_id,
            estimated_cost_usd=est_cost,
            estimated_quota_cost=est_quota,
        )

    def _reload_shot_from_disk(self, shot: dict) -> None:
        """Sync the orchestrator's in-memory view with the on-disk manifest.

        The CLI tools (cmd_render, cmd_judge, cmd_revise, cmd_generate_ref)
        each load, mutate, and atomically save their own copy of the manifest.
        The `shot` dict the orchestrator passed them is NOT mutated. Without
        this reload, the orchestrator reads stale `status` / `attempts` /
        `prompt` and the state machine silently stalls.

        Reload the whole manifest (budget counters move too) and mutate the
        caller-held `shot` in-place via clear()+update() so existing
        references stay valid.
        """
        try:
            disk = json.loads(Path(self.manifest_path).read_text())
        except (OSError, json.JSONDecodeError):
            return  # disk not readable mid-write; keep in-memory state
        # Refresh the manifest-level state (budget, run_state, timestamps).
        for key in ("budget", "run_state", "updated_at", "edit"):
            if key in disk:
                self.manifest[key] = disk[key]
        # Re-sync the matching shot in-place so callers holding the ref
        # see the fresh state.
        for fresh in disk.get("shots", []):
            if fresh.get("shot_id") == shot.get("shot_id"):
                shot.clear()
                shot.update(fresh)
                return

    # -- Audio phase -------------------------------------------------------

    def _maybe_process_audio(self, shot: dict) -> None:
        """Dispatch each entry in shot.audio_cues once, project results into
        manifest.audio.{sfx|dialogue}, set shot.audio_status.

        Guard-rails:
        - No-op if `self.tools.audio` is None (audio not wired for this run).
        - No-op if `shot.audio_status` is already terminal (ok/partial/failed/
          skipped) — audio is at-most-once per shot per run, matching the
          "adapter-level retry cap of 1" policy. To re-attempt audio, the
          operator edits the manifest to reset audio_status to "pending" and
          re-runs the orchestrator.
        - An audio call failure does NOT change shot.status. The shot stays
          approved; audio_status carries the partial/failed signal for the
          Editor to consume at composition time.
        """
        if self.tools.audio is None:
            return
        if shot.get("audio_status") in ("ok", "partial", "failed", "skipped"):
            # Refresh stats from shot if we're resuming and the stats dict
            # is empty (e.g., orchestrator was re-instantiated mid-film).
            return

        shot_id = shot["shot_id"]
        cues = shot.get("audio_cues") or []
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        if not cues:
            shot["audio_status"] = "skipped"
            self._audio_stats[shot_id] = {"cues_total": 0, "cues_ok": 0, "credits_used": 0}
            self._save_manifest_after_audio()
            return

        ok_count = 0
        credits = 0
        for i, cue in enumerate(cues):
            try:
                result = self.tools.audio(shot=shot, cue=cue, attempt_id=i + 1)
            except Exception as exc:
                # Bare exceptions (missing key, bad ffprobe) bubble here;
                # adapter-surfaced failures come back as status=failed dicts.
                # Log into history and continue — one crashed cue shouldn't
                # torpedo later cues on the same shot.
                shot.setdefault("history", []).append({
                    "ts": now_iso,
                    "event": "audio_dispatch_error",
                    "by": "audio_agent",
                    "detail": (
                        f"cue={cue.get('sfx_id') or cue.get('line_id') or i} "
                        f"exception={type(exc).__name__}: {exc}"
                    )[:200],
                })
                continue

            if result.get("status") == "ok":
                ok_count += 1
                credits += int(result.get("quota_cost", 0))
                _project_audio_result(self.manifest, shot, result)
                shot.setdefault("history", []).append({
                    "ts": now_iso,
                    "event": "audio_cue_ok",
                    "by": "audio_agent",
                    "detail": (
                        f"mode={result.get('mode')} "
                        f"{result.get('sfx_id') or result.get('line_id')} "
                        f"credits={result.get('quota_cost', 0)}"
                    )[:200],
                })
            else:
                shot.setdefault("history", []).append({
                    "ts": now_iso,
                    "event": "audio_cue_failed",
                    "by": "audio_agent",
                    "detail": (
                        f"mode={result.get('mode')} "
                        f"stage={result.get('failure_stage')}"
                    )[:200],
                })

        total = len(cues)
        if ok_count == total:
            shot["audio_status"] = "ok"
        elif ok_count == 0:
            shot["audio_status"] = "failed"
        else:
            shot["audio_status"] = "partial"

        self._audio_stats[shot_id] = {
            "cues_total": total,
            "cues_ok": ok_count,
            "credits_used": credits,
        }
        self._save_manifest_after_audio()

    def _save_manifest_after_audio(self) -> None:
        """Atomic save after the audio phase mutated the in-memory manifest
        (shot.audio_status, audio.sfx[], audio.dialogue[], history rows)."""
        last_event_id = self.manifest.get("run_state", {}).get("last_event_id", 0)
        save_manifest_atomic(
            self.manifest_path, self.manifest, last_event_id=last_event_id
        )


# ---------------------------------------------------------------------------
# Pure helpers (no mutation of the manifest — read-only inspections)
# ---------------------------------------------------------------------------


def _project_audio_result(manifest: dict, shot: dict, result: Mapping[str, Any]) -> None:
    """Append an audio adapter result into manifest.audio.{sfx|dialogue}.

    Shape matches the schema requirements:
    - audio.sfx[]: shot_id, sfx_id, description, audio_path (4 required fields)
    - audio.dialogue[]: shot_id, line_id, text, voice_id, audio_path,
                        duration_s, timing (7 required fields)

    The `timing` for dialogue is set to [0, duration_s] — the shot-relative
    position. The Editor Agent refines timing at composition time if needed.
    """
    audio_block = manifest.setdefault("audio", {"dialogue": [], "sfx": []})
    mode = result.get("mode")
    # Paths come back as absolute; convert to schema-compliant relative.
    raw_path = result.get("audio_path") or ""
    audio_path = _relative_path(raw_path)

    if mode == "sfx":
        audio_block.setdefault("sfx", []).append({
            "shot_id": shot["shot_id"],
            "sfx_id": str(result.get("sfx_id") or f"sfx_{len(audio_block.get('sfx', [])) + 1}"),
            "description": str(result.get("description") or ""),
            "audio_path": audio_path,
        })
    elif mode == "tts":
        duration_s = float(result.get("duration_s") or 0.5)
        if duration_s <= 0:
            duration_s = 0.5  # schema requires exclusiveMinimum 0
        audio_block.setdefault("dialogue", []).append({
            "shot_id": shot["shot_id"],
            "line_id": str(result.get("line_id") or f"line_{len(audio_block.get('dialogue', [])) + 1}"),
            "text": str(result.get("text") or ""),
            "voice_id": str(result.get("voice_id") or ""),
            "audio_path": audio_path,
            "duration_s": duration_s,
            "timing": {"in_s": 0.0, "out_s": duration_s},
        })


def _relative_path(path_str: str) -> str:
    """Schema's relativePath pattern forbids leading '/' or '~'. Convert
    absolute paths to cwd-relative."""
    import os
    if not path_str:
        return ""
    if path_str.startswith(("/", "~")):
        return os.path.relpath(path_str, start=Path.cwd())
    return path_str


def _best_judge_score(shot: dict) -> float | None:
    scores = [
        float(a["judge_score"])
        for a in shot.get("attempts", [])
        if isinstance(a, Mapping) and isinstance(a.get("judge_score"), (int, float))
    ]
    return max(scores) if scores else None


def _shot_cost(shot: dict) -> float:
    return round(
        sum(float(a.get("cost_usd", 0.0)) for a in shot.get("attempts", [])),
        4,
    )


def _latest_escalation_reason(shot: dict) -> str | None:
    """Pull the escalation_reason from attempts[-1] if present. Falls back to
    looking at the judged_escalated history entry if the attempt doesn't
    have the field (e.g., from an older manifest before the escalation_reason
    schema extension)."""
    attempts = shot.get("attempts") or []
    if attempts and isinstance(attempts[-1], Mapping):
        reason = attempts[-1].get("escalation_reason")
        if reason:
            return str(reason)
    # History fallback.
    for entry in reversed(shot.get("history") or []):
        if entry.get("event") == "judged_escalated":
            detail = entry.get("detail") or ""
            for token in detail.split():
                if token.startswith("escalation_reason="):
                    return token.split("=", 1)[1]
    return None


def _shot_needs_reference_image(shot: dict) -> bool:
    """I2V and reference-to-video models need an image input. Heuristic:
    check the chosen_model for the I2V indicator substrings we use elsewhere."""
    model = (shot.get("routing") or {}).get("chosen_model") or ""
    model_lower = model.lower()
    return "image-to-video" in model_lower or "reference-to-video" in model_lower


def _summary_from_existing(
    shot: dict, started: float, now: float,
    audio_stats: Mapping[str, int] | None = None,
) -> ShotSummary:
    """Build a ShotSummary for an already-terminal shot (approved or
    escalated before the orchestrator touched it)."""
    status = shot["status"]
    final_render = (shot.get("final") or {}).get("render_path")
    attempts = shot.get("attempts", [])
    if status == "approved":
        final_status = "approved"
        esc_reason = None
    elif status == "escalated":
        final_status = "escalated"
        esc_reason = _latest_escalation_reason(shot)
    else:
        # Shouldn't happen — caller should have filtered.
        final_status = status
        esc_reason = None
    stats = audio_stats or {}
    return ShotSummary(
        shot_id=shot["shot_id"],
        final_status=final_status,
        attempts=len(attempts),
        best_judge_score=_best_judge_score(shot),
        final_render_path=final_render,
        total_cost_usd=_shot_cost(shot),
        escalation_reason=esc_reason,
        latency_s=round(now - started, 3),
        audio_status=shot.get("audio_status", "pending"),
        audio_cues_total=int(stats.get("cues_total", len(shot.get("audio_cues") or []))),
        audio_cues_ok=int(stats.get("cues_ok", 0)),
        audio_credits_used=int(stats.get("credits_used", 0)),
    )


def _escalated_summary(
    shot: dict, started: float, now: float, reason: str | None,
    audio_stats: Mapping[str, int] | None = None,
) -> ShotSummary:
    stats = audio_stats or {}
    return ShotSummary(
        shot_id=shot["shot_id"],
        final_status="escalated",
        attempts=len(shot.get("attempts", [])),
        best_judge_score=_best_judge_score(shot),
        final_render_path=(shot.get("final") or {}).get("render_path"),
        total_cost_usd=_shot_cost(shot),
        escalation_reason=reason,
        latency_s=round(now - started, 3),
        audio_status=shot.get("audio_status", "pending"),
        audio_cues_total=int(stats.get("cues_total", len(shot.get("audio_cues") or []))),
        audio_cues_ok=int(stats.get("cues_ok", 0)),
        audio_credits_used=int(stats.get("credits_used", 0)),
    )


def _summarize(
    *,
    manifest: dict,
    manifest_path: Path,
    summaries: list[ShotSummary],
    total_latency_s: float,
    halted_reason: str | None,
) -> FilmResult:
    approved = sum(1 for s in summaries if s.final_status == "approved")
    escalated = sum(1 for s in summaries if s.final_status == "escalated")
    budget_halted = sum(1 for s in summaries if s.final_status == "budget_halted")
    failed = sum(1 for s in summaries if s.final_status == "failed")

    by_reason: dict[str, int] = {}
    for s in summaries:
        if s.final_status == "escalated" and s.escalation_reason:
            by_reason[s.escalation_reason] = by_reason.get(s.escalation_reason, 0) + 1

    audio_breakdown: dict[str, int] = {}
    total_audio_credits = 0
    for s in summaries:
        audio_breakdown[s.audio_status] = audio_breakdown.get(s.audio_status, 0) + 1
        total_audio_credits += s.audio_credits_used

    return FilmResult(
        project_id=str(manifest.get("project_id", "")),
        shot_count=len(summaries),
        approved_count=approved,
        escalated_count=escalated,
        budget_halted_count=budget_halted,
        failed_count=failed,
        total_spent_usd=round(
            sum(s.total_cost_usd for s in summaries), 4
        ),
        total_latency_s=round(total_latency_s, 3),
        manifest_path=str(manifest_path),
        shots=tuple(summaries),
        escalations_by_reason=by_reason,
        audio_status_breakdown=audio_breakdown,
        total_audio_credits_used=total_audio_credits,
        halted_reason=halted_reason,
    )
