"""Unit tests for src.producer.veo.VeoRendererTool.

Hermetic: inject fake urlopen + sleep + a stub token_provider so no network
and no google-auth are ever needed.

Intent:      tool result dict shape matches what render_cmd projects
Edge cases:  missing project id, failed auth, content policy (raiMediaFilteredCount),
             poll timeout, inline base64 decode, gs:// download, duration snap
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
from pathlib import Path
from typing import Any, Callable

import pytest

from src.producer.veo import (
    DEFAULT_LOCATION,
    DEFAULT_MODEL_ID,
    PROVIDER_ID,
    VALID_DURATIONS_S,
    VeoRendererTool,
    _build_poll_url,
    _build_submit_url,
    _cost_for,
    _snap_duration,
)


# ---------------------------------------------------------------------------
# Fake HTTP plumbing (mirrors test_renderer.py / test_kling.py)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int | None = None) -> bytes:
        if n is None:
            return self._body
        out, self._body = self._body[:n], self._body[n:]
        return out


def _json_response(obj: Any) -> _FakeResponse:
    return _FakeResponse(json.dumps(obj).encode("utf-8"))


def _http_error(status: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://us-central1-aiplatform.googleapis.com/",
        code=status,
        msg="Fake",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode("utf-8")),
    )


def _make_urlopen(plan: list[Callable[[Any], _FakeResponse]]) -> Callable:
    calls = []

    def _fake(req_or_url, timeout=None):
        if not plan:
            raise AssertionError("unexpected extra urlopen call")
        url = getattr(req_or_url, "full_url", None) or str(req_or_url)
        method = getattr(req_or_url, "method", "GET")
        headers = dict(getattr(req_or_url, "headers", {}) or {})
        data = getattr(req_or_url, "data", None)
        parsed_body = None
        if data:
            try:
                parsed_body = json.loads(data.decode("utf-8"))
            except (ValueError, TypeError, UnicodeDecodeError):
                pass
        calls.append({
            "url": url,
            "method": method,
            "headers": headers,
            "body": parsed_body,
        })
        return plan.pop(0)(req_or_url)

    _fake.calls = calls  # type: ignore[attr-defined]
    return _fake


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(
    tmp_path: Path,
    *,
    attempt_id: int = 1,
    duration_s: int = 8,
    resolution: str = "720p",
    aspect_ratio: str = "16:9",
    negative_prompt: str = "",
    seed: int | None = None,
    image_base64: str | None = None,
) -> dict:
    p: dict[str, Any] = {
        "prompt": "A wide quiet dawn shot of a lighthouse.",
        "negative_prompt": negative_prompt,
        "duration_s": duration_s,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "output_dir": tmp_path / "sh_001",
        "attempt_id": attempt_id,
    }
    if seed is not None:
        p["seed"] = seed
    if image_base64 is not None:
        p["image_base64"] = image_base64
    return p


def _op_submit(name: str = "projects/p/locations/us-central1/operations/op_1") -> dict:
    return {"name": name}


def _op_done_inline(mp4_bytes: bytes) -> dict:
    return {
        "name": "projects/p/locations/us-central1/operations/op_1",
        "done": True,
        "response": {
            "@type": "type.googleapis.com/cloud.ai.large_models.vision.GenerateVideoResponse",
            "raiMediaFilteredCount": 0,
            "videos": [
                {
                    "bytesBase64Encoded": base64.b64encode(mp4_bytes).decode("ascii"),
                    "mimeType": "video/mp4",
                }
            ],
        },
    }


def _op_done_gcs(gcs_uri: str) -> dict:
    return {
        "name": "projects/p/locations/us-central1/operations/op_1",
        "done": True,
        "response": {
            "@type": "type.googleapis.com/cloud.ai.large_models.vision.GenerateVideoResponse",
            "raiMediaFilteredCount": 0,
            "videos": [{"gcsUri": gcs_uri, "mimeType": "video/mp4"}],
        },
    }


def _op_done_filtered() -> dict:
    return {
        "name": "projects/p/locations/us-central1/operations/op_1",
        "done": True,
        "response": {
            "@type": "type.googleapis.com/cloud.ai.large_models.vision.GenerateVideoResponse",
            "raiMediaFilteredCount": 1,
            "raiMediaFilteredReasons": ["17301594: Celebrity"],
            "videos": [],
        },
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_inline_base64(tmp_path: Path) -> None:
    mp4 = b"\x00\x00\x00\x20ftypisom" + bytes(512)
    plan = [
        lambda req: _json_response(_op_submit()),        # submit
        lambda req: _json_response({"done": False}),     # poll 1
        lambda req: _json_response(_op_done_inline(mp4)),  # poll 2 done
    ]
    tool = VeoRendererTool(
        project_id="p",
        token_provider=lambda: "stub-token",
        urlopen=_make_urlopen(plan),
        sleep=lambda _: None,
    )
    result = tool("sh_001", _payload(tmp_path, duration_s=8))

    assert result["status"] == "ok"
    assert result["provider"] == PROVIDER_ID
    assert result["model"] == DEFAULT_MODEL_ID
    assert result["task_id"].endswith("op_1")
    assert result["render_path"].endswith("sh_001/v1.mp4")
    assert result["output_size_bytes"] == len(mp4)
    # Veo 3.1 Fast with audio disabled: $0.10/s * 8s = $0.80
    assert result["cost_usd"] == 0.80
    assert result["actual_duration_s"] == 8
    assert "note" not in result


def test_happy_path_gcs_download(tmp_path: Path) -> None:
    mp4 = b"\x00\x00\x00\x20ftypgcs_" + bytes(256)
    plan = [
        lambda req: _json_response(_op_submit()),
        lambda req: _json_response(
            _op_done_gcs("gs://my-bucket/rectoverso/sh_001/sample_0.mp4")
        ),
        lambda req: _FakeResponse(mp4),  # storage.googleapis.com GET
    ]
    fake = _make_urlopen(plan)
    tool = VeoRendererTool(
        project_id="p",
        token_provider=lambda: "stub-token",
        storage_uri_root="gs://my-bucket/rectoverso",
        urlopen=fake,
        sleep=lambda _: None,
    )
    result = tool("sh_001", _payload(tmp_path, duration_s=6))
    assert result["status"] == "ok"

    # Third call is to storage.googleapis.com with bearer auth.
    download_call = fake.calls[2]
    assert "storage.googleapis.com" in download_call["url"]
    assert download_call["headers"].get("Authorization") == "Bearer stub-token"

    # submit body should include storageUri and generateAudio=False.
    submit_body = fake.calls[0]["body"]
    assert submit_body["parameters"]["storageUri"].startswith("gs://my-bucket/")
    assert submit_body["parameters"]["generateAudio"] is False
    assert submit_body["parameters"]["personGeneration"] == "disallow"


def test_duration_clamp_snaps_upward(tmp_path: Path) -> None:
    mp4 = b"\x00\x00\x00\x20ftyp"
    plan = [
        lambda req: _json_response(_op_submit()),
        lambda req: _json_response(_op_done_inline(mp4)),
    ]
    fake = _make_urlopen(plan)
    tool = VeoRendererTool(
        project_id="p",
        token_provider=lambda: "t",
        urlopen=fake,
        sleep=lambda _: None,
    )
    # Request 5s — clamps upward to 6.
    result = tool("sh_001", _payload(tmp_path, duration_s=5))

    assert result["status"] == "ok"
    assert result["actual_duration_s"] == 6
    assert result["requested_duration_s"] == 5
    assert "note" in result and "clamped" in result["note"]
    # The submit body itself carries the clamped duration.
    assert fake.calls[0]["body"]["parameters"]["durationSeconds"] == 6


def test_submit_body_shape(tmp_path: Path) -> None:
    mp4 = b"x"
    plan = [
        lambda req: _json_response(_op_submit()),
        lambda req: _json_response(_op_done_inline(mp4)),
    ]
    fake = _make_urlopen(plan)
    tool = VeoRendererTool(
        project_id="my-proj",
        token_provider=lambda: "stub",
        urlopen=fake,
        sleep=lambda _: None,
    )
    tool("sh_001", _payload(tmp_path, negative_prompt="blur, distort", seed=42))

    body = fake.calls[0]["body"]
    assert body["instances"] == [{"prompt": "A wide quiet dawn shot of a lighthouse."}]
    params = body["parameters"]
    assert params["sampleCount"] == 1
    assert params["aspectRatio"] == "16:9"
    assert params["durationSeconds"] == 8
    assert params["resolution"] == "720p"
    assert params["generateAudio"] is False
    assert params["personGeneration"] == "disallow"
    assert params["negativePrompt"] == "blur, distort"
    assert params["seed"] == 42
    # No storageUri when the adapter runs without a bucket — we want inline b64.
    assert "storageUri" not in params


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


def test_missing_project_id_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Neutralize all project-id lookup paths: constructor arg unset, shell env
    # cleared, AND .env file walk suppressed (the project's real .env has
    # GCP_PROJECT_ID set and the walk starts from the veo.py module path,
    # so chdir alone isn't enough).
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr("src.producer.veo._resolve_env", lambda *_args, **_kw: None)
    tool = VeoRendererTool(
        token_provider=lambda: "t", urlopen=_make_urlopen([]), sleep=lambda _: None
    )
    with pytest.raises(RuntimeError, match="GCP project id"):
        tool("sh_001", _payload(tmp_path))


def test_auth_provider_failure_becomes_failure_result(tmp_path: Path) -> None:
    def _bad():
        raise RuntimeError("ADC broken")

    tool = VeoRendererTool(
        project_id="p",
        token_provider=_bad,
        urlopen=_make_urlopen([]),
        sleep=lambda _: None,
    )
    result = tool("sh_001", _payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "auth"


def test_content_policy_on_completed_operation_is_billed(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_op_submit()),
        lambda req: _json_response(_op_done_filtered()),
    ]
    tool = VeoRendererTool(
        project_id="p",
        token_provider=lambda: "t",
        urlopen=_make_urlopen(plan),
        sleep=lambda _: None,
    )
    result = tool("sh_001", _payload(tmp_path, duration_s=8))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "content_policy"
    # Veo bills filtered samples — cost_usd must reflect that, not 0.0.
    assert result["cost_usd"] == 0.80


def test_submit_400_is_validation_failure(tmp_path: Path) -> None:
    def _bad(req):
        raise _http_error(
            400,
            '{"error":{"code":400,"message":"Invalid duration","status":"INVALID_ARGUMENT"}}',
        )

    plan = [_bad]
    tool = VeoRendererTool(
        project_id="p", token_provider=lambda: "t",
        urlopen=_make_urlopen(plan), sleep=lambda _: None,
    )
    result = tool("sh_001", _payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "validation"


def test_submit_429_is_rate_limit(tmp_path: Path) -> None:
    def _throttled(req):
        raise _http_error(429, '{"error":{"code":429,"status":"RESOURCE_EXHAUSTED"}}')

    plan = [_throttled]
    tool = VeoRendererTool(
        project_id="p", token_provider=lambda: "t",
        urlopen=_make_urlopen(plan), sleep=lambda _: None,
    )
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "rate_limit"


def test_poll_timeout(tmp_path: Path) -> None:
    responses = [_json_response(_op_submit())] + [
        _json_response({"done": False}) for _ in range(200)
    ]
    plan = [(lambda r, o=r: o) for r in responses]  # idle lambdas

    t0 = [0.0]
    def _sleep(_n):
        t0[0] += 30.0

    import time as _time
    orig = _time.time
    _time.time = lambda: t0[0]  # type: ignore[assignment]
    try:
        tool = VeoRendererTool(
            project_id="p",
            token_provider=lambda: "t",
            poll_interval_s=5,
            poll_timeout_s=20,
            urlopen=_make_urlopen(plan),
            sleep=_sleep,
        )
        result = tool("sh_001", _payload(tmp_path))
    finally:
        _time.time = orig  # type: ignore[assignment]

    assert result["status"] == "failed"
    assert result["failure_stage"] == "poll:timeout"


def test_operation_error_surfaces(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_op_submit()),
        lambda req: _json_response({
            "name": "x", "done": True,
            "error": {"code": 13, "message": "internal"},
        }),
    ]
    tool = VeoRendererTool(
        project_id="p", token_provider=lambda: "t",
        urlopen=_make_urlopen(plan), sleep=lambda _: None,
    )
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "operation:error"
    assert "internal" in result["stderr_tail"]


def test_malformed_videos_payload(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_op_submit()),
        lambda req: _json_response({
            "name": "x", "done": True,
            "response": {"raiMediaFilteredCount": 0, "videos": [{"mimeType": "video/mp4"}]},
        }),
    ]
    tool = VeoRendererTool(
        project_id="p", token_provider=lambda: "t",
        urlopen=_make_urlopen(plan), sleep=lambda _: None,
    )
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "result_malformed"
    # Still billed: sample was generated even though we can't fetch it.
    assert result["cost_usd"] == 0.80


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_snap_duration_covers_all_values() -> None:
    assert _snap_duration(1) == 4
    assert _snap_duration(4) == 4
    assert _snap_duration(5) == 6
    assert _snap_duration(6) == 6
    assert _snap_duration(7) == 8
    assert _snap_duration(8) == 8
    assert _snap_duration(20) == 8
    assert VALID_DURATIONS_S == (4, 6, 8)


def test_cost_for_fast_vs_standard() -> None:
    assert _cost_for("veo-3.1-fast-generate-001", 8) == 0.80
    assert _cost_for("veo-3.1-fast-generate-001", 6) == 0.60
    # Non-fast Veo 3.1 would be $0.40/s
    assert _cost_for("veo-3.1-generate-001", 8) == 3.20


def test_build_urls() -> None:
    submit = _build_submit_url("us-central1", "my-proj", DEFAULT_MODEL_ID)
    assert submit.endswith(":predictLongRunning")
    assert "projects/my-proj" in submit
    assert "us-central1-aiplatform" in submit

    poll = _build_poll_url("us-central1", "my-proj", DEFAULT_MODEL_ID)
    assert poll.endswith(":fetchPredictOperation")


def test_default_location() -> None:
    assert DEFAULT_LOCATION == "us-central1"
