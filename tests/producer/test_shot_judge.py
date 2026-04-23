"""Unit tests for src.producer.shot_judge.ShotJudgeTool.

Architecture: inject fake LLMClient + fake frame extractor so tests are
hermetic — no Anthropic API, no ffmpeg, no filesystem beyond tmp_path.

Coverage areas:
    - outcome thresholds (approved / rejected / escalated) with controlled scores
    - artifact penalty and forced-rejection
    - text-adherence fallback when ffmpeg is unavailable
    - judge_notes backfill on rejected/escalated verdicts (Contract 2 safety)
    - JSON parsing leniency (retry on malformed output)
    - usage passthrough for downstream accounting
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.producer.llm import LLMResponse
from src.producer.shot_judge import (
    APPROVE_THRESHOLD,
    ARTIFACT_PENALTY,
    ESCALATE_THRESHOLD,
    MAX_ATTEMPTS_BEFORE_ESCALATE,
    ShotJudgeTool,
    _compute_judge_score,
    _decide_outcome,
    _evenly_spaced_fractions,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class StubClient:
    responses: list[str]
    usage: dict[str, int] | None = None

    def __post_init__(self):
        self.calls: list[dict[str, Any]] = []

    def create_message(self, *, model, system, messages, max_tokens):
        self.calls.append(
            {"model": model, "system": system, "messages": messages, "max_tokens": max_tokens}
        )
        text = self.responses.pop(0)
        return LLMResponse(
            text=text,
            model=model,
            usage=self.usage or {"input_tokens": 10, "output_tokens": 5},
            stop_reason="end_turn",
        )


def _make_render(tmp_path: Path, name: str = "v1.mp4") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x00\x00\x00\x20ftypisom")  # minimal MP4 magic
    return p


def _inject_frames(n: int = 3):
    def _fake(*, render_path, duration_s, n, ffmpeg):
        return [b"\xff\xd8\xff\xe0" + bytes(16) for _ in range(n)]  # fake JPEG bytes
    return _fake


def _ctx(
    render_path: Path,
    *,
    attempts: int = 0,
    prior_scores: list[float] | None = None,
    continuity_refs: list[str] | None = None,
    primary: str = "A quiet shot of the ocean.",
    negative: str = "",
    artistic_direction: str = "",
    brief_tone: list[str] | None = None,
) -> dict:
    # prior_scores: optional judge_score values to stamp onto the prior
    # attempts (used by volatility tests). The CURRENT attempt is the last
    # one in the list and gets no prior score (the judge call itself will
    # produce it). So prior_scores is applied to attempts[:-1].
    attempts_list = [
        {"attempt_id": i + 1, "provider": "x"} for i in range(attempts)
    ]
    if prior_scores:
        # Score the first len(prior_scores) attempts, leave the last attempt
        # unscored (it's the one about to be judged).
        to_score = attempts_list[: len(prior_scores)]
        for a, s in zip(to_score, prior_scores):
            a["judge_score"] = s
    return {
        "shot": {
            "shot_id": "sh_001",
            "description": "Test shot.",
            "duration_s": 3.0,
            "prompt": {"primary": primary, "negative": negative},
            "continuity_refs": continuity_refs or [],
            "attempts": attempts_list,
            "artistic_direction": artistic_direction,
        },
        "render_path": str(render_path),
        "brief": {"tone": brief_tone or ["quiet"], "artistic_style": "handheld"},
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_evenly_spaced_fractions() -> None:
    assert _evenly_spaced_fractions(1) == [0.5]
    assert _evenly_spaced_fractions(2) == [0.3333, 0.6667]
    assert _evenly_spaced_fractions(3) == [0.25, 0.5, 0.75]


def test_compute_judge_score_no_artifact() -> None:
    s = _compute_judge_score(
        {"composition": 0.8, "prompt_adherence": 0.7, "continuity": 0.9, "artifact_flag": False}
    )
    assert abs(s - 0.8) < 1e-6


def test_compute_judge_score_artifact_penalty() -> None:
    s = _compute_judge_score(
        {"composition": 0.9, "prompt_adherence": 0.9, "continuity": 0.9, "artifact_flag": True}
    )
    assert abs(s - (0.9 - ARTIFACT_PENALTY)) < 1e-6


def test_decide_outcome_branches() -> None:
    # Return tuple shape: (outcome, rejection_reason, escalation_reason)
    assert _decide_outcome(judge_score=0.9, artifact_flag=False, attempts_count=1) == (
        "approved",
        None,
        None,
    )
    # Artifact forces rejection even above approve threshold
    outcome, rej, esc = _decide_outcome(
        judge_score=0.9, artifact_flag=True, attempts_count=1
    )
    assert outcome == "rejected" and rej == "artifact" and esc is None
    # Mid-range score, early attempt: rejected/auto_judge (retry possible)
    outcome, rej, esc = _decide_outcome(
        judge_score=0.6, artifact_flag=False, attempts_count=1
    )
    assert outcome == "rejected" and rej == "auto_judge" and esc is None
    # Below escalate threshold: escalated/below_threshold
    outcome, rej, esc = _decide_outcome(
        judge_score=0.2, artifact_flag=False, attempts_count=1
    )
    assert outcome == "escalated" and rej is None and esc == "below_threshold"
    # Max attempts + NON-approving score: escalated/max_attempts_exhausted
    outcome, rej, esc = _decide_outcome(
        judge_score=0.6,
        artifact_flag=False,
        attempts_count=MAX_ATTEMPTS_BEFORE_ESCALATE,
    )
    assert outcome == "escalated" and esc == "max_attempts_exhausted"
    # Max attempts + stable approving score: APPROVED (retry count doesn't
    # override a legitimate pass; that's the bug the v3 A/B surfaced)
    outcome, rej, esc = _decide_outcome(
        judge_score=0.9,
        artifact_flag=False,
        attempts_count=MAX_ATTEMPTS_BEFORE_ESCALATE,
        prior_scores=[0.85, 0.88],
    )
    assert outcome == "approved" and rej is None and esc is None
    # Max attempts + volatile approving score: escalated/volatile_scores
    outcome, rej, esc = _decide_outcome(
        judge_score=0.83,
        artifact_flag=False,
        attempts_count=MAX_ATTEMPTS_BEFORE_ESCALATE,
        prior_scores=[0.78, 0.67],
    )
    assert outcome == "escalated" and esc == "volatile_scores"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_vision_mode_approved(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "composition": 0.82,
            "prompt_adherence": 0.85,
            "continuity": 0.90,
            "artifact_flag": False,
            "judge_notes": "Clean framing, horizon level, subject reads.",
            "feedback": [
                {
                    "feedback_type": "composition",
                    "severity": "note",
                    "observation": "rule of thirds hit",
                }
            ],
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",  # skip reading the real prompt file
    )
    result = tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))

    assert result["outcome"] == "approved"
    assert result["rejection_reason"] is None
    assert result["mode"] == "vision"
    assert result["judge_score"] >= APPROVE_THRESHOLD
    assert result["composition"] == 0.82
    assert result["artifact_flag"] is False
    assert "horizon" in result["judge_notes"]
    assert result["feedback"][0]["feedback_type"] == "composition"


def test_vision_mode_rejected(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "composition": 0.55,
            "prompt_adherence": 0.60,
            "continuity": 0.70,
            "artifact_flag": False,
            "judge_notes": "Subject over-centered; lighting flat; reads as studio-test.",
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    result = tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))
    assert result["outcome"] == "rejected"
    assert result["rejection_reason"] == "auto_judge"
    assert "over-centered" in result["judge_notes"]


def test_vision_mode_artifact_forces_rejection(tmp_path: Path) -> None:
    # Scores above threshold but artifact_flag=True should reject with
    # rejection_reason="artifact".
    response = json.dumps(
        {
            "composition": 0.85,
            "prompt_adherence": 0.90,
            "continuity": 0.95,
            "artifact_flag": True,
            "judge_notes": "Subject has a second arm in frame at 1.2s.",
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    result = tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))
    assert result["outcome"] == "rejected"
    assert result["rejection_reason"] == "artifact"
    # Judge score includes penalty
    expected = (0.85 + 0.90 + 0.95) / 3 - ARTIFACT_PENALTY
    assert abs(result["judge_score"] - round(expected, 4)) < 1e-4


def test_vision_mode_escalated_below_floor(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "composition": 0.25,
            "prompt_adherence": 0.25,
            "continuity": 0.25,
            "artifact_flag": False,
            "judge_notes": "Did not render the described scene.",
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    result = tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))
    assert result["outcome"] == "escalated"


def test_attempts_cap_with_stable_good_score_approves(tmp_path: Path) -> None:
    """Max-attempts on a STABLE, approving score is approved — retry count
    shouldn't override a legitimate pass. This is the behavior change the
    sh_005 v3 A/B surfaced.
    """
    response = json.dumps(
        {
            "composition": 0.85,
            "prompt_adherence": 0.80,
            "continuity": 0.90,
            "artifact_flag": False,
            "judge_notes": "Looks fine and trajectory is stable.",
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    # Prior scores are clustered (stable); current is a pass. No volatility.
    result = tool(
        shot_id="sh_001",
        payload=_ctx(
            _make_render(tmp_path),
            attempts=MAX_ATTEMPTS_BEFORE_ESCALATE,
            prior_scores=[0.82, 0.85],
        ),
    )
    assert result["outcome"] == "approved"
    assert result["escalation_reason"] is None


def test_attempts_cap_with_volatile_good_score_escalates(tmp_path: Path) -> None:
    """Max-attempts + volatile trajectory + approving current score triggers
    volatile_scores escalation. Pipeline produced this take but not reliably."""
    response = json.dumps(
        {
            "composition": 0.85,
            "prompt_adherence": 0.80,
            "continuity": 0.90,
            "artifact_flag": False,
            "judge_notes": "Looks fine; earlier attempts were much weaker.",
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    # Mirrors sh_005's actual v1/v2/v3 trajectory: 0.78, 0.67, ~0.83.
    result = tool(
        shot_id="sh_001",
        payload=_ctx(
            _make_render(tmp_path),
            attempts=MAX_ATTEMPTS_BEFORE_ESCALATE,
            prior_scores=[0.78, 0.67],
        ),
    )
    assert result["outcome"] == "escalated"
    assert result["escalation_reason"] == "volatile_scores"


def test_attempts_cap_with_mid_score_escalates_max_attempts(tmp_path: Path) -> None:
    """Max-attempts + NON-approving score = max_attempts_exhausted."""
    response = json.dumps(
        {
            "composition": 0.60,
            "prompt_adherence": 0.60,
            "continuity": 0.70,
            "artifact_flag": False,
            "judge_notes": "Still not landing after three tries.",
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    result = tool(
        shot_id="sh_001",
        payload=_ctx(
            _make_render(tmp_path),
            attempts=MAX_ATTEMPTS_BEFORE_ESCALATE,
            prior_scores=[0.58, 0.62],
        ),
    )
    assert result["outcome"] == "escalated"
    assert result["escalation_reason"] == "max_attempts_exhausted"


# ---------------------------------------------------------------------------
# Text-adherence fallback
# ---------------------------------------------------------------------------


def test_text_adherence_when_ffmpeg_missing(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "composition": 0.5,  # model might return anything; we force 0.7 neutral
            "prompt_adherence": 0.8,
            "continuity": 1.0,
            "artifact_flag": True,  # should be forced to False in text mode
            "judge_notes": "Cannot assess composition; prompt describes a beach; plausible.",
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin=None,  # no ffmpeg
        system_prompt="SYSTEM",
    )
    result = tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))
    assert result["mode"] == "text-adherence"
    # Module enforces neutral composition + no artifact in text-adherence mode
    # when the model's raw value is below the neutral default.
    assert result["artifact_flag"] is False


def test_text_adherence_respects_prompt_adherence(tmp_path: Path) -> None:
    """In text mode, a low prompt_adherence should still push toward rejection."""
    response = json.dumps(
        {
            "composition": 0.7,
            "prompt_adherence": 0.3,
            "continuity": 1.0,
            "artifact_flag": False,
            "judge_notes": "Caption describes a city; prompt asked for a beach.",
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin=None,
        system_prompt="SYSTEM",
    )
    result = tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))
    # mean = (0.7 + 0.3 + 1.0) / 3 = 0.667 → rejected
    assert result["outcome"] == "rejected"


# ---------------------------------------------------------------------------
# Contract 2 safety: rejected outcome must carry non-empty notes
# ---------------------------------------------------------------------------


def test_rejected_with_empty_notes_gets_backfilled(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "composition": 0.55,
            "prompt_adherence": 0.55,
            "continuity": 0.55,
            "artifact_flag": False,
            "judge_notes": "",  # empty — would violate Contract 2 on revision
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    result = tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))
    assert result["outcome"] == "rejected"
    # Backfilled, non-empty — PromptSmith revision can now proceed
    assert result["judge_notes"]
    assert "empty judge_notes" in result["judge_notes"]


def test_escalated_also_gets_synthetic_notes(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "composition": 0.1,
            "prompt_adherence": 0.1,
            "continuity": 0.1,
            "artifact_flag": False,
            "judge_notes": "",
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    result = tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))
    assert result["outcome"] == "escalated"
    assert result["judge_notes"]


# ---------------------------------------------------------------------------
# JSON parsing + retry
# ---------------------------------------------------------------------------


def test_json_retry_recovers_after_malformed_first_response(tmp_path: Path) -> None:
    malformed = "Here's my judgment: the shot looks OK."
    good = json.dumps(
        {
            "composition": 0.8,
            "prompt_adherence": 0.8,
            "continuity": 0.8,
            "artifact_flag": False,
            "judge_notes": "All good.",
        }
    )
    client = StubClient(responses=[malformed, good])
    tool = ShotJudgeTool(
        client=client,
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    result = tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))
    assert result["outcome"] == "approved"
    assert len(client.calls) == 2
    # Second call should have included the reminder text block
    reminder_text = " ".join(
        b.get("text", "")
        for b in client.calls[1]["messages"][0]["content"]
        if isinstance(b, dict) and b.get("type") == "text"
    )
    assert "STRICT JSON" in reminder_text


def test_json_still_malformed_raises(tmp_path: Path) -> None:
    bad = "not json"
    client = StubClient(responses=[bad, bad])
    tool = ShotJudgeTool(
        client=client,
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    with pytest.raises(RuntimeError, match="unparseable"):
        tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))


def test_tolerates_fenced_json(tmp_path: Path) -> None:
    response = (
        "I'll judge this shot.\n"
        "```json\n"
        '{"composition": 0.8, "prompt_adherence": 0.8, "continuity": 0.8, '
        '"artifact_flag": false, "judge_notes": "ok"}\n'
        "```\n"
    )
    tool = ShotJudgeTool(
        client=StubClient(responses=[response]),
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    result = tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))
    assert result["outcome"] == "approved"


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


def test_missing_shot_id_raises(tmp_path: Path) -> None:
    tool = ShotJudgeTool(
        client=StubClient(responses=["{}"]),
        system_prompt="SYSTEM",
    )
    with pytest.raises(ValueError, match="shot_id"):
        tool(shot_id=None, payload=_ctx(_make_render(tmp_path)))


def test_missing_render_raises(tmp_path: Path) -> None:
    tool = ShotJudgeTool(
        client=StubClient(responses=["{}"]),
        system_prompt="SYSTEM",
    )
    payload = _ctx(_make_render(tmp_path))
    payload["render_path"] = str(tmp_path / "nope.mp4")
    with pytest.raises(FileNotFoundError):
        tool(shot_id="sh_001", payload=payload)


# ---------------------------------------------------------------------------
# Multi-image payload shape
# ---------------------------------------------------------------------------


def test_vision_payload_attaches_keyframes(tmp_path: Path) -> None:
    """Verify the messages we send include `type: image` blocks for each frame."""
    response = json.dumps(
        {
            "composition": 0.8,
            "prompt_adherence": 0.8,
            "continuity": 0.8,
            "artifact_flag": False,
            "judge_notes": "ok",
        }
    )
    client = StubClient(responses=[response])
    tool = ShotJudgeTool(
        client=client,
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))

    content = client.calls[0]["messages"][0]["content"]
    image_blocks = [b for b in content if b.get("type") == "image"]
    assert len(image_blocks) == 3
    for b in image_blocks:
        assert b["source"]["type"] == "base64"
        assert b["source"]["media_type"] == "image/jpeg"
        assert b["source"]["data"]  # non-empty base64


def test_usage_passed_through(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "composition": 0.8,
            "prompt_adherence": 0.8,
            "continuity": 0.8,
            "artifact_flag": False,
            "judge_notes": "ok",
        }
    )
    tool = ShotJudgeTool(
        client=StubClient(
            responses=[response],
            usage={"input_tokens": 5000, "output_tokens": 120, "cache_read_input_tokens": 4000},
        ),
        ffmpeg_bin="/fake/ffmpeg",
        extract_frames=_inject_frames(3),
        system_prompt="SYSTEM",
    )
    result = tool(shot_id="sh_001", payload=_ctx(_make_render(tmp_path)))
    assert result["usage"]["input_tokens"] == 5000
    assert result["usage"]["cache_read_input_tokens"] == 4000
