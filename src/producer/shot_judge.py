"""ShotJudgeTool — Tier-2 vision-based shot evaluator.

Architecture in three steps:

    1. Extract N keyframes from the attempt's MP4 via ffmpeg (stdlib subprocess).
    2. Send them as image blocks to Claude Opus 4.7 Messages API alongside the
       shot's prompt, description, and (optionally) reference keyframes from
       continuity_refs.
    3. Parse the JSON response (scores + flags + notes), compute the outcome
       locally against tunable thresholds, return a dict shaped to project
       into manifest.shots[i].attempts[-1].

The local-threshold split is deliberate (docs/agents.md § Shot Judge, rubric):
    approved      : judge_score >= 0.75 AND no artifact flag
    rejected      : 0.4 <= judge_score < 0.75
    escalated     : judge_score < 0.4 OR attempts >= 3

Keeping the thresholds in code instead of the prompt lets us tune them without
breaking the model's cached system prompt.

Contract tie-in: the output of this tool always populates
`attempts[-1].judge_notes` when outcome is "rejected" (Contract 2
`shot_judge_to_prompt_smith`). A rejected outcome without notes would be a
regression — the tool refuses to return one.

Fallback: if ffmpeg is missing or frame extraction fails, the tool switches to
**text-adherence mode** (per prompts/shot_judge.md § Fallback) and scores on
prompt + description alone. composition defaults to 0.7 (neutral) and
artifact detection is disabled. `mode` is surfaced in the result so the audit
trail shows which path was taken.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .llm import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    LLMClient,
    LLMEmptyResponse,
    LLMJSONDecodeError,
    LLMResponse,
    default_client,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_PROMPT_PATH = REPO_ROOT / "prompts" / "shot_judge.md"

# Thresholds per docs/agents.md § Shot Judge. Tune in code, not in the prompt.
APPROVE_THRESHOLD = 0.75
ESCALATE_THRESHOLD = 0.40
ARTIFACT_PENALTY = 0.30
MAX_ATTEMPTS_BEFORE_ESCALATE = 3

DEFAULT_KEYFRAMES = 3


@dataclass(frozen=True)
class JudgeVerdict:
    """Structured view of the tool's output dict. Kept as a docstring for
    callers; the runtime payload is a plain dict for Tool Protocol compliance."""

    judge_score: float
    composition: float
    prompt_adherence: float
    continuity: float
    artifact_flag: bool
    judge_notes: str
    outcome: str  # "approved" | "rejected" | "escalated"
    rejection_reason: str | None
    mode: str  # "vision" | "text-adherence"


class ShotJudgeTool:
    """Tool-Protocol adapter. `name == "shot_judge"`.

    Payload (ctx dict received via dispatch):
        shot          : full shot dict (description, prompt, continuity_refs,
                        artistic_direction, duration_s, attempts)
        render_path   : path to the attempt's MP4 to judge
        brief         : the manifest's brief (for artistic_style anchor)
        reference_frames : optional list of paths — one keyframe per
                           continuity_ref shot, already extracted by caller
    """

    name = "shot_judge"

    def __init__(
        self,
        *,
        client: LLMClient | None = None,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        system_prompt: str | None = None,
        keyframes: int = DEFAULT_KEYFRAMES,
        ffmpeg_bin: str | None = None,
        extract_frames: Any = None,  # injection hook for tests
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._system_prompt = (
            system_prompt
            if system_prompt is not None
            else SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        )
        self._keyframes = keyframes
        self._ffmpeg = ffmpeg_bin or shutil.which("ffmpeg")
        self._extract_frames = extract_frames or self._default_extract_frames

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if shot_id is None:
            raise ValueError("ShotJudgeTool requires a shot_id")

        shot = payload["shot"]
        render_path = Path(payload["render_path"])
        if not render_path.exists():
            raise FileNotFoundError(f"render not found at {render_path}")

        brief = payload.get("brief") or {}
        reference_frames = list(payload.get("reference_frames") or [])
        attempts = shot.get("attempts") or []

        # 1) frames
        frames, mode_reason = self._extract_or_fallback(render_path, shot)
        mode = "vision" if frames else "text-adherence"

        # 2) build request
        user_blocks = _build_user_blocks(
            shot=shot,
            brief=brief,
            frames=frames,
            reference_frames=reference_frames if mode == "vision" else [],
            mode=mode,
            mode_reason=mode_reason,
        )

        client = self._client or default_client()
        model = self._model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

        system_blocks = [
            {
                "type": "text",
                "text": self._system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        try:
            parsed, resp = _call_with_retry(
                client=client,
                model=model,
                system=system_blocks,
                user_blocks=user_blocks,
                max_tokens=self._max_tokens,
            )
        except (LLMEmptyResponse, LLMJSONDecodeError) as exc:
            raise RuntimeError(
                f"shot_judge: model returned unparseable output ({type(exc).__name__}: {exc})"
            ) from exc

        # 3) normalize, compute outcome. Pass prior attempts' judge_scores so
        # _decide_outcome can detect volatility across the shot's history.
        # The current attempt's score is NOT in prior_scores — it's passed
        # separately as `judge_score`.
        scores = _normalize_scores(parsed, mode=mode)
        judge_score = _compute_judge_score(scores)
        prior_scores = [
            float(a["judge_score"]) for a in attempts[:-1]
            if isinstance(a, Mapping) and isinstance(a.get("judge_score"), (int, float))
        ]
        outcome, rejection_reason, escalation_reason = _decide_outcome(
            judge_score=judge_score,
            artifact_flag=scores["artifact_flag"],
            attempts_count=len(attempts),
            prior_scores=prior_scores,
        )

        notes = _ensure_notes(
            parsed.get("judge_notes"), mode, outcome, escalation_reason
        )

        return {
            "judge_score": round(judge_score, 4),
            "composition": round(scores["composition"], 4),
            "prompt_adherence": round(scores["prompt_adherence"], 4),
            "continuity": round(scores["continuity"], 4),
            "artifact_flag": bool(scores["artifact_flag"]),
            "judge_notes": notes,
            "feedback": parsed.get("feedback") or [],
            "outcome": outcome,
            "rejection_reason": rejection_reason,
            "escalation_reason": escalation_reason,
            "mode": mode,
            "model": resp.model,
            "usage": _usage_dict(resp.usage),
        }

    # -- frame extraction --------------------------------------------------

    def _extract_or_fallback(
        self, render_path: Path, shot: Mapping[str, Any]
    ) -> tuple[list[bytes], str]:
        """Return (frames_as_jpeg_bytes, reason). Empty list = text-adherence mode."""
        if self._ffmpeg is None:
            return [], "ffmpeg not available on PATH"
        try:
            frames = self._extract_frames(
                render_path=render_path,
                duration_s=float(shot.get("duration_s") or 0),
                n=self._keyframes,
                ffmpeg=self._ffmpeg,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            return [], f"ffmpeg frame extraction failed: {exc}"
        if not frames:
            return [], "ffmpeg returned zero frames"
        return frames, ""

    def _default_extract_frames(
        self,
        *,
        render_path: Path,
        duration_s: float,
        n: int,
        ffmpeg: str,
    ) -> list[bytes]:
        """ffmpeg → N JPEG bytes. Pulls at fractions 10/50/90% of duration."""
        frames: list[bytes] = []
        # Probe duration if caller didn't supply one
        dur = duration_s if duration_s > 0 else _ffprobe_duration(ffmpeg, render_path)
        if dur <= 0:
            return []
        fractions = _evenly_spaced_fractions(n)
        for f in fractions:
            t = round(dur * f, 3)
            # -ss before -i is fast seek; -frames:v 1 captures one frame;
            # -f image2pipe + -vcodec mjpeg streams JPEG bytes to stdout.
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel", "error",
                "-ss", str(t),
                "-i", str(render_path),
                "-frames:v", "1",
                "-vf", "scale='min(1024,iw)':-2",  # cap width at 1024, keep aspect
                "-q:v", "3",
                "-f", "image2pipe",
                "-vcodec", "mjpeg",
                "pipe:1",
            ]
            proc = subprocess.run(cmd, capture_output=True, check=False)
            if proc.returncode != 0 or not proc.stdout:
                continue
            frames.append(proc.stdout)
        return frames


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _evenly_spaced_fractions(n: int) -> list[float]:
    if n <= 1:
        return [0.5]
    return [round((i + 1) / (n + 1), 4) for i in range(n)]


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> Any:
    """Lenient JSON extractor — same intent as llm._extract_json but local."""
    candidates: list[str] = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    # last resort — pull a JSON object substring
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"no JSON object found in: {text[:300]!r}")


def _call_with_retry(
    *,
    client: LLMClient,
    model: str,
    system: list[dict[str, Any]],
    user_blocks: list[dict[str, Any]],
    max_tokens: int,
) -> tuple[dict[str, Any], LLMResponse]:
    messages = [{"role": "user", "content": user_blocks}]
    resp = client.create_message(
        model=model, system=system, messages=messages, max_tokens=max_tokens
    )
    text = (resp.text or "").strip()
    if not text:
        raise LLMEmptyResponse("shot_judge empty response", raw="")
    try:
        return _extract_json(text), resp
    except ValueError:
        pass

    # Retry once with an explicit reminder appended to the original user text.
    reminder_blocks = list(user_blocks)
    reminder_blocks.append(
        {
            "type": "text",
            "text": "Reminder: respond with STRICT JSON matching the schema. No prose, no code fence.",
        }
    )
    resp2 = client.create_message(
        model=model,
        system=system,
        messages=[{"role": "user", "content": reminder_blocks}],
        max_tokens=max_tokens,
    )
    text2 = (resp2.text or "").strip()
    if not text2:
        raise LLMEmptyResponse("shot_judge empty response on retry", raw="")
    try:
        return _extract_json(text2), resp2
    except ValueError as exc:
        raise LLMJSONDecodeError(f"shot_judge JSON unparseable: {exc}", raw=text2) from exc


def _build_user_blocks(
    *,
    shot: Mapping[str, Any],
    brief: Mapping[str, Any],
    frames: list[bytes],
    reference_frames: list[Path | str],
    mode: str,
    mode_reason: str,
) -> list[dict[str, Any]]:
    """Assemble the multi-modal user content for the Messages API."""
    blocks: list[dict[str, Any]] = []

    prompt = (shot.get("prompt") or {}).get("primary") or ""
    negative = (shot.get("prompt") or {}).get("negative") or ""
    description = shot.get("description") or ""
    artistic_direction = shot.get("artistic_direction") or ""
    tone = ", ".join(brief.get("tone") or [])
    artistic_style = brief.get("artistic_style") or ""
    attempts_count = len(shot.get("attempts") or [])
    continuity_refs = shot.get("continuity_refs") or []

    preamble = [
        f"Shot: {shot.get('shot_id')}  attempt #{attempts_count}",
        f"Description: {description}",
        f"Primary prompt: {prompt}",
    ]
    if negative:
        preamble.append(f"Negative prompt: {negative}")
    if artistic_direction:
        preamble.append(f"Artistic direction (binding): {artistic_direction}")
    if artistic_style:
        preamble.append(f"Brief artistic_style: {artistic_style}")
    if tone:
        preamble.append(f"Brief tone: {tone}")
    if continuity_refs:
        preamble.append(f"Continuity refs: {', '.join(continuity_refs)}")
    if mode == "text-adherence":
        preamble.append(f"[MODE: text-adherence — {mode_reason}]")

    blocks.append({"type": "text", "text": "\n".join(preamble)})

    # Keyframes of the attempt
    if frames:
        blocks.append(
            {
                "type": "text",
                "text": (
                    f"The next {len(frames)} images are keyframes from the rendered "
                    f"attempt, sampled at evenly spaced times across its duration."
                ),
            }
        )
        for f in frames:
            blocks.append(_image_block(f))

    # Reference frames (optional continuity context)
    if reference_frames:
        blocks.append(
            {
                "type": "text",
                "text": (
                    "The next images are single keyframes from the approved renders "
                    "listed as continuity_refs — use these to judge continuity."
                ),
            }
        )
        for ref in reference_frames:
            ref_path = Path(ref)
            if ref_path.is_file():
                blocks.append(_image_block(ref_path.read_bytes()))

    blocks.append(
        {
            "type": "text",
            "text": (
                "Return STRICT JSON, no code fence, with exactly these keys:\n"
                '  {"composition": 0..1, "prompt_adherence": 0..1, "continuity": 0..1,\n'
                '   "artifact_flag": bool, "judge_notes": "<concrete, specific observations>",\n'
                '   "feedback": [{"feedback_type": "composition|lighting|timing|continuity|artifact|motion",\n'
                '                 "severity": "critical|warn|note", "observation": "...", "suggestion": "..."}]}\n'
                "If text-adherence mode, score composition=0.7 (neutral) and artifact_flag=false.\n"
                "For the first shot or a shot with no continuity_refs, continuity defaults to 1.0."
            ),
        }
    )
    return blocks


def _image_block(data: bytes) -> dict[str, Any]:
    b64 = base64.b64encode(data).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": b64,
        },
    }


def _normalize_scores(parsed: Mapping[str, Any], *, mode: str) -> dict[str, Any]:
    """Coerce Claude's response dict to the contract shape. Bounds-check each score."""
    c = _clip01(parsed.get("composition"), default=0.7 if mode == "text-adherence" else 0.5)
    pa = _clip01(parsed.get("prompt_adherence"), default=0.5)
    co = _clip01(parsed.get("continuity"), default=1.0)
    artifact = bool(parsed.get("artifact_flag", False)) if mode == "vision" else False
    return {
        "composition": c,
        "prompt_adherence": pa,
        "continuity": co,
        "artifact_flag": artifact,
    }


def _clip01(value: Any, *, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(0.0, min(1.0, v))


def _compute_judge_score(scores: Mapping[str, Any]) -> float:
    mean_score = (
        float(scores["composition"])
        + float(scores["prompt_adherence"])
        + float(scores["continuity"])
    ) / 3.0
    penalty = ARTIFACT_PENALTY if scores["artifact_flag"] else 0.0
    return max(0.0, mean_score - penalty)


def _decide_outcome(
    *,
    judge_score: float,
    artifact_flag: bool,
    attempts_count: int,
    prior_scores: list[float] | None = None,
) -> tuple[str, str | None, str | None]:
    """Decide (outcome, rejection_reason, escalation_reason).

    Rules are documented in prompts/shot_judge.md § "Decision thresholds" and
    § "Volatility escalation". Kept local (not in the cached system prompt) so
    we can tune thresholds without invalidating the prompt cache on every
    deploy.

    Priority order matters — first match wins:

    1. score < ESCALATE_THRESHOLD      → escalated / below_threshold
    2. would-approve AND volatile      → escalated / volatile_scores
    3. would-approve AND stable        → approved
    4. not would-approve AND attempts >= max → escalated / max_attempts_exhausted
    5. otherwise                        → rejected (with rejection_reason)

    Important: max_attempts_exhausted ONLY fires when the current attempt
    also fails to approve. A would-approve attempt at attempts_count >= 3
    goes to `approved` (or `volatile_scores` if the trajectory is unstable),
    not `max_attempts_exhausted` — retry-count shouldn't override a legitimate
    pass, only a second failure.
    """
    if judge_score < ESCALATE_THRESHOLD:
        return "escalated", None, "below_threshold"
    would_approve = judge_score >= APPROVE_THRESHOLD and not artifact_flag
    if would_approve and _scores_are_volatile(prior_scores, judge_score):
        return "escalated", None, "volatile_scores"
    if would_approve:
        return "approved", None, None
    # Below approve, above escalate. If we've retried enough, stop; otherwise
    # send it back for one more revision.
    if attempts_count >= MAX_ATTEMPTS_BEFORE_ESCALATE:
        return "escalated", None, "max_attempts_exhausted"
    reason = "artifact" if artifact_flag else "auto_judge"
    return "rejected", reason, None


# Any two scores differing by ≥ VOLATILITY_DELTA make the trajectory volatile.
# 0.10 is tuned against the sh_005 v1/v2/v3 run where [0.783, 0.667, 0.833]
# fired (0.833-0.667 = 0.166 > 0.10) and correctly escalated instead of
# silent-approving on a lucky take.
VOLATILITY_DELTA = 0.10


def _scores_are_volatile(prior: list[float] | None, current: float) -> bool:
    """Return True when the attempt history + current score swing enough to
    justify human review even though the current score would approve.

    Needs at least TWO prior attempts to fire (so the current attempt is
    attempt 3+). Two attempts alone can't demonstrate a pattern — one rejection
    followed by a successful revision is the normal retry loop, not volatility.
    """
    if not prior or len(prior) < 2:
        return False
    all_scores = [*prior, current]
    return (max(all_scores) - min(all_scores)) >= VOLATILITY_DELTA


def _ensure_notes(
    raw_notes: Any,
    mode: str,
    outcome: str,
    escalation_reason: str | None = None,
) -> str:
    """Contract 2 requires non-empty judge_notes when outcome == 'rejected'.
    If the model returned empty notes on a rejection, synthesize a minimal
    note so the pipeline doesn't stall on the next revision dispatch.

    For escalations, synthesize a reason-specific note when absent so the
    orchestrator's summary has enough context to route the shot to the right
    human-review queue.
    """
    notes = (raw_notes or "").strip() if isinstance(raw_notes, str) else ""
    if outcome == "rejected" and not notes:
        notes = (
            f"[{mode} mode] Model returned empty judge_notes on a rejected verdict. "
            "Treat as 'quality below threshold; detailed feedback unavailable'."
        )
    if outcome == "escalated" and not notes:
        reason_notes = {
            "below_threshold": (
                f"Judge escalated: score below {ESCALATE_THRESHOLD:.2f}. "
                "Render is too broken for prompt revision to fix — swap provider, "
                "rework the reference image, or drop the shot."
            ),
            "max_attempts_exhausted": (
                f"Judge escalated: {MAX_ATTEMPTS_BEFORE_ESCALATE} attempts without "
                "landing approved. Problem is upstream (prompt, reference, or "
                "routing); more retries will compound cost without signal."
            ),
            "volatile_scores": (
                "Judge escalated: score trajectory across attempts is unstable "
                f"(delta ≥ {VOLATILITY_DELTA:.2f}). The pipeline produced this take "
                "but didn't do so reliably — human should confirm which attempt ships."
            ),
        }.get(escalation_reason or "", "Judge escalated (no structured reason).")
        notes = f"[{mode} mode] {reason_notes}"
    return notes


def _usage_dict(usage: Any) -> dict[str, int]:
    if isinstance(usage, Mapping):
        return {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}
    return {}


def _ffprobe_duration(ffmpeg: str, path: Path) -> float:
    """Best-effort duration probe via ffmpeg. Returns 0.0 on failure."""
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
    if not Path(ffprobe).is_file() and not shutil.which("ffprobe"):
        return 0.0
    probe_bin = ffprobe if Path(ffprobe).is_file() else "ffprobe"
    try:
        out = subprocess.run(
            [
                probe_bin,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return 0.0
