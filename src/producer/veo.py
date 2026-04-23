"""VeoRendererTool — Vertex AI Veo 3.1 Fast text/image-to-video adapter.

Tier-4 worker. Shares Tool-Protocol shape with WanRendererTool / KlingRendererTool
so the Producer can dispatch it interchangeably; only the provider surface differs.

Verified 2026-04-23 against Google's Vertex AI Veo docs:
    - Submit : POST {LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/
               locations/{LOCATION}/publishers/google/models/{MODEL}:predictLongRunning
    - Poll   : POST ...:fetchPredictOperation  with body {"operationName": <name>}
               (Veo uses a sibling method, NOT the generic GET operations/<id>.)
    - Auth   : Bearer <ADC token>  (google-auth, scope cloud-platform)
    - Result : response.videos[0].bytesBase64Encoded  (if storageUri omitted)
               or response.videos[0].gcsUri (if a GCS bucket was provided)
    - Duration: discrete {4, 6, 8}s only for Veo 3.x — adapter snaps upward
    - `generateAudio: false` drops price from $0.15/s to $0.10/s; we always
      force audio off because ElevenLabs owns the audio track.
    - Content rejects come back on a HTTP 200 completed operation with
      `raiMediaFilteredCount > 0` — billed anyway, NOT retryable verbatim.

Router hard rule (humans_never_veo) is the primary gate for person content. The
adapter still passes `personGeneration="disallow"` as a belt+braces double-gate;
note that EU/UK/CH/MENA regions reject `disallow` and force `allow_adult`. For
us-central1 (our default) disallow works.

Dependencies:
    `google-auth` (>=2.25) for ADC. Imported lazily inside the default token
    provider so the rest of the package stays importable without it.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from ._common import http_error_body, md5_file, resolve_env_key


DEFAULT_LOCATION = "us-central1"
DEFAULT_MODEL_ID = "veo-3.1-fast-generate-001"
DEFAULT_POLL_INTERVAL_S = 12
DEFAULT_POLL_TIMEOUT_S = 300       # 5 min — Fast typically 30-90s; long tail ~3 min
DEFAULT_HTTP_TIMEOUT_S = 60

PROVIDER_ID = "vertex_veo_3_1_fast"

# Veo 3.x supports only these durations.
VALID_DURATIONS_S = (4, 6, 8)


class VeoRendererTool:
    """Vertex AI Veo 3.1 Fast adapter.

    Tool Protocol: `name == "renderer"`, `__call__(shot_id, payload) -> dict`.

    Constructor args:
        project_id       : GCP project id; fallback env GCP_PROJECT_ID or
                           GOOGLE_CLOUD_PROJECT
        location         : GCP region, default us-central1
        model_id         : default veo-3.1-fast-generate-001
        token_provider   : zero-arg callable returning a fresh bearer token.
                           Default uses google-auth ADC. Tests pass a stub.
        storage_uri_root : optional "gs://bucket/prefix" — if set, Veo writes
                           directly to GCS and the adapter downloads from there.
                           If None, adapter requests inline base64 and decodes.

    Payload fields:
        prompt             : primary prompt text
        negative_prompt    : optional — Veo 3.1 Fast supports it
        duration_s         : int; snaps to {4, 6, 8}
        resolution         : "720p" | "1080p" (default 720p)
        aspect_ratio       : "16:9" | "9:16" (default 16:9)
        seed               : optional int
        output_dir         : directory to save artifacts/renders/{shot_id}/
        attempt_id         : 1-indexed int, appended to the output filename
        image_url          : optional — base64 image for I2V mode
    """

    name = "renderer"

    def __init__(
        self,
        *,
        project_id: str | None = None,
        location: str = DEFAULT_LOCATION,
        model_id: str = DEFAULT_MODEL_ID,
        token_provider: Callable[[], str] | None = None,
        storage_uri_root: str | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        sleep: Any = time.sleep,
        urlopen: Any = None,
    ) -> None:
        self._project_id = project_id
        self._location = location
        self._model_id = model_id
        self._token_provider = token_provider or _default_token_provider
        self._storage_uri_root = storage_uri_root.rstrip("/") if storage_uri_root else None
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
            raise ValueError("VeoRendererTool requires a shot_id")

        project_id = self._project_id or resolve_env_key(
            "GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT"
        )
        if not project_id:
            raise RuntimeError(
                "GCP project id missing: set GCP_PROJECT_ID or pass project_id= "
                "(Veo requires projects/<ID>/locations/<LOC>/... in the URL)"
            )

        prompt = payload["prompt"]
        output_dir = Path(payload["output_dir"])
        attempt_id = int(payload["attempt_id"])
        duration_s = int(payload.get("duration_s", 8))
        negative_prompt = payload.get("negative_prompt") or ""
        resolution = payload.get("resolution", "720p")
        aspect_ratio = payload.get("aspect_ratio", "16:9")
        seed = payload.get("seed")
        image_b64 = payload.get("image_base64")   # optional I2V
        image_mime = payload.get("image_mime", "image/jpeg")

        actual_duration = _snap_duration(duration_s)

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"v{attempt_id}.mp4"

        started = time.time()
        poll_log: list[dict[str, Any]] = []

        try:
            token = self._token_provider()
        except Exception as exc:
            return _failure(
                stage="auth",
                started=started,
                poll_log=poll_log,
                stderr=f"token_provider raised: {exc!r}"[:500],
                model=self._model_id,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        submit_url = _build_submit_url(self._location, project_id, self._model_id)
        poll_url = _build_poll_url(self._location, project_id, self._model_id)

        instance: dict[str, Any] = {"prompt": prompt}
        if image_b64:
            instance["image"] = {
                "bytesBase64Encoded": image_b64,
                "mimeType": image_mime,
            }

        parameters: dict[str, Any] = {
            "sampleCount": 1,
            "aspectRatio": aspect_ratio,
            "durationSeconds": actual_duration,
            "resolution": resolution,
            # ElevenLabs owns audio; disable Veo audio and pay the $0.10/s tier.
            "generateAudio": False,
            # Belt+braces — router is the primary gate. us-central1 accepts
            # "disallow"; EU locations would 400 on this.
            "personGeneration": "disallow",
        }
        if negative_prompt:
            parameters["negativePrompt"] = negative_prompt
        if seed is not None:
            parameters["seed"] = int(seed)
        if self._storage_uri_root:
            parameters["storageUri"] = f"{self._storage_uri_root}/{shot_id}/"

        body = {"instances": [instance], "parameters": parameters}

        # 1) submit — returns {"name": "projects/.../operations/<OP>"}.
        try:
            submit_resp = self._post_json(submit_url, token, body)
        except urllib.error.HTTPError as exc:
            body_text = http_error_body(exc)
            stage = _submit_failure_stage(exc.code, body_text)
            return _failure(
                stage=stage,
                started=started,
                poll_log=poll_log,
                stderr=body_text,
                model=self._model_id,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        operation_name = submit_resp.get("name")
        if not operation_name:
            return _failure(
                stage="submit:malformed",
                started=started,
                poll_log=poll_log,
                stderr=f"submit missing operation name: {submit_resp!r}"[:500],
                model=self._model_id,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        # 2) poll operation — Veo uses POST :fetchPredictOperation with the
        # operation name in the body. `done: true` signals completion.
        deadline = started + self._poll_timeout_s
        done_resp: dict[str, Any] | None = None
        while time.time() < deadline:
            try:
                # Refresh token if it might have expired on a long poll.
                if time.time() - started > 50:
                    token = self._token_provider()
                poll_resp = self._post_json(
                    poll_url, token, {"operationName": operation_name}
                )
            except urllib.error.HTTPError as exc:
                poll_log.append(
                    {"t": round(time.time() - started, 2), "http_error": exc.code}
                )
                self._sleep(self._poll_interval_s)
                continue

            done = bool(poll_resp.get("done"))
            poll_log.append({"t": round(time.time() - started, 2), "done": done})
            if done:
                done_resp = poll_resp
                break
            self._sleep(self._poll_interval_s)
        else:
            return _failure(
                stage="poll:timeout",
                started=started,
                poll_log=poll_log,
                stderr=f"operation {operation_name} did not complete within "
                f"{self._poll_timeout_s}s",
                model=self._model_id,
                task_id=operation_name,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        assert done_resp is not None

        # 3) check for error, content filter, or response payload.
        err = done_resp.get("error")
        if err:
            return _failure(
                stage="operation:error",
                started=started,
                poll_log=poll_log,
                stderr=json.dumps(err)[:500],
                model=self._model_id,
                task_id=operation_name,
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        response = done_resp.get("response") or {}
        filtered_count = int(response.get("raiMediaFilteredCount", 0))
        filtered_reasons = response.get("raiMediaFilteredReasons") or []
        videos = response.get("videos") or []

        if filtered_count > 0 or not videos:
            return _failure(
                stage="content_policy",
                started=started,
                poll_log=poll_log,
                stderr=f"raiMediaFilteredCount={filtered_count} reasons={filtered_reasons}"[:500],
                model=self._model_id,
                task_id=operation_name,
                # Veo bills filtered samples anyway — surface the cost so the
                # budget layer can reconcile.
                billed_cost_usd=_cost_for(self._model_id, actual_duration),
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        video = videos[0]

        # 4) materialize the MP4 — either inline base64 or a gs:// URI.
        try:
            if "bytesBase64Encoded" in video:
                output_path.write_bytes(base64.b64decode(video["bytesBase64Encoded"]))
            elif "gcsUri" in video:
                self._download_gcs(video["gcsUri"], token, output_path)
            else:
                return _failure(
                    stage="result_malformed",
                    started=started,
                    poll_log=poll_log,
                    stderr=f"video[0] missing bytesBase64Encoded and gcsUri: {video!r}"[:500],
                    model=self._model_id,
                    task_id=operation_name,
                    billed_cost_usd=_cost_for(self._model_id, actual_duration),
                    clamp_note=_clamp_note(duration_s, actual_duration),
                )
        except urllib.error.HTTPError as exc:
            return _failure(
                stage="download",
                started=started,
                poll_log=poll_log,
                stderr=http_error_body(exc),
                model=self._model_id,
                task_id=operation_name,
                billed_cost_usd=_cost_for(self._model_id, actual_duration),
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        size_bytes = output_path.stat().st_size
        if size_bytes == 0:
            return _failure(
                stage="download:empty",
                started=started,
                poll_log=poll_log,
                stderr=f"materialized file is 0 bytes: {output_path}",
                model=self._model_id,
                task_id=operation_name,
                billed_cost_usd=_cost_for(self._model_id, actual_duration),
                clamp_note=_clamp_note(duration_s, actual_duration),
            )

        md5 = md5_file(output_path)
        latency = round(time.time() - started, 3)
        cost = _cost_for(self._model_id, actual_duration)

        return {
            "status": "ok",
            "provider": PROVIDER_ID,
            "model": self._model_id,
            "task_id": operation_name,
            "render_path": str(output_path),
            "render_md5": md5,
            "output_size_bytes": size_bytes,
            "cost_usd": cost,
            "quota_cost": 0,
            "latency_s": latency,
            "requested_duration_s": duration_s,
            "actual_duration_s": actual_duration,
            "resolution": resolution,
            "stdout_tail": json.dumps(poll_log)[-500:],
            "stderr_tail": "",
            **({"note": _clamp_note(duration_s, actual_duration)}
               if _clamp_note(duration_s, actual_duration) else {}),
        }

    # -- HTTP helpers ------------------------------------------------------

    def _post_json(
        self,
        url: str,
        token: str,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with self._urlopen(req, timeout=self._http_timeout_s) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))

    def _download_gcs(self, gcs_uri: str, token: str, dest: Path) -> None:
        """Download a gs://bucket/object URI via the JSON API with bearer auth."""
        if not gcs_uri.startswith("gs://"):
            raise ValueError(f"not a gs:// URI: {gcs_uri}")
        bucket, _, object_path = gcs_uri[len("gs://"):].partition("/")
        if not object_path:
            raise ValueError(f"gs:// URI missing object path: {gcs_uri}")
        url = (
            f"https://storage.googleapis.com/storage/v1/b/{bucket}"
            f"/o/{urllib.parse.quote(object_path, safe='')}?alt=media"
        )
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}, method="GET"
        )
        with self._urlopen(req, timeout=self._http_timeout_s) as resp, open(
            dest, "wb"
        ) as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------


def _build_submit_url(location: str, project_id: str, model_id: str) -> str:
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project_id}/locations/{location}/"
        f"publishers/google/models/{model_id}:predictLongRunning"
    )


def _build_poll_url(location: str, project_id: str, model_id: str) -> str:
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project_id}/locations/{location}/"
        f"publishers/google/models/{model_id}:fetchPredictOperation"
    )


# ---------------------------------------------------------------------------
# Default ADC token provider (lazy import of google-auth)
# ---------------------------------------------------------------------------


def _default_token_provider() -> str:
    """Get a fresh ADC bearer token via google-auth. Lazy import so importing
    this module doesn't require google-auth unless Veo actually gets invoked."""
    try:
        from google.auth import default as _default_creds  # type: ignore
        from google.auth.transport.requests import Request as _AuthRequest  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "VeoRendererTool requires google-auth>=2.25 "
            "(install via `pip install google-auth`) or pass a custom "
            "token_provider=... to the constructor"
        ) from exc
    creds, _ = _default_creds(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(_AuthRequest())
    if not creds.token:
        raise RuntimeError(
            "ADC produced no token — run `gcloud auth application-default login` "
            "or point GOOGLE_APPLICATION_CREDENTIALS at a service-account key"
        )
    return creds.token


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _snap_duration(requested_s: int) -> int:
    """Veo 3.x accepts {4, 6, 8}. Snap UP to the nearest legal value so we never
    under-render the shot's planned length."""
    for v in VALID_DURATIONS_S:
        if requested_s <= v:
            return v
    return VALID_DURATIONS_S[-1]


def _cost_for(model: str, duration_s: int) -> float:
    """Veo 3.1 Fast: $0.10/s with audio off (we force audio off). $0.15/s with
    audio — but the adapter never enables audio. Keep a single branch."""
    if "fast" in model.lower():
        return round(0.10 * duration_s, 4)
    # Veo 3.1 non-fast: $0.40/s.
    return round(0.40 * duration_s, 4)


def _submit_failure_stage(http_code: int, body_text: str) -> str:
    """Classify submit-time errors. Content policy would be rare at submit
    (Veo rejects at the operation), but INVALID_ARGUMENT for unsupported
    personGeneration (EU) or bad duration shows up here."""
    if http_code == 429:
        return "rate_limit"
    if http_code == 400:
        return "validation"
    if http_code in (401, 403):
        return "auth"
    return f"submit:http_{http_code}"


def _clamp_note(requested: int, actual: int) -> str:
    if requested == actual:
        return ""
    return (
        f"duration clamped: requested {requested}s -> actual {actual}s "
        f"(Veo accepts {{4,6,8}})"
    )


def _failure(
    *,
    stage: str,
    started: float,
    poll_log: list[dict[str, Any]],
    stderr: str,
    model: str,
    task_id: str | None = None,
    billed_cost_usd: float = 0.0,
    clamp_note: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "failed",
        "failure_stage": stage,
        "provider": PROVIDER_ID,
        "model": model,
        "render_path": "",
        "render_md5": None,
        "output_size_bytes": 0,
        # Veo's key gotcha: filtered samples ARE billed, so content_policy
        # failures pass a non-zero cost up for the Producer to record against
        # the spend cap.
        "cost_usd": billed_cost_usd,
        "quota_cost": 0,
        "latency_s": round(time.time() - started, 3),
        "stdout_tail": json.dumps(poll_log)[-500:],
        "stderr_tail": stderr[-500:] if stderr else "",
    }
    if task_id:
        payload["task_id"] = task_id
    if clamp_note:
        payload["note"] = clamp_note
    return payload

