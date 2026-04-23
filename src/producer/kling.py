"""KlingRendererTool — fal.ai Kling 2.x image-to-video adapter.

Tier-4 worker. Shares shape + contract with src.producer.renderer.WanRendererTool
so the Producer can dispatch it interchangeably; only the provider surface differs.

Verified 2026-04-23 against fal.ai docs:
    - Submit : POST https://queue.fal.run/fal-ai/kling-video/v2.1/{standard|pro}/image-to-video
    - Status : GET  {status_url from submit}  (or /requests/{id}/status?logs=1)
    - Result : GET  {response_url from submit}
    - Auth   : header 'Authorization: Key <FAL_KEY>' — literal word "Key", NOT "Bearer"
    - Kling 2.1 is I2V-only: `image_url` is MANDATORY. A shot with no
      reference image cannot go to Kling; adapter refuses loudly.
    - `duration` is stringified: `"5"` or `"10"`. No arbitrary values.
    - `tail_image_url` (first+last frame) is Pro-only.

Failover:
    Two fal keys are supported (primary + backup). The adapter catches auth
    and rate-limit errors (401/403/429) on the primary and retries the submit
    once against the backup. Anything else fails through with failure_stage.

Pricing (fal pages, 2026-04-23):
    standard: $0.25 base for 5s + $0.05 per additional second
    pro     : $0.49 base for 5s + $0.098 per additional second
Cost is computed from the actual duration returned by fal, not the requested
one, so a clamp from 10s → 5s is billed honestly.

Policy reject surface: HTTP 422 with
    {"detail": [{..., "type": "content_policy_violation", ...}]}
→ failure_stage="content_policy" and stdout_tail keeps the raw detail.
Non-retryable by design; re-running the same prompt will fail the same way.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from ._common import http_error_body, md5_file, resolve_env_key


FAL_QUEUE_BASE = "https://queue.fal.run"
DEFAULT_POLL_INTERVAL_S = 8
DEFAULT_POLL_TIMEOUT_S = 600       # Kling 5s generations run ~3-5 minutes typical
DEFAULT_HTTP_TIMEOUT_S = 60

# Fields in the fal input schema for Kling 2.1 I2V (verified via model-page API tab).
SUPPORTED_DURATIONS = ("5", "10")

# Provider-slot IDs in router/capabilities.yaml.
PROVIDER_STD = "fal_kling_2_1_standard"
PROVIDER_PRO = "fal_kling_2_1_pro"


class KlingRendererTool:
    """fal.ai Kling 2.x I2V adapter.

    Tool Protocol: `name == "renderer"`, `__call__(shot_id, payload) -> dict`.

    Payload fields:
        model              : fal model id, e.g.
                             "fal-ai/kling-video/v2.1/standard/image-to-video"
                             or        ".../pro/image-to-video"
        prompt             : primary prompt text
        image_url          : absolute https URL, or data:image/*;base64,... URI.
                             REQUIRED — Kling 2.1 refuses pure text-to-video.
        duration_s         : int; snaps to fal's accepted "5" or "10".
        negative_prompt    : optional; defaults to fal's baked-in negatives
                             if absent
        tail_image_url     : optional; first+last-frame control (Pro tier only)
        cfg_scale          : optional float, default 0.5 (fal default)
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
            raise ValueError("KlingRendererTool requires a shot_id")

        primary, backup = self._resolve_keys()
        if not primary:
            raise RuntimeError(
                "FAL_KEY missing from env and .env; cannot submit Kling job"
            )

        model = payload["model"]
        prompt = payload["prompt"]
        image_url = payload.get("image_url")
        if not image_url:
            raise ValueError(
                "Kling 2.1 is image-to-video only: payload['image_url'] is required. "
                "Provide a public URL or data: URI (use kling.encode_image_as_data_uri "
                "for a local path)."
            )

        output_dir = Path(payload["output_dir"])
        attempt_id = int(payload["attempt_id"])
        duration_s = int(payload.get("duration_s", 5))
        negative_prompt = payload.get("negative_prompt") or ""
        tail_image_url = payload.get("tail_image_url")
        cfg_scale = float(payload.get("cfg_scale", 0.5))

        actual_duration = _snap_duration(duration_s)

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"v{attempt_id}.mp4"

        started = time.time()
        poll_log: list[dict[str, Any]] = []

        body: dict[str, Any] = {
            "prompt": prompt,
            "image_url": image_url,
            "duration": actual_duration,
            "cfg_scale": cfg_scale,
        }
        if negative_prompt:
            body["negative_prompt"] = negative_prompt
        if tail_image_url:
            # Pro-only field; Standard will 422 on this. Router should never
            # route a tail-image shot to Standard (end_frame_requires_capable_provider).
            body["tail_image_url"] = tail_image_url

        submit_url = f"{self._queue_base}/{model}"

        # 1) submit (with one-shot key failover on 401/403/429).
        submit_resp, used_key, submit_err = self._submit_with_failover(
            submit_url, body, primary, backup
        )
        if submit_err is not None:
            return _failure(
                stage=submit_err["stage"],
                started=started,
                poll_log=poll_log,
                stderr=submit_err["stderr"],
                model=model,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        request_id = submit_resp.get("request_id")
        status_url = submit_resp.get("status_url")
        response_url = submit_resp.get("response_url")
        if not (request_id and status_url and response_url):
            return _failure(
                stage="submit:malformed",
                started=started,
                poll_log=poll_log,
                stderr=f"submit missing request_id/status_url/response_url: {submit_resp!r}"[:500],
                model=model,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        # 2) poll status until COMPLETED.
        deadline = started + self._poll_timeout_s
        last_status: str | None = None
        while time.time() < deadline:
            try:
                status_resp = self._get_json(status_url, used_key)
            except urllib.error.HTTPError as exc:
                poll_log.append(
                    {"t": round(time.time() - started, 2), "http_error": exc.code}
                )
                self._sleep(self._poll_interval_s)
                continue

            status = status_resp.get("status")
            last_status = status
            poll_log.append(
                {"t": round(time.time() - started, 2), "status": status}
            )
            if status == "COMPLETED":
                break
            if status in ("ERROR", "FAILED", "CANCELLED"):
                return _failure(
                    stage=f"poll:{status}",
                    started=started,
                    poll_log=poll_log,
                    stderr=str(status_resp.get("error") or status_resp)[:500],
                    model=model,
                    request_id=request_id,
                    clamp_note=_clamp_note(duration_s, actual_duration),
                )
            self._sleep(self._poll_interval_s)
        else:
            return _failure(
                stage="poll:timeout",
                started=started,
                poll_log=poll_log,
                stderr=f"request {request_id} did not complete within {self._poll_timeout_s}s "
                f"(last_status={last_status})",
                model=model,
                request_id=request_id,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        # 3) fetch result payload
        try:
            result = self._get_json(response_url, used_key)
        except urllib.error.HTTPError as exc:
            return _failure(
                stage="result_fetch",
                started=started,
                poll_log=poll_log,
                stderr=http_error_body(exc),
                model=model,
                request_id=request_id,
                clamp_note=_clamp_note(duration_s, actual_duration),
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
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        # 4) download the MP4 — fal URLs are ephemeral, so do this immediately
        try:
            self._download(video_url, output_path)
        except urllib.error.HTTPError as exc:
            return _failure(
                stage="download",
                started=started,
                poll_log=poll_log,
                stderr=http_error_body(exc),
                model=model,
                request_id=request_id,
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
                request_id=request_id,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        md5 = md5_file(output_path)
        latency = round(time.time() - started, 3)
        cost = _cost_for(model, actual_duration)
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
            "requested_duration_s": duration_s,
            "actual_duration_s": int(actual_duration),
            "stdout_tail": json.dumps(poll_log)[-500:],
            "stderr_tail": "",
            **({"note": _clamp_note(duration_s, actual_duration)}
               if _clamp_note(duration_s, actual_duration) else {}),
        }

    # -- HTTP helpers ------------------------------------------------------

    def _submit_with_failover(
        self,
        url: str,
        body: Mapping[str, Any],
        primary_key: str,
        backup_key: str | None,
    ) -> tuple[dict[str, Any], str, dict[str, str] | None]:
        """Submit on primary; on 401/403/429 (if backup exists), swap and retry once.

        Returns (response_body, successful_key, None) on success, or
        ({}, primary_key, {stage, stderr}) on terminal failure.
        """
        try:
            resp = self._post_json(url, primary_key, body)
            return resp, primary_key, None
        except urllib.error.HTTPError as exc:
            retryable = exc.code in (401, 403, 429) and backup_key is not None
            body_text = http_error_body(exc)
            policy = _is_content_policy_violation(body_text)
            if policy:
                return {}, primary_key, {
                    "stage": "content_policy",
                    "stderr": body_text,
                }
            if not retryable:
                return {}, primary_key, {
                    "stage": f"submit:http_{exc.code}",
                    "stderr": body_text,
                }
            try:
                resp = self._post_json(url, backup_key, body)  # type: ignore[arg-type]
                return resp, backup_key, None  # type: ignore[return-value]
            except urllib.error.HTTPError as exc2:
                body_text2 = http_error_body(exc2)
                if _is_content_policy_violation(body_text2):
                    return {}, backup_key or primary_key, {  # type: ignore[return-value]
                        "stage": "content_policy",
                        "stderr": body_text2,
                    }
                return {}, backup_key or primary_key, {  # type: ignore[return-value]
                    "stage": f"submit:http_{exc2.code}:backup_also_failed",
                    "stderr": body_text2,
                }

    def _post_json(
        self,
        url: str,
        api_key: str,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Key {api_key}",  # fal uses literal 'Key', not 'Bearer'
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with self._urlopen(req, timeout=self._http_timeout_s) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))

    def _get_json(self, url: str, api_key: str) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Key {api_key}"},
            method="GET",
        )
        with self._urlopen(req, timeout=self._http_timeout_s) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))

    def _download(self, url: str, dest: Path) -> None:
        # fal video URLs require no auth header; they're signed ephemeral URLs.
        with self._urlopen(url, timeout=self._http_timeout_s) as resp, open(
            dest, "wb"
        ) as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)

    # -- Key resolution ----------------------------------------------------

    def _resolve_keys(self) -> tuple[str | None, str | None]:
        """Resolve primary + backup keys. Constructor args win over env/.env.

        Looks up multiple env var names — fal's canonical `FAL_KEY` plus the
        project's existing `FAL_KEY_PRIMARY`/`FAL_KEY_SECONDARY` convention so
        both work without a rename.
        """
        primary = self._primary_key or resolve_env_key("FAL_KEY", "FAL_KEY_PRIMARY")
        backup = self._backup_key or resolve_env_key(
            "FAL_KEY_2", "FAL_KEY_BACKUP", "FAL_KEY_SECONDARY"
        )
        return primary, backup


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def encode_image_as_data_uri(path: str | Path) -> str:
    """Convert a local image to a `data:image/...;base64,...` URI fal accepts.

    Use this when the shot's reference_subject_paths[0] is a local file and
    we don't have a public-URL upload step in the pipeline yet.
    """
    p = Path(path)
    mime, _ = mimetypes.guess_type(str(p))
    if mime is None:
        mime = "image/jpeg"
    data = p.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _snap_duration(requested_s: int) -> int:
    """Kling 2.1 accepts exactly {5, 10}. Snap upward so we never under-render."""
    if requested_s <= 5:
        return 5
    return 10


def _cost_for(model: str, duration_s: int) -> float:
    """Kling cost = base_5s + max(0, duration_s - 5) * per_second_usd.
    Numbers match router/capabilities.yaml; we duplicate rather than parse the
    YAML at runtime because this adapter has no other reason to depend on it."""
    m = model.lower()
    if "/pro/" in m:
        return round(0.49 + max(0, duration_s - 5) * 0.098, 4)
    # Default: standard tier
    return round(0.25 + max(0, duration_s - 5) * 0.05, 4)


def _provider_from_model(model: str) -> str:
    m = model.lower()
    if "/pro/" in m:
        return PROVIDER_PRO
    if "/standard/" in m:
        return PROVIDER_STD
    return "fal_kling"


def _is_content_policy_violation(body_text: str) -> bool:
    """fal returns 422 with a `detail` list; policy rejects carry type=
    'content_policy_violation'. Return True if ANY entry has that type."""
    if "content_policy_violation" not in body_text:
        return False
    try:
        obj = json.loads(body_text)
    except (ValueError, TypeError):
        return False
    detail = obj.get("detail")
    if not isinstance(detail, list):
        return False
    return any(
        isinstance(d, dict) and d.get("type") == "content_policy_violation"
        for d in detail
    )


def _clamp_note(requested: int, actual: int) -> str:
    if requested == actual:
        return ""
    return f"duration clamped: requested {requested}s -> actual {actual}s (Kling supports {{5,10}})"


def _failure(
    *,
    stage: str,
    started: float,
    poll_log: list[dict[str, Any]],
    stderr: str,
    model: str,
    request_id: str | None = None,
    clamp_note: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "failed",
        "failure_stage": stage,
        "provider": _provider_from_model(model),
        "model": model,
        "render_path": "",
        "render_md5": None,
        "output_size_bytes": 0,
        "cost_usd": 0.0,     # fal only bills on COMPLETED, so a failed submit is free
        "quota_cost": 0,
        "latency_s": round(time.time() - started, 3),
        "stdout_tail": json.dumps(poll_log)[-500:],
        "stderr_tail": stderr[-500:] if stderr else "",
    }
    if request_id:
        payload["task_id"] = request_id
    if clamp_note:
        payload["note"] = clamp_note
    return payload
