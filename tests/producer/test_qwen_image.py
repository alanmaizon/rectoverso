"""Unit tests for src.producer.qwen_image.QwenImageTool.

Hermetic: inject fake urlopen + sleep. Pattern mirrors test_renderer.py /
test_kling.py.

Coverage:
    happy path (submit -> poll -> download)
    duration-style clamp equivalent (size mapping)
    content-policy FAILED task
    submit HTTP errors (400, 401, 429, 500)
    poll timeout
    missing API key
    malformed response
    pure helpers
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any, Callable

import pytest

from src.producer.qwen_image import (
    ASPECT_TO_SIZE,
    CONTENT_POLICY_CODES,
    DEFAULT_MODEL,
    QWEN_POLL_URL_FMT,
    QWEN_SUBMIT_URL,
    QwenImageTool,
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
        url="https://dashscope-intl.aliyuncs.com/",
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
        "prompt": "A solitary figure in a dark weatherproof coat on a misty forest path, cold blue-grey palette",
        "negative_prompt": negative_prompt,
        "aspect_ratio": aspect_ratio,
        "output_dir": tmp_path / "refs",
        "attempt_id": attempt_id,
    }
    if seed is not None:
        p["seed"] = seed
    return p


def _submit_succeeded(task_id: str = "img_task_1") -> dict:
    return {
        "output": {"task_id": task_id, "task_status": "PENDING"},
        "request_id": "req_abc",
    }


def _poll_succeeded(url: str = "https://oss.fake/result.png") -> dict:
    return {
        "output": {
            "task_id": "img_task_1",
            "task_status": "SUCCEEDED",
            "results": [
                {"url": url, "orig_prompt": "x", "actual_prompt": "x"}
            ],
        },
        "usage": {"image_count": 1},
    }


def _poll_failed(code: str = "DataInspectionFailed") -> dict:
    return {
        "output": {
            "task_id": "img_task_1",
            "task_status": "FAILED",
            "code": code,
            "message": "blocked by content inspector",
        }
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_submit_poll_download(tmp_path: Path) -> None:
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512
    plan = [
        lambda req: _json_response(_submit_succeeded()),
        lambda req: _json_response({"output": {"task_status": "RUNNING"}}),
        lambda req: _json_response(_poll_succeeded()),
        lambda req: _FakeResponse(png_bytes),
    ]
    tool = QwenImageTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_006", _payload(tmp_path))

    assert result["status"] == "ok"
    assert result["provider"] == "dashscope_qwen_image"
    assert result["model"] == DEFAULT_MODEL
    assert result["task_id"] == "img_task_1"
    assert result["image_path"].endswith("refs/sh_006_v1.png")
    assert result["output_size_bytes"] == len(png_bytes)
    assert result["size"] == "1664*928"
    assert result["cost_usd"] == 0.0
    assert result["quota_cost"] == 1


def test_submit_body_shape(tmp_path: Path) -> None:
    png = b"\x89PNG" + b"\x00" * 32
    plan = [
        lambda req: _json_response(_submit_succeeded()),
        lambda req: _json_response(_poll_succeeded()),
        lambda req: _FakeResponse(png),
    ]
    fake = _make_urlopen(plan)
    tool = QwenImageTool(api_key="sk", urlopen=fake, sleep=lambda _: None)
    tool("sh_006", _payload(tmp_path, aspect_ratio="1:1", negative_prompt="bad anatomy", seed=777))

    submit = fake.calls[0]
    assert submit["url"] == QWEN_SUBMIT_URL
    assert submit["method"] == "POST"
    # Async header is present in the Request; keys are case-capitalized by urllib
    auth = submit["headers"].get("Authorization")
    assert auth == "Bearer sk"
    xdash = submit["headers"].get("X-dashscope-async") or submit["headers"].get("X-DashScope-Async")
    assert xdash == "enable"
    body = submit["body"]
    assert body["model"] == DEFAULT_MODEL
    assert body["input"]["prompt"].startswith("A solitary")
    assert body["input"]["negative_prompt"] == "bad anatomy"
    params = body["parameters"]
    assert params["size"] == "1328*1328"
    assert params["n"] == 1
    assert params["seed"] == 777
    assert params["prompt_extend"] is False
    assert params["watermark"] is False


def test_all_supported_aspects_map_correctly() -> None:
    assert ASPECT_TO_SIZE["16:9"] == "1664*928"
    assert ASPECT_TO_SIZE["4:3"] == "1472*1104"
    assert ASPECT_TO_SIZE["1:1"] == "1328*1328"
    assert ASPECT_TO_SIZE["3:4"] == "1104*1472"
    assert ASPECT_TO_SIZE["9:16"] == "928*1664"


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


def test_unsupported_aspect_raises(tmp_path: Path) -> None:
    tool = QwenImageTool(api_key="sk", urlopen=_make_urlopen([]), sleep=lambda _: None)
    with pytest.raises(ValueError, match="aspect_ratio"):
        tool("sh_006", _payload(tmp_path, aspect_ratio="21:9"))


def test_missing_api_key_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.producer.qwen_image._resolve_dashscope_key", lambda: None
    )
    tool = QwenImageTool(urlopen=_make_urlopen([]), sleep=lambda _: None)
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        tool("sh_006", _payload(tmp_path))


def test_content_policy_rejection_is_terminal(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_succeeded()),
        lambda req: _json_response(_poll_failed("DataInspectionFailed")),
    ]
    tool = QwenImageTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_006", _payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "content_policy"
    assert "content inspector" in result["stderr_tail"]


def test_submit_400_content_policy_detected_via_body(tmp_path: Path) -> None:
    def _bad(req):
        raise _http_error(400, '{"code":"DataInspectionFailed","message":"nope"}')
    plan = [_bad]
    tool = QwenImageTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_006", _payload(tmp_path))
    assert result["failure_stage"] == "content_policy"


def test_submit_400_plain_validation(tmp_path: Path) -> None:
    def _bad(req):
        raise _http_error(400, '{"code":"InvalidParameter","message":"bad size"}')
    plan = [_bad]
    tool = QwenImageTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_006", _payload(tmp_path))
    assert result["failure_stage"] == "validation"


def test_submit_429_is_rate_limit(tmp_path: Path) -> None:
    def _throttled(req):
        raise _http_error(429, '{"code":"Throttling.RateQuota","message":"slow down"}')
    tool = QwenImageTool(api_key="sk", urlopen=_make_urlopen([_throttled]), sleep=lambda _: None)
    result = tool("sh_006", _payload(tmp_path))
    assert result["failure_stage"] == "rate_limit"


def test_submit_401_is_auth(tmp_path: Path) -> None:
    def _noauth(req):
        raise _http_error(401, '{"code":"InvalidApiKey","message":"bad key"}')
    tool = QwenImageTool(api_key="sk", urlopen=_make_urlopen([_noauth]), sleep=lambda _: None)
    result = tool("sh_006", _payload(tmp_path))
    assert result["failure_stage"] == "auth"


def test_poll_timeout(tmp_path: Path) -> None:
    responses = [_json_response(_submit_succeeded())] + [
        _json_response({"output": {"task_status": "RUNNING"}}) for _ in range(200)
    ]
    plan = [(lambda r, o=r: o) for r in responses]

    t0 = [0.0]
    def _sleep(_n):
        t0[0] += 5.0

    import time as _time
    orig = _time.time
    _time.time = lambda: t0[0]  # type: ignore[assignment]
    try:
        tool = QwenImageTool(
            api_key="sk",
            poll_interval_s=1,
            poll_timeout_s=10,
            urlopen=_make_urlopen(plan),
            sleep=_sleep,
        )
        result = tool("sh_006", _payload(tmp_path))
    finally:
        _time.time = orig  # type: ignore[assignment]

    assert result["status"] == "failed"
    assert result["failure_stage"] == "poll:timeout"


def test_missing_task_id_in_submit(tmp_path: Path) -> None:
    plan = [lambda req: _json_response({"output": {}})]
    tool = QwenImageTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_006", _payload(tmp_path))
    assert result["failure_stage"] == "submit:malformed"


def test_missing_results_url(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_succeeded()),
        lambda req: _json_response({
            "output": {"task_status": "SUCCEEDED", "results": [{}]}
        }),
    ]
    tool = QwenImageTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_006", _payload(tmp_path))
    assert result["status"] == "failed"
    # SUCCEEDED but no url: classified as result_malformed
    assert result["failure_stage"] == "result_malformed"


def test_zero_byte_download(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_succeeded()),
        lambda req: _json_response(_poll_succeeded()),
        lambda req: _FakeResponse(b""),
    ]
    tool = QwenImageTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_006", _payload(tmp_path))
    assert result["failure_stage"] == "download:empty"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_submit_failure_stage_classification() -> None:
    assert _submit_failure_stage(429, "{}") == "rate_limit"
    assert _submit_failure_stage(401, "{}") == "auth"
    assert _submit_failure_stage(403, "{}") == "auth"
    assert _submit_failure_stage(400, "{}") == "validation"
    assert _submit_failure_stage(500, "{}") == "submit:http_500"
    assert _submit_failure_stage(400, '"DataInspectionFailed"') == "content_policy"


def test_content_policy_codes_membership() -> None:
    assert "DataInspectionFailed" in CONTENT_POLICY_CODES


def test_poll_url_format() -> None:
    assert QWEN_POLL_URL_FMT.format(task_id="abc").endswith("/api/v1/tasks/abc")
