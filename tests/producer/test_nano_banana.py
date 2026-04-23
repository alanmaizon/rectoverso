"""Unit tests for src.producer.nano_banana.NanoBananaImageTool.

Hermetic: inject fake urlopen. Gemini image API is synchronous (no polling),
so the fake just needs one response per call.
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
from pathlib import Path
from typing import Any, Callable

import pytest

from src.producer.nano_banana import (
    BLOCKED_FINISH_REASONS,
    DEFAULT_MODEL,
    GEMINI_ENDPOINT_FMT,
    NanoBananaImageTool,
    _compose_prompt,
    _cost_for,
    _submit_failure_stage,
)


# ---------------------------------------------------------------------------
# Fake HTTP plumbing
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
        url="https://generativelanguage.googleapis.com/",
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
        body = None
        if data:
            try:
                body = json.loads(data.decode("utf-8"))
            except (ValueError, TypeError, UnicodeDecodeError):
                pass
        calls.append({"url": url, "method": method, "headers": headers, "body": body})
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
    aspect_ratio: str = "16:9",
    negative_prompt: str = "",
    seed: int | None = None,
) -> dict:
    p: dict[str, Any] = {
        "prompt": "A solitary figure in a dark coat walks down a misty forest path",
        "negative_prompt": negative_prompt,
        "aspect_ratio": aspect_ratio,
        "output_dir": tmp_path / "refs",
        "attempt_id": attempt_id,
    }
    if seed is not None:
        p["seed"] = seed
    return p


def _ok_response(image_bytes: bytes, mime: str = "image/png", finish: str = "STOP") -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return {
        "candidates": [{
            "content": {
                "role": "model",
                "parts": [
                    {"inlineData": {"mimeType": mime, "data": b64}},
                ],
            },
            "finishReason": finish,
        }],
        "usageMetadata": {"promptTokenCount": 42, "candidatesTokenCount": 1280},
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_inline_base64(tmp_path: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n_fakepng_content_" + b"\x00" * 200
    plan = [lambda req: _json_response(_ok_response(png))]
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen(plan))
    result = tool("sh_006", _payload(tmp_path))

    assert result["status"] == "ok"
    assert result["provider"] == "gemini_nano_banana"
    assert result["model"] == DEFAULT_MODEL
    assert result["image_path"].endswith("refs/sh_006_v1.png")
    assert result["output_size_bytes"] == len(png)
    assert result["mime_type"] == "image/png"
    # gemini-2.5-flash-image pricing
    assert result["cost_usd"] == 0.039
    assert result["size"] == "16:9"


def test_uses_api_key_header_and_body_shape(tmp_path: Path) -> None:
    png = b"\x89PNG" + b"\x00" * 16
    plan = [lambda req: _json_response(_ok_response(png))]
    fake = _make_urlopen(plan)
    tool = NanoBananaImageTool(api_key="gk-primary", urlopen=fake)

    tool("sh_001", _payload(tmp_path, negative_prompt="blurry, watermark", aspect_ratio="4:3"))

    call = fake.calls[0]
    assert call["url"] == GEMINI_ENDPOINT_FMT.format(model=DEFAULT_MODEL)
    # Gemini Dev API uses x-goog-api-key header (urllib capitalizes keys)
    api_key_header = (
        call["headers"].get("X-goog-api-key")
        or call["headers"].get("x-goog-api-key")
    )
    assert api_key_header == "gk-primary"
    # No Bearer header (that's Vertex, not Dev API)
    assert "Authorization" not in call["headers"]

    body = call["body"]
    # Gemini contents/parts format
    assert body["contents"][0]["role"] == "user"
    parts = body["contents"][0]["parts"]
    assert parts[0]["text"].startswith("A solitary figure")
    # Negative prompt folded into the prompt text
    assert "Avoid: blurry, watermark" in parts[0]["text"]
    # Aspect hint embedded as belt+braces
    assert "Aspect ratio: 4:3" in parts[0]["text"]
    # Response modalities + aspect ratio config
    gen = body["generationConfig"]
    assert gen["responseModalities"] == ["IMAGE"]
    assert gen["imageConfig"]["aspectRatio"] == "4:3"


def test_pro_model_cost_override(tmp_path: Path) -> None:
    png = b"\x89" + b"\x00" * 8
    plan = [lambda req: _json_response(_ok_response(png))]
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen(plan))
    payload = _payload(tmp_path)
    payload["model"] = "gemini-3-pro-image-preview"
    result = tool("sh_001", payload)
    assert result["cost_usd"] == 0.134
    assert result["model"] == "gemini-3-pro-image-preview"


def test_snake_case_inline_data_accepted(tmp_path: Path) -> None:
    """Some Gemini SDK responses use inline_data (snake_case) instead of the
    camelCase inlineData. Adapter should accept either."""
    png = b"\x89PNG" + b"\x00" * 32
    resp = {
        "candidates": [{
            "content": {
                "role": "model",
                "parts": [
                    {"inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(png).decode("ascii"),
                    }}
                ],
            },
            "finishReason": "STOP",
        }]
    }
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([lambda r: _json_response(resp)]))
    result = tool("sh_001", _payload(tmp_path))
    assert result["status"] == "ok"
    assert result["mime_type"] == "image/jpeg"


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


def test_missing_api_key_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.producer.nano_banana._resolve_gemini_key", lambda: None)
    tool = NanoBananaImageTool(urlopen=_make_urlopen([]))
    with pytest.raises(RuntimeError, match="GEMINI_KEY"):
        tool("sh_001", _payload(tmp_path))


def test_unsupported_aspect_raises(tmp_path: Path) -> None:
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([]))
    with pytest.raises(ValueError, match="aspect_ratio"):
        tool("sh_001", _payload(tmp_path, aspect_ratio="2:1"))


def test_prompt_feedback_block_is_content_policy(tmp_path: Path) -> None:
    resp = {
        "promptFeedback": {"blockReason": "SAFETY"},
        "candidates": [],
    }
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([lambda r: _json_response(resp)]))
    result = tool("sh_001", _payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "content_policy"
    assert "SAFETY" in result["stderr_tail"]


def test_safety_finish_reason_is_content_policy(tmp_path: Path) -> None:
    resp = {
        "candidates": [{
            "content": {"role": "model", "parts": []},
            "finishReason": "SAFETY",
        }],
    }
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([lambda r: _json_response(resp)]))
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "content_policy"
    assert "SAFETY" in result["stderr_tail"]


def test_prohibited_content_is_content_policy(tmp_path: Path) -> None:
    resp = {
        "candidates": [{
            "content": {"role": "model", "parts": []},
            "finishReason": "PROHIBITED_CONTENT",
        }],
    }
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([lambda r: _json_response(resp)]))
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "content_policy"


def test_no_candidates_is_result_malformed(tmp_path: Path) -> None:
    resp = {}
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([lambda r: _json_response(resp)]))
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "result_malformed"


def test_no_inline_image_is_result_malformed(tmp_path: Path) -> None:
    resp = {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": "I can't generate that."}]},
            "finishReason": "STOP",
        }],
    }
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([lambda r: _json_response(resp)]))
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "result_malformed"


def test_400_classified_as_validation_by_default(tmp_path: Path) -> None:
    def _bad(req):
        raise _http_error(400, '{"error":{"code":400,"message":"bad request"}}')
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([_bad]))
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "validation"


def test_400_with_blocked_message_is_content_policy(tmp_path: Path) -> None:
    def _blocked(req):
        raise _http_error(400, '{"error":{"message":"Your prompt was blocked by safety settings"}}')
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([_blocked]))
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "content_policy"


def test_401_is_auth(tmp_path: Path) -> None:
    def _noauth(req):
        raise _http_error(401, '{"error":{"code":401,"status":"UNAUTHENTICATED"}}')
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([_noauth]))
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "auth"


def test_429_is_rate_limit(tmp_path: Path) -> None:
    def _throttled(req):
        raise _http_error(429, '{"error":{"code":429,"status":"RESOURCE_EXHAUSTED"}}')
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([_throttled]))
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "rate_limit"


def test_base64_decode_failure(tmp_path: Path) -> None:
    resp = {
        "candidates": [{
            "content": {
                "role": "model",
                "parts": [{"inlineData": {"mimeType": "image/png", "data": "!!!not-base64!!!"}}],
            },
            "finishReason": "STOP",
        }]
    }
    tool = NanoBananaImageTool(api_key="gk", urlopen=_make_urlopen([lambda r: _json_response(resp)]))
    result = tool("sh_001", _payload(tmp_path))
    assert result["failure_stage"] == "result_malformed"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_compose_prompt_folds_negatives_and_aspect() -> None:
    out = _compose_prompt("A cat", "blurry, text", "16:9")
    assert "A cat" in out
    assert "Avoid: blurry, text" in out
    assert "Aspect ratio: 16:9" in out


def test_compose_prompt_skips_empty_negative() -> None:
    out = _compose_prompt("A cat", "", "1:1")
    assert "Avoid" not in out
    assert "Aspect ratio: 1:1" in out


def test_cost_for_all_tiers() -> None:
    assert _cost_for("gemini-2.5-flash-image") == 0.039
    assert _cost_for("gemini-3.1-flash-image-preview") == 0.045
    assert _cost_for("gemini-3-pro-image-preview") == 0.134


def test_blocked_finish_reasons_membership() -> None:
    assert "SAFETY" in BLOCKED_FINISH_REASONS
    assert "PROHIBITED_CONTENT" in BLOCKED_FINISH_REASONS
    assert "STOP" not in BLOCKED_FINISH_REASONS


def test_submit_failure_stage_classification() -> None:
    assert _submit_failure_stage(429, "x") == "rate_limit"
    assert _submit_failure_stage(401, "x") == "auth"
    assert _submit_failure_stage(400, "x") == "validation"
    assert _submit_failure_stage(500, "x") == "submit:http_500"
    # Content policy detected in body text regardless of status code
    assert _submit_failure_stage(400, "prompt was blocked by safety") == "content_policy"
    assert _submit_failure_stage(400, "prohibited content") == "content_policy"
