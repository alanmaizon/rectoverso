"""Unit tests for src.producer.seedance.SeedanceRendererTool.

Hermetic: inject fake urlopen + sleep. Pattern mirrors test_kling.py but
exercises the Seedance-specific gotchas: duration-as-string, no
negative_prompt field, generate_audio=False, end_image_url, multiple variants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.producer.seedance import (
    MAX_DURATION_S,
    MIN_DURATION_S,
    PROVIDER_FAST_I2V,
    PROVIDER_FAST_T2V,
    PROVIDER_PRO_I2V,
    PROVIDER_PRO_REF,
    PROVIDER_PRO_T2V,
    SUPPORTED_ASPECTS,
    SUPPORTED_RESOLUTIONS,
    SeedanceRendererTool,
    _clamp_duration,
    _cost_for,
    _mode_from_model,
    _provider_from_model,
)


from tests.producer._fakes import (
    FakeResponse as _FakeResponse,
    http_error as _http_error,
    json_response as _json_response,
    make_urlopen as _make_urlopen,
)


# ---------------------------------------------------------------------------
# Constants + helpers
# ---------------------------------------------------------------------------


MODEL_PRO_I2V = "bytedance/seedance-2.0/image-to-video"
MODEL_PRO_T2V = "bytedance/seedance-2.0/text-to-video"
MODEL_PRO_REF = "bytedance/seedance-2.0/reference-to-video"
MODEL_FAST_I2V = "bytedance/seedance-2.0/fast/image-to-video"


def _payload(
    tmp_path: Path,
    *,
    attempt_id: int = 1,
    duration_s: int = 5,
    model: str = MODEL_PRO_I2V,
    image_url: str | None = "https://cdn.example.com/ref.jpg",
    reference_image_urls: list[str] | None = None,
    end_image_url: str | None = None,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    seed: int | None = None,
    generate_audio: bool | None = None,
) -> dict:
    p: dict[str, Any] = {
        "model": model,
        "prompt": "slow push-in, mist lifting off the water",
        "duration_s": duration_s,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "output_dir": tmp_path / "sh_001",
        "attempt_id": attempt_id,
    }
    if image_url is not None:
        p["image_url"] = image_url
    if reference_image_urls is not None:
        p["reference_image_urls"] = reference_image_urls
    if end_image_url is not None:
        p["end_image_url"] = end_image_url
    if seed is not None:
        p["seed"] = seed
    if generate_audio is not None:
        p["generate_audio"] = generate_audio
    return p


def _submit_response(request_id: str = "req_seed_1") -> dict:
    base = f"https://queue.fal.run/{MODEL_PRO_I2V}/requests/{request_id}"
    return {
        "request_id": request_id,
        "status_url": f"{base}/status",
        "response_url": base,
        "cancel_url": f"{base}/cancel",
        "queue_position": 0,
    }


# ---------------------------------------------------------------------------
# Happy path — I2V
# ---------------------------------------------------------------------------


def test_happy_path_i2v(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "IN_PROGRESS"}),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/seed.mp4"}}),
        lambda req: _FakeResponse(b"\x00" * 4096),
    ]
    tool = SeedanceRendererTool(
        api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None
    )
    result = tool("sh_001", _payload(tmp_path, duration_s=5))

    assert result["status"] == "ok"
    assert result["provider"] == PROVIDER_PRO_I2V
    assert result["model"] == MODEL_PRO_I2V
    assert result["task_id"] == "req_seed_1"
    assert result["render_path"].endswith("sh_001/v1.mp4")
    assert result["output_size_bytes"] == 4096
    # $0.3024/s * 5s * 1.0 (720p) = $1.512
    assert result["cost_usd"] == 1.512
    assert result["actual_duration_s"] == 5
    assert result["aspect_ratio"] == "16:9"
    assert result["resolution"] == "720p"


def test_submit_body_shape_i2v(tmp_path: Path) -> None:
    """Locks in the Seedance-specific gotchas: duration-as-string,
    no negative_prompt, generate_audio defaults False."""
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/s.mp4"}}),
        lambda req: _FakeResponse(b"\x01" * 128),
    ]
    fake = _make_urlopen(plan)
    tool = SeedanceRendererTool(api_key="sk", urlopen=fake, sleep=lambda _: None)
    tool(
        "sh_001",
        _payload(
            tmp_path, duration_s=8, aspect_ratio="9:16", resolution="1080p", seed=42
        ),
    )

    submit = fake.calls[0]
    assert submit["method"] == "POST"
    assert submit["url"] == f"https://queue.fal.run/{MODEL_PRO_I2V}"
    assert submit["headers"].get("Authorization") == "Key sk"  # NOT "Bearer"

    body = submit["body"]
    assert body["prompt"].startswith("slow push-in")
    assert body["image_url"] == "https://cdn.example.com/ref.jpg"
    # Duration must be a STRING — sending int 422s on Seedance
    assert body["duration"] == "8"
    assert isinstance(body["duration"], str)
    assert body["resolution"] == "1080p"
    assert body["aspect_ratio"] == "9:16"
    # Adapter forces audio off by default so ElevenLabs owns the track
    assert body["generate_audio"] is False
    assert body["seed"] == 42
    # Seedance has NO negative_prompt field — must not be sent
    assert "negative_prompt" not in body
    # No cfg_scale either
    assert "cfg_scale" not in body


def test_generate_audio_override(tmp_path: Path) -> None:
    """Operator can opt in to Seedance-generated audio by passing the flag."""
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/x.mp4"}}),
        lambda req: _FakeResponse(b"\x00" * 16),
    ]
    fake = _make_urlopen(plan)
    tool = SeedanceRendererTool(api_key="sk", urlopen=fake, sleep=lambda _: None)
    tool("sh_001", _payload(tmp_path, generate_audio=True))
    assert fake.calls[0]["body"]["generate_audio"] is True


def test_end_image_url_supported(tmp_path: Path) -> None:
    """Seedance supports first+last frame control on every variant via end_image_url."""
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/x.mp4"}}),
        lambda req: _FakeResponse(b"\x00" * 16),
    ]
    fake = _make_urlopen(plan)
    tool = SeedanceRendererTool(api_key="sk", urlopen=fake, sleep=lambda _: None)
    tool("sh_001", _payload(tmp_path, end_image_url="https://cdn/end.jpg"))
    assert fake.calls[0]["body"]["end_image_url"] == "https://cdn/end.jpg"


# ---------------------------------------------------------------------------
# T2V variant
# ---------------------------------------------------------------------------


def test_t2v_omits_image_url(tmp_path: Path) -> None:
    """Text-to-video must not carry an image_url — would 422 if present."""
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/t2v.mp4"}}),
        lambda req: _FakeResponse(b"\x00" * 32),
    ]
    fake = _make_urlopen(plan)
    tool = SeedanceRendererTool(api_key="sk", urlopen=fake, sleep=lambda _: None)
    tool("sh_001", _payload(tmp_path, model=MODEL_PRO_T2V, image_url=None))

    body = fake.calls[0]["body"]
    assert "image_url" not in body
    assert body["prompt"].startswith("slow push-in")


# ---------------------------------------------------------------------------
# Reference-to-video variant
# ---------------------------------------------------------------------------


def test_ref_variant_requires_reference_list(tmp_path: Path) -> None:
    tool = SeedanceRendererTool(api_key="sk", urlopen=_make_urlopen([]), sleep=lambda _: None)
    with pytest.raises(ValueError, match="reference_image_urls"):
        tool("sh_001", _payload(tmp_path, model=MODEL_PRO_REF, image_url=None))


def test_ref_variant_sends_list(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/ref.mp4"}}),
        lambda req: _FakeResponse(b"\x00" * 64),
    ]
    fake = _make_urlopen(plan)
    tool = SeedanceRendererTool(api_key="sk", urlopen=fake, sleep=lambda _: None)
    refs = ["https://a.com/1.jpg", "https://a.com/2.jpg"]
    tool(
        "sh_001",
        _payload(tmp_path, model=MODEL_PRO_REF, image_url=None, reference_image_urls=refs),
    )
    body = fake.calls[0]["body"]
    assert body["reference_image_urls"] == refs


# ---------------------------------------------------------------------------
# Duration / validation
# ---------------------------------------------------------------------------


def test_duration_clamp_low_and_high(tmp_path: Path) -> None:
    """Seedance accepts 4-15s; adapter clamps out-of-range values into it."""
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/x.mp4"}}),
        lambda req: _FakeResponse(b"\x00" * 16),
    ]
    fake = _make_urlopen(plan)
    tool = SeedanceRendererTool(api_key="sk", urlopen=fake, sleep=lambda _: None)
    # Request 2s (< MIN) — clamps to 4
    result = tool("sh_001", _payload(tmp_path, duration_s=2))
    assert result["actual_duration_s"] == 4
    assert fake.calls[0]["body"]["duration"] == "4"


def test_duration_clamp_above_max(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/x.mp4"}}),
        lambda req: _FakeResponse(b"\x00" * 16),
    ]
    fake = _make_urlopen(plan)
    tool = SeedanceRendererTool(api_key="sk", urlopen=fake, sleep=lambda _: None)
    # Request 20s — clamps to 15 (MAX)
    result = tool("sh_001", _payload(tmp_path, duration_s=20))
    assert result["actual_duration_s"] == 15
    assert fake.calls[0]["body"]["duration"] == "15"


def test_unsupported_aspect_ratio_raises(tmp_path: Path) -> None:
    tool = SeedanceRendererTool(api_key="sk", urlopen=_make_urlopen([]), sleep=lambda _: None)
    with pytest.raises(ValueError, match="aspect_ratio"):
        tool("sh_001", _payload(tmp_path, aspect_ratio="2:1"))


def test_unsupported_resolution_raises(tmp_path: Path) -> None:
    tool = SeedanceRendererTool(api_key="sk", urlopen=_make_urlopen([]), sleep=lambda _: None)
    with pytest.raises(ValueError, match="resolution"):
        tool("sh_001", _payload(tmp_path, resolution="4k"))


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_missing_image_url_for_i2v(tmp_path: Path) -> None:
    tool = SeedanceRendererTool(api_key="sk", urlopen=_make_urlopen([]), sleep=lambda _: None)
    with pytest.raises(ValueError, match="image-to-video"):
        tool("sh_001", _payload(tmp_path, image_url=None))


def test_missing_api_key_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.producer.seedance.resolve_fal_keys", lambda *_a, **_k: (None, None))
    tool = SeedanceRendererTool(urlopen=_make_urlopen([]), sleep=lambda _: None)
    with pytest.raises(RuntimeError, match="FAL_KEY"):
        tool("sh_001", _payload(tmp_path))


def test_content_policy_422_is_terminal(tmp_path: Path) -> None:
    body = json.dumps({
        "detail": [{
            "loc": ["body", "prompt"],
            "msg": "blocked",
            "type": "content_policy_violation",
        }]
    })

    def _responder(req):
        raise _http_error(422, body)

    tool = SeedanceRendererTool(
        api_key="sk", urlopen=_make_urlopen([_responder]), sleep=lambda _: None
    )
    result = tool("sh_001", _payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "content_policy"


def test_poll_FAILED_status(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "FAILED", "error": "runner crashed"}),
    ]
    tool = SeedanceRendererTool(
        api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None
    )
    result = tool("sh_001", _payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "poll:FAILED"


def test_zero_byte_download(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/empty.mp4"}}),
        lambda req: _FakeResponse(b""),
    ]
    tool = SeedanceRendererTool(
        api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None
    )
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "download:empty"


def test_result_missing_video_url(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {}}),
    ]
    tool = SeedanceRendererTool(
        api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None
    )
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "result_malformed"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_clamp_duration() -> None:
    assert _clamp_duration(1) == MIN_DURATION_S
    assert _clamp_duration(4) == 4
    assert _clamp_duration(5) == 5
    assert _clamp_duration(15) == 15
    assert _clamp_duration(20) == MAX_DURATION_S


def test_mode_from_model() -> None:
    assert _mode_from_model(MODEL_PRO_I2V) == "i2v"
    assert _mode_from_model(MODEL_PRO_T2V) == "t2v"
    assert _mode_from_model(MODEL_PRO_REF) == "ref"
    assert _mode_from_model(MODEL_FAST_I2V) == "i2v"


def test_cost_for_pro_tier() -> None:
    # $0.3024/s * 5s * 1.0 = $1.512 at 720p
    assert _cost_for(MODEL_PRO_I2V, 5, "720p") == 1.512
    # 1080p multiplier 2.25
    assert _cost_for(MODEL_PRO_I2V, 5, "1080p") == round(1.512 * 2.25, 4)
    # 480p multiplier 0.44
    assert _cost_for(MODEL_PRO_I2V, 5, "480p") == round(1.512 * 0.44, 4)


def test_cost_for_fast_tier() -> None:
    # Fast I2V: $0.2419/s
    assert _cost_for(MODEL_FAST_I2V, 5, "720p") == round(0.2419 * 5, 4)


def test_provider_from_model_slots() -> None:
    assert _provider_from_model(MODEL_PRO_I2V) == PROVIDER_PRO_I2V
    assert _provider_from_model(MODEL_PRO_T2V) == PROVIDER_PRO_T2V
    assert _provider_from_model(MODEL_PRO_REF) == PROVIDER_PRO_REF
    assert _provider_from_model(MODEL_FAST_I2V) == PROVIDER_FAST_I2V


def test_supported_aspects_and_resolutions_frozen() -> None:
    # Lock in the set of accepted values so accidental YAML edits surface here.
    assert set(SUPPORTED_ASPECTS) == {
        "auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16",
    }
    assert set(SUPPORTED_RESOLUTIONS) == {"480p", "720p", "1080p"}
