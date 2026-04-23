"""Unit tests for src.producer.renderer.WanRendererTool.

Architecture: inject fake urlopen + fake sleep so tests are hermetic and fast.
All tests cover one shot dispatch end-to-end (submit -> poll -> download).

Intent:      tool result dict shape matches what Producer projects into manifest
Edge cases:  missing API key, HTTP error on submit, task FAILED, poll timeout,
             zero-byte download, duration clamp, auth injection
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any, Callable

import pytest

from src.producer.renderer import (
    WAN_POLL_URL_FMT,
    WAN_SUBMIT_URL,
    WanRendererTool,
    _resolution_to_size,
    _snap_duration,
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


def _make_urlopen(plan: list[Callable[[Any], _FakeResponse]]) -> Callable:
    """plan is a sequence of responders; each call consumes one."""
    calls = []

    def _fake(req_or_url, timeout=None):
        if not plan:
            raise AssertionError("unexpected extra urlopen call")
        url = getattr(req_or_url, "full_url", None) or str(req_or_url)
        calls.append({"url": url, "method": getattr(req_or_url, "method", "GET")})
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
    duration_s: int = 5,
    resolution: str = "720p",
    model: str = "wan-2.7-plus",
    negative_prompt: str = "",
    seed: int | None = None,
) -> dict:
    return {
        "model": model,
        "prompt": "A wide quiet dawn shot of a lighthouse.",
        "negative_prompt": negative_prompt,
        "duration_s": duration_s,
        "resolution": resolution,
        "output_dir": tmp_path / "sh_003",
        "attempt_id": attempt_id,
        **({"seed": seed} if seed is not None else {}),
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_submit_poll_download(tmp_path: Path) -> None:
    plan = [
        # submit -> task_id
        lambda req: _json_response({"output": {"task_id": "t_123", "task_status": "PENDING"}}),
        # poll 1 -> still RUNNING
        lambda req: _json_response({"output": {"task_status": "RUNNING"}}),
        # poll 2 -> SUCCEEDED with video_url
        lambda req: _json_response(
            {"output": {"task_status": "SUCCEEDED", "video_url": "https://fake/video.mp4"}}
        ),
        # download
        lambda req: _FakeResponse(b"\x00" * 2048),
    ]
    fake_urlopen = _make_urlopen(plan)

    tool = WanRendererTool(
        api_key="sk-test",
        urlopen=fake_urlopen,
        sleep=lambda _s: None,
        poll_interval_s=0,
    )
    result = tool(shot_id="sh_003", payload=_payload(tmp_path))

    assert result["status"] == "ok"
    assert result["provider"] == "alibaba_wan_2_7_plus"
    assert result["model"] == "wan-2.7-plus"
    assert result["task_id"] == "t_123"
    assert result["output_size_bytes"] == 2048
    assert result["render_md5"] and len(result["render_md5"]) == 32
    assert result["cost_usd"] == 0.0
    assert result["quota_cost"] == 1
    assert result["latency_s"] >= 0

    out_path = Path(result["render_path"])
    assert out_path.exists()
    assert out_path.read_bytes() == b"\x00" * 2048
    assert out_path.name == "v1.mp4"
    assert out_path.parent.name == "sh_003"

    # Call 1 is submit
    assert fake_urlopen.calls[0]["url"] == WAN_SUBMIT_URL
    # Calls 2-3 poll the task endpoint
    poll_url = WAN_POLL_URL_FMT.format(task_id="t_123")
    assert fake_urlopen.calls[1]["url"] == poll_url
    assert fake_urlopen.calls[2]["url"] == poll_url
    # Call 4 downloads the video
    assert fake_urlopen.calls[3]["url"] == "https://fake/video.mp4"


def test_submit_body_shape(tmp_path: Path) -> None:
    """The submit request body must carry model, prompt, and parameters.size/duration."""
    captured: dict[str, Any] = {}

    def _capture_submit(req):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _json_response({"output": {"task_id": "t_1", "task_status": "PENDING"}})

    plan = [
        _capture_submit,
        lambda req: _json_response(
            {"output": {"task_status": "SUCCEEDED", "video_url": "https://x/v.mp4"}}
        ),
        lambda req: _FakeResponse(b"data"),
    ]
    fake_urlopen = _make_urlopen(plan)

    tool = WanRendererTool(
        api_key="sk-abc",
        urlopen=fake_urlopen,
        sleep=lambda _s: None,
        poll_interval_s=0,
    )
    tool(
        shot_id="sh_003",
        payload=_payload(
            tmp_path,
            duration_s=5,
            resolution="1080p",
            negative_prompt="no people",
            seed=42,
        ),
    )

    assert captured["body"]["model"] == "wan-2.7-plus"
    assert captured["body"]["input"]["prompt"].startswith("A wide")
    assert captured["body"]["input"]["negative_prompt"] == "no people"
    assert captured["body"]["parameters"]["size"] == "1920*1080"
    assert captured["body"]["parameters"]["duration"] == 5
    assert captured["body"]["parameters"]["seed"] == 42
    # Auth + async headers
    assert captured["headers"]["Authorization"] == "Bearer sk-abc"
    assert captured["headers"]["X-dashscope-async"].lower() == "enable"


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_missing_api_key_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    # Also stub out .env discovery by pointing to a tmpdir with no .env.
    monkeypatch.setattr(
        "src.producer.renderer._resolve_dashscope_key", lambda: None
    )
    tool = WanRendererTool(urlopen=lambda *a, **k: None, sleep=lambda _s: None)
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        tool(shot_id="sh_003", payload=_payload(tmp_path))


def test_shot_id_required(tmp_path: Path) -> None:
    tool = WanRendererTool(api_key="sk", urlopen=lambda *a, **k: None, sleep=lambda _s: None)
    with pytest.raises(ValueError, match="shot_id"):
        tool(shot_id=None, payload=_payload(tmp_path))


def test_submit_http_error_returns_failure(tmp_path: Path) -> None:
    def _raise(req):
        raise urllib.error.HTTPError(
            WAN_SUBMIT_URL, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}')
        )

    tool = WanRendererTool(
        api_key="bad", urlopen=_make_urlopen([_raise]), sleep=lambda _s: None
    )
    result = tool(shot_id="sh_003", payload=_payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "submit"
    assert "bad key" in result["stderr_tail"]


def test_poll_task_failed(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response({"output": {"task_id": "t_2", "task_status": "PENDING"}}),
        lambda req: _json_response(
            {"output": {"task_status": "FAILED", "message": "content policy violation"}}
        ),
    ]
    tool = WanRendererTool(
        api_key="sk",
        urlopen=_make_urlopen(plan),
        sleep=lambda _s: None,
        poll_interval_s=0,
    )
    result = tool(shot_id="sh_003", payload=_payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "poll:FAILED"
    assert "content policy" in result["stderr_tail"]
    assert result["task_id"] == "t_2"


def test_poll_timeout(tmp_path: Path) -> None:
    """Every poll returns RUNNING; tool should give up after poll_timeout_s.

    We advance a fake monotonic-ish clock via `sleep` — injecting time travel
    instead of depending on wall clock lets the test run in microseconds.
    """
    call_count = {"n": 0}

    def _fake(req_or_url, timeout=None):
        url = getattr(req_or_url, "full_url", None) or str(req_or_url)
        if "video-synthesis" in url:
            return _json_response(
                {"output": {"task_id": "t_3", "task_status": "PENDING"}}
            )
        # poll endpoint — always RUNNING
        call_count["n"] += 1
        return _json_response({"output": {"task_status": "RUNNING"}})

    # The tool sleeps between polls; hijack sleep() to fast-forward the clock.
    import time as _time

    base = _time.time()

    def _fast_sleep(seconds):
        # Not actually sleeping — just accumulate virtual time.
        _fast_sleep.elapsed += seconds

    _fast_sleep.elapsed = 0.0  # type: ignore[attr-defined]

    # Monkey-patch time.time INSIDE the tool so `time.time() < deadline` ticks forward.
    orig_time = _time.time

    def _stepping_time():
        return base + _fast_sleep.elapsed

    try:
        _time.time = _stepping_time  # type: ignore[assignment]
        tool = WanRendererTool(
            api_key="sk",
            urlopen=_fake,
            sleep=_fast_sleep,
            poll_interval_s=60,    # each sleep advances 60s of virtual time
            poll_timeout_s=300,    # so the loop exits after ~5 polls
        )
        result = tool(shot_id="sh_003", payload=_payload(tmp_path))
    finally:
        _time.time = orig_time  # type: ignore[assignment]

    assert result["status"] == "failed"
    assert result["failure_stage"] == "poll:timeout"
    assert "did not complete" in result["stderr_tail"]
    # Should have polled a handful of times, not thousands.
    assert 1 <= call_count["n"] <= 10


def test_empty_download_is_failure(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response({"output": {"task_id": "t_4", "task_status": "PENDING"}}),
        lambda req: _json_response(
            {"output": {"task_status": "SUCCEEDED", "video_url": "https://x/v.mp4"}}
        ),
        lambda req: _FakeResponse(b""),  # 0 bytes
    ]
    tool = WanRendererTool(
        api_key="sk",
        urlopen=_make_urlopen(plan),
        sleep=lambda _s: None,
        poll_interval_s=0,
    )
    result = tool(shot_id="sh_003", payload=_payload(tmp_path))
    assert result["status"] == "failed"
    assert result["failure_stage"] == "download:empty"
    assert result["output_size_bytes"] == 0


# ---------------------------------------------------------------------------
# Helpers / pure functions
# ---------------------------------------------------------------------------


def test_snap_duration() -> None:
    assert _snap_duration(1) == 5
    assert _snap_duration(5) == 5
    assert _snap_duration(6) == 10
    assert _snap_duration(10) == 10
    assert _snap_duration(12) == 15
    assert _snap_duration(30) == 15


def test_resolution_to_size() -> None:
    assert _resolution_to_size("720p") == "1280*720"
    assert _resolution_to_size("1080p") == "1920*1080"
    assert _resolution_to_size("anything else") == "1280*720"


def test_duration_clamp_note(tmp_path: Path) -> None:
    plan = [
        lambda req: _json_response({"output": {"task_id": "t_5", "task_status": "PENDING"}}),
        lambda req: _json_response(
            {"output": {"task_status": "SUCCEEDED", "video_url": "https://x/v.mp4"}}
        ),
        lambda req: _FakeResponse(b"\x01" * 100),
    ]
    tool = WanRendererTool(
        api_key="sk",
        urlopen=_make_urlopen(plan),
        sleep=lambda _s: None,
        poll_interval_s=0,
    )
    # Request 3s -> Wan clamps to 5s
    result = tool(shot_id="sh_003", payload=_payload(tmp_path, duration_s=3))
    assert result["status"] == "ok"
    assert result["requested_duration_s"] == 3
    assert result["actual_duration_s"] == 5
    assert "clamped" in result["note"]
