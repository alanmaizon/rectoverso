"""Renderer tool adapters — submit/poll/download video generation jobs.

Tier-4 worker per docs/agents.md § Tier 4. Called synchronously from the
Producer's dispatch loop; returns once the MP4 has been downloaded to disk.

Scope in this module:
    - WanRendererTool   — Alibaba Cloud DashScope (free quota; USD=0)

Deliberately out-of-scope (separate modules when they land):
    - Kling via fal.ai  (paid; $136 budget; two-key failover)
    - Vertex AI Veo     (paid; $15 hard cap; ADC auth)

All adapters share the same Tool Protocol shape:
    tool.name == "renderer"
    tool(shot_id, payload) -> dict

The dict returned maps cleanly into a `dispatch_result` EventLog entry AND
into a new `shots[i].attempts[-1]` row when the Producer projects it:

    {
      "status": "ok" | "failed",
      "provider": "alibaba_wan_2_7_plus",
      "model": "wan-2.7-plus",
      "task_id": "<provider task id>",
      "render_path": "artifacts/renders/sh_003/v1.mp4",
      "render_md5": "...",
      "output_size_bytes": 1234567,
      "cost_usd": 0.0,               # Wan is free-quota; other providers > 0
      "latency_s": 123.4,            # submit -> MP4 bytes on disk
      "stdout_tail": "...",          # structured polling log (JSON lines)
      "stderr_tail": "...",          # populated on failure
    }

Intent / Architecture / Edge cases:
    - Intent         : one shot in, one MP4 on disk; determinism is the provider's job
    - Architecture   : pure urllib (stdlib); no new deps; two-phase submit+poll
    - Edge cases     : auth missing; submit fail; poll timeout; task FAILED state;
                       download fail; zero-byte response
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from ._common import http_error_body, md5_file, resolve_env_key


# ---------------------------------------------------------------------------
# Alibaba Cloud DashScope — Wan text-to-video
# ---------------------------------------------------------------------------
#
# Verified via https://www.alibabacloud.com/help/en/model-studio/text-to-video-api-reference:
#   POST  https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis
#   GET   https://dashscope-intl.aliyuncs.com/api/v1/tasks/{task_id}
# Async pattern: `X-DashScope-Async: enable` → task_id → poll until SUCCEEDED.
# Signed MP4 URL valid for 24h after SUCCEEDED.

WAN_SUBMIT_URL = (
    "https://dashscope-intl.aliyuncs.com"
    "/api/v1/services/aigc/video-generation/video-synthesis"
)
WAN_POLL_URL_FMT = "https://dashscope-intl.aliyuncs.com/api/v1/tasks/{task_id}"

# Poll cadence recommended by the DashScope docs.
DEFAULT_POLL_INTERVAL_S = 15
DEFAULT_HTTP_TIMEOUT_S = 60

# Per-model poll-timeout defaults. Wan 2.7 Plus legitimately runs 15-20 min on
# complex prompts (verified during the cold-run orchestrator test — sh_003
# was still RUNNING at 600s and would have completed had we waited). Turbo and
# older variants are typically under 5 min; keep them on the tighter budget.
DEFAULT_POLL_TIMEOUT_S = 600              # base default (Turbo, legacy Wan)
WAN_27_PLUS_POLL_TIMEOUT_S = 1200         # 20 min for Wan 2.7 Plus complex shots


def _poll_timeout_for_model(model: str) -> int:
    """Per-model poll timeout. The constructor's explicit `poll_timeout_s`
    overrides this (honored verbatim for tests and manual tuning); if None,
    this function picks the appropriate default."""
    if model.lower().startswith("wan2.7"):
        return WAN_27_PLUS_POLL_TIMEOUT_S
    return DEFAULT_POLL_TIMEOUT_S


class WanRendererTool:
    """DashScope Wan text-to-video adapter.

    Tool Protocol: `name == "renderer"`, `__call__(shot_id, payload) -> dict`.

    Payload fields (supplied by the Producer from the manifest shot):
        model              : DashScope model id, e.g. "wan-2.7-plus"
        prompt             : primary prompt text (shot.prompt.primary)
        negative_prompt    : optional negative prompt
        duration_s         : integer seconds (Wan takes {5, 10})
        resolution         : "720p" | "1080p" (default 720p)
        output_dir         : directory to save artifacts/renders/{shot_id}/
        attempt_id         : integer (1-indexed; appends to the shot's attempts)
        seed               : optional int for determinism across runs
    """

    name = "renderer"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        submit_url: str = WAN_SUBMIT_URL,
        poll_url_fmt: str = WAN_POLL_URL_FMT,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_timeout_s: float | None = None,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        sleep: Any = time.sleep,
        urlopen: Any = None,
    ) -> None:
        self._api_key = api_key
        self._submit_url = submit_url
        self._poll_url_fmt = poll_url_fmt
        self._poll_interval_s = poll_interval_s
        # None → per-model resolution at call time (see _poll_timeout_for_model).
        # Explicit value → honored verbatim (test hooks, manual tuning).
        self._poll_timeout_s = poll_timeout_s
        self._http_timeout_s = http_timeout_s
        self._sleep = sleep
        # urlopen is injectable for tests — default to urllib.
        self._urlopen = urlopen or urllib.request.urlopen

    # -- Tool Protocol -----------------------------------------------------

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if shot_id is None:
            raise ValueError("WanRendererTool requires a shot_id")

        api_key = self._api_key or _resolve_dashscope_key()
        if not api_key:
            raise RuntimeError(
                "DASHSCOPE_API_KEY missing from env and .env; cannot submit Wan job"
            )

        model = payload["model"]
        prompt = payload["prompt"]
        output_dir = Path(payload["output_dir"])
        attempt_id = int(payload["attempt_id"])
        negative_prompt = payload.get("negative_prompt") or ""
        duration_s = int(payload.get("duration_s", 5))
        resolution = payload.get("resolution", "720p")
        seed = payload.get("seed")

        # Resolve effective poll timeout. If the constructor got a non-None
        # override, use it; otherwise pick per-model (Plus=1200s, Turbo=600s).
        effective_poll_timeout_s = (
            self._poll_timeout_s
            if self._poll_timeout_s is not None
            else _poll_timeout_for_model(model)
        )

        # Wan's supported durations per the API docs are {5, 10}. Callers
        # pass the shot's planned duration; we snap to the nearest legal value
        # and record the clamp in the result for the audit trail.
        actual_duration = _snap_duration(duration_s)

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"v{attempt_id}.mp4"

        started = time.time()
        poll_log: list[dict[str, Any]] = []

        # 1) submit — body shape differs by model family.
        #   wan2.7-*  : new protocol → parameters = {resolution, ratio, duration}
        #   wan2.6-*  : legacy proto → parameters = {size, duration}
        # See https://www.alibabacloud.com/help/en/model-studio/text-to-video-api-reference
        body: dict[str, Any] = {
            "model": model,
            "input": {"prompt": prompt},
            "parameters": _build_parameters(model, resolution, actual_duration),
        }
        if negative_prompt:
            body["input"]["negative_prompt"] = negative_prompt
        if seed is not None:
            body["parameters"]["seed"] = int(seed)

        try:
            submit_resp = self._post_json(
                self._submit_url,
                api_key,
                body,
                extra_headers={"X-DashScope-Async": "enable"},
            )
        except urllib.error.HTTPError as exc:
            return _failure(
                stage="submit",
                started=started,
                poll_log=poll_log,
                stderr=http_error_body(exc),
                model=model,
                attempt_output_name=output_path.name,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        task_id = submit_resp.get("output", {}).get("task_id")
        if not task_id:
            return _failure(
                stage="submit",
                started=started,
                poll_log=poll_log,
                stderr=f"submit response missing task_id: {submit_resp!r}"[:500],
                model=model,
                attempt_output_name=output_path.name,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        # 2) poll
        poll_url = self._poll_url_fmt.format(task_id=task_id)
        deadline = started + effective_poll_timeout_s
        video_url: str | None = None
        last_status: str | None = None

        while time.time() < deadline:
            try:
                poll_resp = self._get_json(poll_url, api_key)
            except urllib.error.HTTPError as exc:
                poll_log.append(
                    {"t": round(time.time() - started, 2), "http_error": exc.code}
                )
                self._sleep(self._poll_interval_s)
                continue

            output = poll_resp.get("output", {})
            status = output.get("task_status")
            last_status = status
            poll_log.append(
                {"t": round(time.time() - started, 2), "task_status": status}
            )

            if status == "SUCCEEDED":
                video_url = output.get("video_url")
                break
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                return _failure(
                    stage=f"poll:{status}",
                    started=started,
                    poll_log=poll_log,
                    stderr=(output.get("message") or "")[:500],
                    model=model,
                    attempt_output_name=output_path.name,
                    task_id=task_id,
                    clamp_note=_clamp_note(duration_s, actual_duration),
                )
            self._sleep(self._poll_interval_s)

        if video_url is None:
            return _failure(
                stage="poll:timeout",
                started=started,
                poll_log=poll_log,
                stderr=f"task {task_id} did not complete within {effective_poll_timeout_s}s "
                f"(last_status={last_status})",
                model=model,
                attempt_output_name=output_path.name,
                task_id=task_id,
                clamp_note=_clamp_note(duration_s, actual_duration),
                poll_timeout_s=effective_poll_timeout_s,
                last_status=last_status,
            )

        # 3) download
        try:
            self._download(video_url, output_path)
        except urllib.error.HTTPError as exc:
            return _failure(
                stage="download",
                started=started,
                poll_log=poll_log,
                stderr=http_error_body(exc),
                model=model,
                attempt_output_name=output_path.name,
                task_id=task_id,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        size_bytes = output_path.stat().st_size
        if size_bytes == 0:
            return _failure(
                stage="download:empty",
                started=started,
                poll_log=poll_log,
                stderr=f"downloaded file is 0 bytes: {output_path}",
                model=model,
                attempt_output_name=output_path.name,
                task_id=task_id,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        md5 = md5_file(output_path)
        latency = round(time.time() - started, 3)
        note = _clamp_note(duration_s, actual_duration)

        return {
            "status": "ok",
            "provider": _provider_from_model(model),
            "model": model,
            "task_id": task_id,
            "render_path": str(output_path),
            "render_md5": md5,
            "output_size_bytes": size_bytes,
            "cost_usd": 0.0,  # Wan is free-quota-metered
            "quota_cost": 1,  # conservative: 1 call = 1 quota unit
            "latency_s": latency,
            "requested_duration_s": duration_s,
            "actual_duration_s": actual_duration,
            "resolution": resolution,
            "poll_timeout_s": effective_poll_timeout_s,
            "stdout_tail": json.dumps(poll_log)[-500:],
            "stderr_tail": "",
            **({"note": note} if note else {}),
        }

    # -- HTTP helpers ------------------------------------------------------

    def _post_json(
        self,
        url: str,
        api_key: str,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with self._urlopen(req, timeout=self._http_timeout_s) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))

    def _get_json(self, url: str, api_key: str) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            method="GET",
        )
        with self._urlopen(req, timeout=self._http_timeout_s) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))

    def _download(self, url: str, dest: Path) -> None:
        with self._urlopen(url, timeout=self._http_timeout_s) as resp, open(
            dest, "wb"
        ) as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)


# ---------------------------------------------------------------------------
# Helpers — module-level, pure
# ---------------------------------------------------------------------------


def _resolve_dashscope_key() -> str | None:
    """Resolve DASHSCOPE_API_KEY from shell env, then project .env."""
    return resolve_env_key("DASHSCOPE_API_KEY")


def _snap_duration(requested_s: int) -> int:
    """Wan supports {5, 10} seconds on both the 2.6 and 2.7 text-to-video
    endpoints. Snap upward so we never under-render the shot's planned length."""
    if requested_s <= 5:
        return 5
    return 10


def _resolution_to_size(resolution: str) -> str:
    """Legacy (wan2.6) `size` parameter — width*height string."""
    r = resolution.strip().lower()
    return {"720p": "1280*720", "1080p": "1920*1080"}.get(r, "1280*720")


def _resolution_tier(resolution: str) -> str:
    """Wan 2.7 `resolution` parameter — tier string (`720P` / `1080P`)."""
    r = resolution.strip().lower()
    return {"720p": "720P", "1080p": "1080P"}.get(r, "720P")


def _build_parameters(model: str, resolution: str, duration_s: int) -> dict[str, Any]:
    """Construct the DashScope `parameters` object for a given model family.

    Wan 2.7 deprecated `size` in favour of `resolution` + `ratio`; Wan 2.6 and
    earlier still use `size`. Mismatches are rejected by the API, so the renderer
    has to branch here rather than paper over it.
    """
    if model.startswith("wan2.7"):
        return {
            "resolution": _resolution_tier(resolution),
            "ratio": "16:9",
            "duration": duration_s,
        }
    # wan2.6 and earlier — legacy protocol
    return {
        "size": _resolution_to_size(resolution),
        "duration": duration_s,
    }


def _provider_from_model(model: str) -> str:
    """Map DashScope model id to the router's provider_id convention.

    Provider slots are semantic (plus/turbo → finals/iteration), but model ids
    are canonical DashScope names. `wan2.7-*` fills the "plus" slot (higher
    fidelity); `wan2.6-*` fills the "turbo" slot (cheaper/faster iteration).
    """
    m = model.lower()
    if m.startswith("wan2.7"):
        return "alibaba_wan_2_7_plus"
    if m.startswith("wan2.6") or m.startswith("wan2.5") or m.startswith("wan2.2") or m.startswith("wan2.1"):
        return "alibaba_wan_2_7_turbo"
    return "alibaba_wan"


def _clamp_note(requested: int, actual: int) -> str:
    if requested == actual:
        return ""
    return f"duration clamped: requested {requested}s -> actual {actual}s (Wan supports {{5,10}})"


def _failure(
    *,
    stage: str,
    started: float,
    poll_log: list[dict[str, Any]],
    stderr: str,
    model: str,
    attempt_output_name: str,
    task_id: str | None = None,
    clamp_note: str = "",
    poll_timeout_s: int | None = None,
    last_status: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "failed",
        "failure_stage": stage,
        "provider": _provider_from_model(model),
        "model": model,
        "render_path": "",
        "render_md5": None,
        "output_size_bytes": 0,
        "cost_usd": 0.0,
        "quota_cost": 0,
        "latency_s": round(time.time() - started, 3),
        "stdout_tail": json.dumps(poll_log)[-500:],
        "stderr_tail": stderr[-500:] if stderr else "",
    }
    if task_id:
        payload["task_id"] = task_id
    if clamp_note:
        payload["note"] = clamp_note
    # Timeout failures surface the budget + last-seen provider state as
    # first-class fields so downstream code (judge/summary/orchestrator)
    # can distinguish "provider slow, consider bumping timeout" from
    # "provider rejected (FAILED/CANCELED)" without parsing stderr.
    if poll_timeout_s is not None:
        payload["poll_timeout_s"] = poll_timeout_s
    if last_status is not None:
        payload["last_status"] = last_status
    return payload
