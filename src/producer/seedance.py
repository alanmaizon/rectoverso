"""SeedanceRendererTool — fal.ai ByteDance Seedance 2.0 video-gen adapter.

Submission-tier alternative to Runway (which is not on fal.ai as of 2026-04-23).
Seedance 2.0 is ByteDance's current video-gen family; it targets the quality
bracket Runway Gen-4 occupies but stays on fal's queue API so we reuse the
same auth + failover + download plumbing as KlingRendererTool via `_fal.py`.

Verified 2026-04-23 via fal.ai model pages:
    - Queue base   : https://queue.fal.run/{model_id}
    - Auth         : `Authorization: Key <FAL_KEY>`  (literal "Key", not Bearer)
    - Failover     : primary + backup fal keys on 401/403/429
    - Policy       : 422 with `detail[].type == "content_policy_violation"`
    - Latency      : "under 2 minutes" SLA per fal; we budget 180s

Variants the adapter dispatches (caller picks via payload["model"]):
    bytedance/seedance-2.0/image-to-video          — quality I2V   ($0.3024/s)
    bytedance/seedance-2.0/text-to-video           — quality T2V   ($0.3034/s)
    bytedance/seedance-2.0/reference-to-video      — multi-ref     ($0.1814/s)
    bytedance/seedance-2.0/fast/image-to-video     — cheap iter    ($0.2419/s)
    bytedance/seedance-2.0/fast/text-to-video      — cheap T2V
    bytedance/seedance-2.0/fast/reference-to-video — cheap multi-ref

Key gotchas vs Kling (hermetic tests lock these in):
    - `duration` is a STRING (`"5"`, not `5`). Seedance accepts any integer
      4-15 as a string, plus `"auto"`.
    - NO `negative_prompt`, NO `cfg_scale`. Don't send them; Seedance will 422.
    - `generate_audio` defaults TRUE on fal — we force FALSE so ElevenLabs
      owns the audio track (prevents paying Seedance for audio tokens we'll
      throw away).
    - Content policy is STRICTER than Kling: pre-generation likeness detector
      blocks recognizable real people / celebrities. Re-prompting rarely
      works — caller should fall back to Kling on content_policy rejection.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from ._common import http_error_body, md5_file
from ._fal import (
    FAL_QUEUE_BASE,
    download,
    get_json,
    poll_until_completed,
    resolve_fal_keys,
    submit_with_failover,
)


DEFAULT_POLL_INTERVAL_S = 6
DEFAULT_POLL_TIMEOUT_S = 240       # Seedance ~60-180s typical; 240 = headroom
DEFAULT_HTTP_TIMEOUT_S = 60

# Seedance accepts integer durations 4-15 (plus "auto"). Adapter clamps.
MIN_DURATION_S = 4
MAX_DURATION_S = 15

SUPPORTED_RESOLUTIONS = ("480p", "720p", "1080p")
SUPPORTED_ASPECTS = ("auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16")

# Provider-slot IDs surfaced in result["provider"]. These match the entries
# in router/capabilities.yaml so budget bookkeeping keys correctly.
PROVIDER_PRO_I2V = "fal_bytedance_seedance_2_0_i2v"
PROVIDER_PRO_T2V = "fal_bytedance_seedance_2_0_t2v"
PROVIDER_PRO_REF = "fal_bytedance_seedance_2_0_ref"
PROVIDER_FAST_I2V = "fal_bytedance_seedance_2_0_fast_i2v"
PROVIDER_FAST_T2V = "fal_bytedance_seedance_2_0_fast_t2v"
PROVIDER_FAST_REF = "fal_bytedance_seedance_2_0_fast_ref"


class SeedanceRendererTool:
    """fal.ai Seedance 2.0 adapter.

    Tool Protocol: `name == "renderer"`, `__call__(shot_id, payload) -> dict`.

    Payload fields:
        model              : fal model id. Mode inferred from the id:
                               ".../image-to-video"     → image_url required
                               ".../text-to-video"      → image_url forbidden
                               ".../reference-to-video" → reference_image_urls list
        prompt             : primary prompt text
        image_url          : absolute URL or data: URI for I2V
        reference_image_urls : optional list (reference-to-video only)
        end_image_url      : optional — first+last-frame continuity hook
        duration_s         : int 4-15 (clamped). Sent as string to fal.
        aspect_ratio       : "auto"|"21:9"|"16:9"|"4:3"|"1:1"|"3:4"|"9:16"
        resolution         : "480p"|"720p"|"1080p"  (default 720p)
        seed               : optional int
        generate_audio     : optional bool; defaults False (we let
                             ElevenLabs own audio to avoid double-billing)
        output_dir         : directory to save artifacts/renders/{shot_id}/
        attempt_id         : 1-indexed int, appended to the output filename
    """

    name = "renderer"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        backup_api_key: str | None = None,
        queue_base: str = FAL_QUEUE_BASE,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        sleep: Any = time.sleep,
        urlopen: Any = None,
    ) -> None:
        self._primary_key = api_key
        self._backup_key = backup_api_key
        self._queue_base = queue_base.rstrip("/")
        self._poll_interval_s = poll_interval_s
        self._poll_timeout_s = poll_timeout_s
        self._http_timeout_s = http_timeout_s
        self._sleep = sleep
        self._urlopen = urlopen or urllib.request.urlopen

    # -- Tool Protocol -----------------------------------------------------

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if shot_id is None:
            raise ValueError("SeedanceRendererTool requires a shot_id")

        primary, backup = resolve_fal_keys(self._primary_key, self._backup_key)
        if not primary:
            raise RuntimeError(
                "FAL_KEY missing from env and .env; cannot submit Seedance job"
            )

        model = payload["model"]
        mode = _mode_from_model(model)
        prompt = payload["prompt"]
        image_url = payload.get("image_url")
        reference_image_urls = payload.get("reference_image_urls") or []
        end_image_url = payload.get("end_image_url")

        if mode == "i2v" and not image_url:
            raise ValueError(
                f"{model} is image-to-video: payload['image_url'] is required."
            )
        if mode == "ref" and not reference_image_urls:
            raise ValueError(
                f"{model} is reference-to-video: payload['reference_image_urls'] "
                "(a non-empty list) is required."
            )

        output_dir = Path(payload["output_dir"])
        attempt_id = int(payload["attempt_id"])
        duration_s = _clamp_duration(int(payload.get("duration_s", 5)))
        aspect_ratio = str(payload.get("aspect_ratio", "16:9"))
        resolution = str(payload.get("resolution", "720p"))
        seed = payload.get("seed")
        generate_audio = bool(payload.get("generate_audio", False))

        if aspect_ratio not in SUPPORTED_ASPECTS:
            raise ValueError(
                f"aspect_ratio {aspect_ratio!r} not supported by Seedance; "
                f"use one of {list(SUPPORTED_ASPECTS)}"
            )
        if resolution not in SUPPORTED_RESOLUTIONS:
            raise ValueError(
                f"resolution {resolution!r} not supported by Seedance; "
                f"use one of {list(SUPPORTED_RESOLUTIONS)}"
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"v{attempt_id}.mp4"

        started = time.time()

        # Seedance body — note that `duration` is a STRING, not int. No
        # negative_prompt / cfg_scale fields exist; sending them 422s.
        body: dict[str, Any] = {
            "prompt": prompt,
            "duration": str(duration_s),
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "generate_audio": generate_audio,
        }
        if mode == "i2v" and image_url:
            body["image_url"] = image_url
        if mode == "ref" and reference_image_urls:
            body["reference_image_urls"] = list(reference_image_urls)
        if end_image_url:
            body["end_image_url"] = end_image_url
        if seed is not None:
            body["seed"] = int(seed)

        submit_url = f"{self._queue_base}/{model}"

        # 1) submit with failover.
        submit_resp, used_key, submit_err = submit_with_failover(
            self._urlopen,
            submit_url,
            body,
            primary,
            backup,
            http_timeout_s=self._http_timeout_s,
        )
        if submit_err is not None:
            return _failure(
                stage=submit_err["stage"],
                started=started,
                poll_log=[],
                stderr=submit_err["stderr"],
                model=model,
            )

        request_id = submit_resp.get("request_id")
        status_url = submit_resp.get("status_url")
        response_url = submit_resp.get("response_url")
        if not (request_id and status_url and response_url):
            return _failure(
                stage="submit:malformed",
                started=started,
                poll_log=[],
                stderr=f"submit missing request_id/status_url/response_url: {submit_resp!r}"[:500],
                model=model,
            )

        # 2) poll.
        status_resp, poll_log, poll_err = poll_until_completed(
            self._urlopen,
            status_url,
            used_key,
            sleep=self._sleep,
            poll_interval_s=self._poll_interval_s,
            started=started,
            poll_timeout_s=self._poll_timeout_s,
            http_timeout_s=self._http_timeout_s,
        )
        if poll_err is not None:
            return _failure(
                stage=poll_err,
                started=started,
                poll_log=poll_log,
                stderr=str(
                    (status_resp or {}).get("error") or status_resp
                    or f"request {request_id} timed out"
                )[:500],
                model=model,
                request_id=request_id,
            )

        # 3) fetch result.
        try:
            result = get_json(
                self._urlopen, response_url, used_key,
                http_timeout_s=self._http_timeout_s,
            )
        except urllib.error.HTTPError as exc:
            return _failure(
                stage="result_fetch",
                started=started,
                poll_log=poll_log,
                stderr=http_error_body(exc),
                model=model,
                request_id=request_id,
            )

        video = result.get("video") or {}
        video_url = video.get("url")
        if not video_url:
            return _failure(
                stage="result_malformed",
                started=started,
                poll_log=poll_log,
                stderr=f"result missing video.url: {result!r}"[:500],
                model=model,
                request_id=request_id,
            )

        # 4) download MP4. fal URLs are ephemeral; fetch immediately.
        try:
            download(
                self._urlopen, video_url, output_path,
                http_timeout_s=self._http_timeout_s,
            )
        except urllib.error.HTTPError as exc:
            return _failure(
                stage="download",
                started=started,
                poll_log=poll_log,
                stderr=http_error_body(exc),
                model=model,
                request_id=request_id,
            )

        size_bytes = output_path.stat().st_size
        if size_bytes == 0:
            return _failure(
                stage="download:empty",
                started=started,
                poll_log=poll_log,
                stderr=f"downloaded file is 0 bytes: {output_path}",
                model=model,
                request_id=request_id,
            )

        md5 = md5_file(output_path)
        latency = round(time.time() - started, 3)
        cost = _cost_for(model, duration_s, resolution)
        provider = _provider_from_model(model)

        return {
            "status": "ok",
            "provider": provider,
            "model": model,
            "task_id": request_id,
            "render_path": str(output_path),
            "render_md5": md5,
            "output_size_bytes": size_bytes,
            "cost_usd": cost,
            "quota_cost": 0,
            "latency_s": latency,
            "requested_duration_s": int(payload.get("duration_s", 5)),
            "actual_duration_s": duration_s,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "stdout_tail": json.dumps(poll_log)[-500:],
            "stderr_tail": "",
        }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _clamp_duration(requested_s: int) -> int:
    """Seedance accepts int durations 4-15. Clamp into range; don't snap."""
    if requested_s < MIN_DURATION_S:
        return MIN_DURATION_S
    if requested_s > MAX_DURATION_S:
        return MAX_DURATION_S
    return requested_s


def _mode_from_model(model: str) -> str:
    """Return "i2v" | "t2v" | "ref" based on the fal model id."""
    m = model.lower()
    if "reference-to-video" in m:
        return "ref"
    if "image-to-video" in m:
        return "i2v"
    if "text-to-video" in m:
        return "t2v"
    return "i2v"  # conservative default


def _cost_for(model: str, duration_s: int, resolution: str) -> float:
    """Seedance pricing on fal.ai (2026-04-23). Per-second; 1080p ≈ 2.25× 720p.

    These numbers are kept in sync with router/capabilities.yaml — if fal
    changes them, update both.
    """
    m = model.lower()
    is_fast = "/fast/" in m
    if "reference-to-video" in m:
        base = 0.1814 if is_fast else 0.1814
    elif "text-to-video" in m:
        base = 0.2419 if is_fast else 0.3034
    else:  # image-to-video
        base = 0.2419 if is_fast else 0.3024
    multiplier = {"480p": 0.44, "720p": 1.0, "1080p": 2.25}.get(resolution, 1.0)
    return round(base * duration_s * multiplier, 4)


def _provider_from_model(model: str) -> str:
    m = model.lower()
    is_fast = "/fast/" in m
    if "reference-to-video" in m:
        return PROVIDER_FAST_REF if is_fast else PROVIDER_PRO_REF
    if "text-to-video" in m:
        return PROVIDER_FAST_T2V if is_fast else PROVIDER_PRO_T2V
    # image-to-video (or unknown — default to I2V slot)
    return PROVIDER_FAST_I2V if is_fast else PROVIDER_PRO_I2V


def _failure(
    *,
    stage: str,
    started: float,
    poll_log: list[dict[str, Any]],
    stderr: str,
    model: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "failed",
        "failure_stage": stage,
        "provider": _provider_from_model(model),
        "model": model,
        "render_path": "",
        "render_md5": None,
        "output_size_bytes": 0,
        "cost_usd": 0.0,  # fal only bills on COMPLETED
        "quota_cost": 0,
        "latency_s": round(time.time() - started, 3),
        "stdout_tail": json.dumps(poll_log)[-500:],
        "stderr_tail": stderr[-500:] if stderr else "",
    }
    if request_id:
        payload["task_id"] = request_id
    return payload
