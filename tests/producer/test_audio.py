"""Unit tests for src.producer.audio.ElevenLabsAudioTool.

Hermetic: inject fake urlopen. ElevenLabs returns audio bytes directly in
the response body — no polling, no queue. Tests construct FakeResponse
instances carrying the byte payload and assert the adapter:

    1. Writes the bytes to the expected path
    2. Reports credit cost matching the published rate
    3. Classifies HTTP errors into the right `failure_stage`
    4. Projects a result dict the manifest audio.dialogue[]/audio.sfx[]
       schema can consume directly

The SFX path is the primary focus (first-class per scope) with TTS covered
for the one dialogue shot in the Earth Day brief.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.producer.audio import (
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_SFX_DURATION_S,
    DEFAULT_SFX_PROMPT_INFLUENCE,
    DEFAULT_TTS_MODEL_ID,
    ElevenLabsAudioTool,
    SFX_MODEL_ID,
    VOICE_PRESETS,
    _classify_http_failure,
    _estimate_sfx_credits,
    _estimate_tts_credits,
)

from tests.producer._fakes import (
    FakeResponse as _FakeResponse,
    http_error as _http_error,
    make_urlopen as _make_urlopen,
)


# ---------------------------------------------------------------------------
# SFX path — primary
# ---------------------------------------------------------------------------


def test_sfx_happy_path_writes_bytes_and_returns_ok(tmp_path: Path) -> None:
    """ElevenLabs returns raw MP3 bytes in the response body. Adapter writes
    them to disk, stamps md5, reports credits."""
    mp3 = b"ID3\x03\x00\x00\x00" + b"\x00" * 1024  # fake MP3 header + padding
    plan = [lambda req: _FakeResponse(mp3)]
    tool = ElevenLabsAudioTool(api_key="xi-test", urlopen=_make_urlopen(plan))

    result = tool(
        "sh_005",
        {
            "mode": "sfx",
            "sfx_id": "ice_crack",
            "description": "the sharp crack of lake ice fracturing",
            "duration_s": 2.0,
            "output_dir": tmp_path / "audio",
            "attempt_id": 1,
        },
    )

    assert result["status"] == "ok"
    assert result["provider"] == "elevenlabs"
    assert result["mode"] == "sfx"
    assert result["shot_id"] == "sh_005"
    assert result["sfx_id"] == "ice_crack"
    assert result["audio_path"].endswith("sh_005_ice_crack_v1.mp3")
    assert result["output_size_bytes"] == len(mp3)
    assert result["duration_s"] == 2.0
    # 2s * 40 credits/s rounded up = 80
    assert result["quota_cost"] == 80
    assert result["cost_usd"] == 0.0
    # Verify file is on disk with the expected bytes
    assert Path(result["audio_path"]).read_bytes() == mp3


def test_sfx_body_shape_and_url(tmp_path: Path) -> None:
    """Lock in the API shape: xi-api-key header, model_id + prompt_influence
    in body, output_format as query param, duration_seconds specified."""
    plan = [lambda req: _FakeResponse(b"fake_mp3")]
    fake = _make_urlopen(plan)
    tool = ElevenLabsAudioTool(api_key="xi-test", urlopen=fake)

    tool(
        "sh_005",
        {
            "mode": "sfx",
            "sfx_id": "fox_breath",
            "description": "a single soft exhale of a fox, close microphone, isolated",
            "duration_s": 1.5,
            "prompt_influence": 0.7,
            "output_dir": tmp_path / "audio",
            "attempt_id": 1,
        },
    )

    call = fake.calls[0]
    # URL carries output_format query string
    assert "/v1/sound-generation?output_format=" in call["url"]
    assert DEFAULT_OUTPUT_FORMAT in call["url"]
    # Auth header is xi-api-key (NOT Bearer, NOT Authorization)
    headers_lower = {k.lower(): v for k, v in call["headers"].items()}
    assert headers_lower.get("xi-api-key") == "xi-test"
    assert "authorization" not in headers_lower
    # Body fields
    body = call["body"]
    assert body["text"].startswith("a single soft exhale")
    assert body["duration_seconds"] == 1.5
    assert body["prompt_influence"] == 0.7
    assert body["model_id"] == SFX_MODEL_ID
    assert body["loop"] is False


def test_sfx_default_duration_is_3s(tmp_path: Path) -> None:
    """Adapter always specifies duration_seconds for budget determinism —
    auto-duration is 100 flat credits even for short clips."""
    plan = [lambda req: _FakeResponse(b"fake")]
    fake = _make_urlopen(plan)
    tool = ElevenLabsAudioTool(api_key="xi", urlopen=fake)

    tool("sh_001", {
        "mode": "sfx",
        "description": "wind through bare branches",
        "output_dir": tmp_path / "audio",
        "attempt_id": 1,
    })

    body = fake.calls[0]["body"]
    assert body["duration_seconds"] == DEFAULT_SFX_DURATION_S


def test_sfx_explicit_none_duration_opts_into_auto(tmp_path: Path) -> None:
    """Passing duration_s=None explicitly means 'let ElevenLabs pick' — flat
    100 credit cost. Distinguishes 'omit duration_seconds from body' from
    'send default 3s'."""
    plan = [lambda req: _FakeResponse(b"fake")]
    fake = _make_urlopen(plan)
    tool = ElevenLabsAudioTool(api_key="xi", urlopen=fake)

    result = tool("sh_001", {
        "mode": "sfx",
        "description": "generic ambience",
        "duration_s": None,
        "output_dir": tmp_path / "audio",
        "attempt_id": 1,
    })

    body = fake.calls[0]["body"]
    assert "duration_seconds" not in body
    # Auto-duration flat 100 credits
    assert result["quota_cost"] == 100


def test_sfx_rejects_duration_out_of_range(tmp_path: Path) -> None:
    tool = ElevenLabsAudioTool(api_key="xi", urlopen=_make_urlopen([]))
    # Below min (0.5s)
    with pytest.raises(ValueError, match="duration_s"):
        tool("sh_001", {
            "mode": "sfx",
            "description": "x",
            "duration_s": 0.1,
            "output_dir": tmp_path / "audio",
            "attempt_id": 1,
        })
    # Above max (30s)
    with pytest.raises(ValueError, match="duration_s"):
        tool("sh_001", {
            "mode": "sfx",
            "description": "x",
            "duration_s": 35.0,
            "output_dir": tmp_path / "audio",
            "attempt_id": 1,
        })


def test_sfx_rejects_empty_description(tmp_path: Path) -> None:
    tool = ElevenLabsAudioTool(api_key="xi", urlopen=_make_urlopen([]))
    with pytest.raises(ValueError, match="description"):
        tool("sh_001", {
            "mode": "sfx",
            "description": "   ",
            "output_dir": tmp_path / "audio",
            "attempt_id": 1,
        })


def test_sfx_quota_exhausted_maps_to_failure_stage(tmp_path: Path) -> None:
    """402 / quota-mentioning body → failure_stage='quota_exhausted'."""
    def _responder(req):
        raise _http_error(
            402,
            json.dumps({"detail": "You have exceeded your credits quota"}),
        )
    tool = ElevenLabsAudioTool(
        api_key="xi", urlopen=_make_urlopen([_responder])
    )
    result = tool("sh_001", {
        "mode": "sfx",
        "description": "x",
        "duration_s": 1.0,
        "output_dir": tmp_path / "audio",
        "attempt_id": 1,
    })
    assert result["status"] == "failed"
    assert result["failure_stage"] == "quota_exhausted"


def test_sfx_empty_response_is_failure(tmp_path: Path) -> None:
    """Zero-byte audio response → failure. Guards against silent truncation."""
    plan = [lambda req: _FakeResponse(b"")]
    tool = ElevenLabsAudioTool(api_key="xi", urlopen=_make_urlopen(plan))
    result = tool("sh_001", {
        "mode": "sfx",
        "description": "x",
        "duration_s": 1.0,
        "output_dir": tmp_path / "audio",
        "attempt_id": 1,
    })
    assert result["status"] == "failed"
    assert result["failure_stage"] == "empty_response"


# ---------------------------------------------------------------------------
# TTS path — secondary
# ---------------------------------------------------------------------------


def test_tts_happy_path(tmp_path: Path) -> None:
    mp3 = b"ID3\x03" + b"\x00" * 256
    plan = [lambda req: _FakeResponse(mp3)]
    tool = ElevenLabsAudioTool(api_key="xi", urlopen=_make_urlopen(plan))

    result = tool(
        "sh_006",
        {
            "mode": "tts",
            "line_id": "l1",
            "text": "Mama, look.",
            "voice_id": VOICE_PRESETS["gigi"],
            "output_dir": tmp_path / "audio",
            "attempt_id": 1,
        },
    )

    assert result["status"] == "ok"
    assert result["mode"] == "tts"
    assert result["text"] == "Mama, look."
    assert result["model_id"] == DEFAULT_TTS_MODEL_ID
    # 11 chars * 1 credit (v3 rate) = 11
    assert result["quota_cost"] == 11
    assert result["audio_path"].endswith("sh_006_l1_v1.mp3")


def test_tts_url_embeds_voice_id(tmp_path: Path) -> None:
    """TTS URL is /v1/text-to-speech/{voice_id}?output_format=..."""
    plan = [lambda req: _FakeResponse(b"audio")]
    fake = _make_urlopen(plan)
    tool = ElevenLabsAudioTool(api_key="xi", urlopen=fake)

    tool("sh_006", {
        "mode": "tts",
        "text": "Mama, look.",
        "voice_id": "voice_abc",
        "output_dir": tmp_path / "audio",
        "attempt_id": 1,
    })

    call = fake.calls[0]
    assert "/v1/text-to-speech/voice_abc?output_format=" in call["url"]


def test_tts_body_shape_includes_voice_settings_and_defaults_to_v3(tmp_path: Path) -> None:
    plan = [lambda req: _FakeResponse(b"audio")]
    fake = _make_urlopen(plan)
    tool = ElevenLabsAudioTool(api_key="xi", urlopen=fake)

    tool("sh_006", {
        "mode": "tts",
        "text": "Mama, look.",
        "voice_id": VOICE_PRESETS["gigi"],
        "output_dir": tmp_path / "audio",
        "attempt_id": 1,
    })

    body = fake.calls[0]["body"]
    assert body["text"] == "Mama, look."
    assert body["model_id"] == "eleven_v3"
    vs = body["voice_settings"]
    assert vs["stability"] == 0.55
    assert vs["similarity_boost"] == 0.75
    assert vs["style"] == 0.0
    assert vs["use_speaker_boost"] is True


def test_tts_seed_and_language_code_passthrough(tmp_path: Path) -> None:
    """Seed makes TTS reproducible — required for snapshot tests and for
    re-doing a voiceover after an unrelated edit without re-diffing audio."""
    plan = [lambda req: _FakeResponse(b"audio")]
    fake = _make_urlopen(plan)
    tool = ElevenLabsAudioTool(api_key="xi", urlopen=fake)

    tool("sh_006", {
        "mode": "tts",
        "text": "Hello.",
        "voice_id": "v",
        "seed": 42,
        "language_code": "en",
        "output_dir": tmp_path / "audio",
        "attempt_id": 1,
    })

    body = fake.calls[0]["body"]
    assert body["seed"] == 42
    assert body["language_code"] == "en"


def test_tts_flash_model_halves_credit_estimate(tmp_path: Path) -> None:
    plan = [lambda req: _FakeResponse(b"audio")]
    tool = ElevenLabsAudioTool(api_key="xi", urlopen=_make_urlopen(plan))

    result = tool("sh_006", {
        "mode": "tts",
        "text": "x" * 20,
        "voice_id": "v",
        "model_id": "eleven_flash_v2_5",
        "output_dir": tmp_path / "audio",
        "attempt_id": 1,
    })

    # 20 chars * 0.5 credits/char = 10
    assert result["quota_cost"] == 10


# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------


def test_requires_valid_mode(tmp_path: Path) -> None:
    tool = ElevenLabsAudioTool(api_key="xi", urlopen=_make_urlopen([]))
    with pytest.raises(ValueError, match="mode"):
        tool("sh_001", {
            "mode": "bogus",
            "output_dir": tmp_path / "audio",
            "attempt_id": 1,
        })


def test_missing_api_key_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "src.producer.audio._resolve_elevenlabs_key", lambda: None
    )
    tool = ElevenLabsAudioTool(urlopen=_make_urlopen([]))
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        tool("sh_001", {
            "mode": "sfx",
            "description": "x",
            "duration_s": 1.0,
            "output_dir": tmp_path / "audio",
            "attempt_id": 1,
        })


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_estimate_sfx_credits_specified_duration() -> None:
    # Documented: 40 credits/second rounded UP.
    assert _estimate_sfx_credits(1.0) == 40
    assert _estimate_sfx_credits(2.0) == 80
    assert _estimate_sfx_credits(2.5) == 100
    assert _estimate_sfx_credits(3.0) == 120
    # Fractional durations round up — matches ElevenLabs billing behavior.
    assert _estimate_sfx_credits(1.1) == 44
    # Invalid → 0
    assert _estimate_sfx_credits(0) == 0
    assert _estimate_sfx_credits(-1.0) == 0


def test_estimate_sfx_credits_auto_duration_is_flat() -> None:
    assert _estimate_sfx_credits(None) == 100


def test_estimate_tts_credits_per_model() -> None:
    # v3 = 1 credit/char
    assert _estimate_tts_credits("x" * 30, "eleven_v3") == 30
    assert _estimate_tts_credits("x" * 30, "eleven_multilingual_v2") == 30
    # flash = 0.5 credit/char, rounded up
    assert _estimate_tts_credits("x" * 30, "eleven_flash_v2_5") == 15
    assert _estimate_tts_credits("x" * 3, "eleven_flash_v2_5") == 2
    # Empty text still bills minimum 1 credit (guard against off-by-one)
    assert _estimate_tts_credits("", "eleven_v3") == 1


def test_classify_http_failure() -> None:
    assert _classify_http_failure(402, "quota exceeded") == "quota_exhausted"
    assert _classify_http_failure(400, "credits") == "quota_exhausted"
    assert _classify_http_failure(429, "slow down") == "rate_limit"
    assert _classify_http_failure(401, "bad key") == "auth"
    assert _classify_http_failure(403, "forbidden") == "auth"
    assert _classify_http_failure(400, "bad field") == "validation"
    assert _classify_http_failure(422, "malformed") == "content_policy"
    assert _classify_http_failure(500, "server error") == "submit:http_500"


def test_voice_presets_include_documentary_and_child_approx() -> None:
    # These IDs are pinned; changing them is a product decision, not a refactor.
    assert VOICE_PRESETS["gigi"] == "jBpfuIE2acCO8z3wKNLl"
    assert VOICE_PRESETS["narrator_m"] == "pqHfZKP75CvOlQylNhV4"
    assert VOICE_PRESETS["narrator_m2"] == "pNInz6obpgDQGcFmaJgB"
    assert VOICE_PRESETS["narrator_f"] == "EXAVITQu4vr4xnSDxMaL"
