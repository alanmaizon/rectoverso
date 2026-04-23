"""Unit tests for src.producer.kling.KlingRendererTool.

Hermetic: inject fake urlopen + sleep. One shot dispatch per test, covering
submit -> poll -> result fetch -> download.

Intent:      tool result dict shape matches what render_cmd projects
Edge cases:  missing image_url, key failover, content_policy 422, poll timeout,
             rate_limit 429, result URL missing, zero-byte download
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.producer.kling import (
    FAL_QUEUE_BASE,
    KlingRendererTool,
    _cost_for,
    _is_content_policy_violation,
    _snap_duration,
    encode_image_as_data_uri,
)


# Shared HTTP plumbing lives in tests/producer/_fakes.py.
from tests.producer._fakes import (
    FakeResponse as _FakeResponse,
    json_response as _json_response,
    http_error as _http_error,
    make_urlopen as _make_urlopen,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


MODEL_STD = "fal-ai/kling-video/v2.1/standard/image-to-video"
MODEL_PRO = "fal-ai/kling-video/v2.1/pro/image-to-video"


def _payload(
    tmp_path: Path,
    *,
    attempt_id: int = 1,
    duration_s: int = 5,
    model: str = MODEL_STD,
    negative_prompt: str = "",
    image_url: str | None = "https://cdn.example.com/ref.jpg",
    tail_image_url: str | None = None,
) -> dict:
    p: dict[str, Any] = {
        "model": model,
        "prompt": "camera pushes in slowly",
        "negative_prompt": negative_prompt,
        "duration_s": duration_s,
        "output_dir": tmp_path / "sh_001",
        "attempt_id": attempt_id,
    }
    if image_url is not None:
        p["image_url"] = image_url
    if tail_image_url is not None:
        p["tail_image_url"] = tail_image_url
    return p


def _submit_response(request_id: str = "req_1") -> dict:
    base = f"{FAL_QUEUE_BASE}/{MODEL_STD}/requests/{request_id}"
    return {
        "request_id": request_id,
        "status_url": f"{base}/status",
        "response_url": base,
        "cancel_url": f"{base}/cancel",
        "queue_position": 0,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_submit_poll_result_download(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "IN_QUEUE"}),
        lambda req: _json_response({"status": "IN_PROGRESS"}),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v3.fal.media/out.mp4"}}),
        lambda req: _FakeResponse(b"\x00" * 2048),
    ]
    tool = KlingRendererTool(
        api_key="sk-primary",
        urlopen=_make_urlopen(plan),
        sleep=lambda _: None,
    )
    result = tool("sh_001", _payload(tmp_path))

    assert result["status"] == "ok"
    assert result["provider"] == "fal_kling_2_1_standard"
    assert result["model"] == MODEL_STD
    assert result["task_id"] == "req_1"
    assert result["render_path"].endswith("sh_001/v1.mp4")
    assert result["output_size_bytes"] == 2048
    assert result["cost_usd"] == 0.25
    assert result["actual_duration_s"] == 5
    assert "note" not in result  # no clamp


def test_auth_header_uses_literal_key_word(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/x.mp4"}}),
        lambda req: _FakeResponse(b"\x00" * 16),
    ]
    fake = _make_urlopen(plan)
    tool = KlingRendererTool(api_key="sk-primary", urlopen=fake, sleep=lambda _: None)
    tool("sh_001", _payload(tmp_path))

    # Submit call's Authorization header must be "Key sk-primary".
    submit_auth = fake.calls[0]["headers"].get("Authorization")
    assert submit_auth == "Key sk-primary"


def test_duration_clamp_snaps_upward(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/x.mp4"}}),
        lambda req: _FakeResponse(b"\x01" * 32),
    ]
    tool = KlingRendererTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    # Request 7s — clamps to 10 (nearest legal upward).
    result = tool("sh_001", _payload(tmp_path, duration_s=7))

    assert result["status"] == "ok"
    assert result["actual_duration_s"] == 10
    assert result["requested_duration_s"] == 7
    assert "note" in result and "clamped" in result["note"]
    assert result["cost_usd"] == 0.5  # 0.25 + 5 * 0.05


def test_pro_tier_cost(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response("req_pro")),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/pro.mp4"}}),
        lambda req: _FakeResponse(b"\x02" * 64),
    ]
    tool = KlingRendererTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_001", _payload(tmp_path, model=MODEL_PRO, duration_s=10))

    assert result["provider"] == "fal_kling_2_1_pro"
    # 0.49 + 5 * 0.098 = 0.98
    assert result["cost_usd"] == 0.98


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


def test_missing_image_url_rejects_before_submit(tmp_path: Path) -> None:
    tool = KlingRendererTool(api_key="sk", urlopen=_make_urlopen([]), sleep=lambda _: None)
    with pytest.raises(ValueError, match="image_url"):
        tool("sh_001", _payload(tmp_path, image_url=None))


def test_missing_api_key_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Neutralize the file-walk .env lookup. The project's real .env has fal keys,
    # and resolve_env_key walks from the _common.py module path (not cwd), so chdir
    # alone wouldn't isolate the test.
    monkeypatch.setattr("src.producer.kling.resolve_env_key", lambda *_a, **_k: None)
    tool = KlingRendererTool(urlopen=_make_urlopen([]), sleep=lambda _: None)
    with pytest.raises(RuntimeError, match="FAL_KEY"):
        tool("sh_001", _payload(tmp_path))


def test_content_policy_422_is_terminal(tmp_path: Path) -> None:
    body = json.dumps({
        "detail": [{
            "loc": ["body", "prompt"],
            "msg": "blocked by content checker",
            "type": "content_policy_violation",
        }]
    })

    def _responder(req):
        raise _http_error(422, body)

    plan = [_responder]
    tool = KlingRendererTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_001", _payload(tmp_path))

    assert result["status"] == "failed"
    assert result["failure_stage"] == "content_policy"
    assert "content_policy_violation" in result["stderr_tail"]


def test_auth_fails_over_to_backup_on_401(tmp_path: Path) -> None:
    def _401(req):
        raise _http_error(401, '{"detail":"unauthorized"}')

    plan: list = [
        _401,  # primary key 401s on submit
        lambda req: _json_response(_submit_response()),  # backup succeeds
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/x.mp4"}}),
        lambda req: _FakeResponse(b"\x00" * 16),
    ]
    fake = _make_urlopen(plan)
    tool = KlingRendererTool(
        api_key="sk-primary",
        backup_api_key="sk-backup",
        urlopen=fake,
        sleep=lambda _: None,
    )
    result = tool("sh_001", _payload(tmp_path))
    assert result["status"] == "ok"

    # After failover, the poll call should use the backup key.
    poll_auth = fake.calls[2]["headers"].get("Authorization")
    assert poll_auth == "Key sk-backup"


def test_no_backup_key_means_auth_failure_is_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Neutralize .env discovery of a backup key (project's real .env has one).
    monkeypatch.setattr("src.producer.kling.resolve_env_key", lambda *_a, **_k: None)
    plan: list = [lambda req: (_ for _ in ()).throw(_http_error(401, '{"detail":"no"}'))]
    tool = KlingRendererTool(api_key="sk-primary", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_001", _payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "submit:http_401"


def test_poll_timeout_surfaces_failure(tmp_path: Path) -> None:
    responses = [_json_response(_submit_response())] + [
        _json_response({"status": "IN_PROGRESS"}) for _ in range(1000)
    ]

    def _responder_factory(idx):
        return lambda req: responses[idx]

    plan = [_responder_factory(i) for i in range(len(responses))]

    # Force a fast timeout by advancing virtual time on each sleep.
    t0 = [0.0]
    def _sleep(_n):
        t0[0] += 10.0

    import time as _time
    orig = _time.time
    _time.time = lambda: t0[0]  # type: ignore[assignment]
    try:
        tool = KlingRendererTool(
            api_key="sk",
            poll_interval_s=1,
            poll_timeout_s=5,
            urlopen=_make_urlopen(plan),
            sleep=_sleep,
        )
        result = tool("sh_001", _payload(tmp_path))
    finally:
        _time.time = orig  # type: ignore[assignment]

    assert result["status"] == "failed"
    assert result["failure_stage"] == "poll:timeout"


def test_result_missing_video_url(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {}}),  # missing url
    ]
    tool = KlingRendererTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_001", _payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "result_malformed"


def test_zero_byte_download(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response(_submit_response()),
        lambda req: _json_response({"status": "COMPLETED"}),
        lambda req: _json_response({"video": {"url": "https://v/x.mp4"}}),
        lambda req: _FakeResponse(b""),
    ]
    tool = KlingRendererTool(api_key="sk", urlopen=_make_urlopen(plan), sleep=lambda _: None)
    result = tool("sh_001", _payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "download:empty"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_snap_duration() -> None:
    assert _snap_duration(1) == 5
    assert _snap_duration(5) == 5
    assert _snap_duration(6) == 10
    assert _snap_duration(10) == 10
    assert _snap_duration(15) == 10


def test_cost_for_standard_and_pro() -> None:
    assert _cost_for(MODEL_STD, 5) == 0.25
    assert _cost_for(MODEL_STD, 10) == 0.50
    assert _cost_for(MODEL_PRO, 5) == 0.49
    assert _cost_for(MODEL_PRO, 10) == 0.98


def test_is_content_policy_violation_true_on_match() -> None:
    body = '{"detail":[{"type":"content_policy_violation","msg":"blocked"}]}'
    assert _is_content_policy_violation(body) is True


def test_is_content_policy_violation_false_on_other_422() -> None:
    body = '{"detail":[{"type":"image_too_large","msg":"bad"}]}'
    assert _is_content_policy_violation(body) is False


def test_is_content_policy_violation_false_on_malformed_body() -> None:
    assert _is_content_policy_violation("not json") is False


def test_encode_image_as_data_uri_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "ref.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n_fakepng_")
    uri = encode_image_as_data_uri(p)
    assert uri.startswith("data:image/png;base64,")
    # and the base64 payload decodes to what we wrote
    import base64
    body = uri.split(",", 1)[1]
    assert base64.b64decode(body) == b"\x89PNG\r\n\x1a\n_fakepng_"
