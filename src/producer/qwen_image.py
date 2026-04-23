"""QwenImageTool — DashScope Qwen-Image text-to-image adapter.

Generates a reference image for a video shot that needs a subject-containing
start frame. Feeds directly into the Kling I2V path: adapter saves a PNG to
`artifacts/refs/{shot_id}.png`, caller base64-encodes it via
`kling.encode_image_as_data_uri()` and hands it to KlingRendererTool.

Verified 2026-04-23 via DashScope docs:
    - Submit  : POST https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis
    - Poll    : GET  https://dashscope-intl.aliyuncs.com/api/v1/tasks/{task_id}
    - Auth    : Bearer $DASHSCOPE_API_KEY  +  X-DashScope-Async: enable
    - Model   : qwen-image-plus  (async-capable; the 2.0/2.0-pro family is sync-only)
    - Sizes   : "1664*928" (16:9), "1472*1104" (4:3), "1328*1328" (1:1),
                "1104*1472" (3:4), "928*1664" (9:16).  No native 720p —
                caller resizes after download if Kling needs 1280x720.
    - Result  : output.results[0].url — OSS signed URL, 24h validity.

Shares the async submit/poll/download skeleton with WanRendererTool; intentional
because DashScope's task pattern is identical. The two could share a helper
some day; for now the duplication keeps each adapter file self-contained.

Content policy: refusals surface as task_status=FAILED with code
"DataInspectionFailed" or "InvalidParameter". Not retryable — a new prompt
is the only path forward. Adapter maps to failure_stage="content_policy".

Quota: DashScope's image quota is a DIFFERENT meter from Wan's video quota.
A new `budget.dashscope_image_quota_remaining` counter tracks it (not
coalesced into `alibaba_quota_remaining`).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping


QWEN_SUBMIT_URL = (
    "https://dashscope-intl.aliyuncs.com"
    "/api/v1/services/aigc/text2image/image-synthesis"
)
QWEN_POLL_URL_FMT = "https://dashscope-intl.aliyuncs.com/api/v1/tasks/{task_id}"

DEFAULT_MODEL = "qwen-image-plus"
DEFAULT_POLL_INTERVAL_S = 2
DEFAULT_POLL_TIMEOUT_S = 120       # plus-family images: 5-15s typical
DEFAULT_HTTP_TIMEOUT_S = 60

# Aspect ratio -> DashScope size string. No 1280x720 is natively supported;
# 1664*928 is the closest 16:9 option (16:9 ratio, 1.79 aspect).
ASPECT_TO_SIZE = {
    "16:9": "1664*928",
    "4:3":  "1472*1104",
    "1:1":  "1328*1328",
    "3:4":  "1104*1472",
    "9:16": "928*1664",
}
DEFAULT_ASPECT = "16:9"

# Content-policy error codes surfaced by DashScope. Mapping to failure_stage
# "content_policy" tells the caller "don't retry with the same prompt".
CONTENT_POLICY_CODES = frozenset({
    "DataInspectionFailed",
    "InvalidParameter.Prompt",        # sometimes used for filter hits
    "InvalidParameter.NegativePrompt",
})


class QwenImageTool:
    """DashScope Qwen-Image text-to-image adapter.

    Tool Protocol: `name == "image_generator"`, `__call__(shot_id, payload) -> dict`.

    Payload fields:
        prompt             : primary prompt text
        negative_prompt    : optional; DashScope supports it
        aspect_ratio       : "16:9" | "4:3" | "1:1" | "3:4" | "9:16"  (default 16:9)
        seed               : optional int for determinism
        output_dir         : directory to save artifacts/refs/
        attempt_id         : 1-indexed int, appended to the output filename
        model              : override DEFAULT_MODEL (rare)

    Returns dict (Tool Protocol):
        status             : "ok" | "failed"
        provider           : "dashscope_qwen_image"
        model              : the model id actually called
        task_id            : DashScope task id
        image_path         : relative path to the saved PNG
        image_md5          : hex md5 of the file
        output_size_bytes  : file size
        cost_usd           : 0.0 (free tier) — caller bumps the image quota
                             counter, not spent_usd
        quota_cost         : 1 (for dashscope_image_quota_remaining)
        latency_s
        size               : the DashScope size string actually requested
        stdout_tail / stderr_tail
    """

    name = "image_generator"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        submit_url: str = QWEN_SUBMIT_URL,
        poll_url_fmt: str = QWEN_POLL_URL_FMT,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        sleep: Any = time.sleep,
        urlopen: Any = None,
    ) -> None:
        self._api_key = api_key
        self._submit_url = submit_url
        self._poll_url_fmt = poll_url_fmt
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
            raise ValueError("QwenImageTool requires a shot_id")

        api_key = self._api_key or _resolve_dashscope_key()
        if not api_key:
            raise RuntimeError(
                "DASHSCOPE_API_KEY missing from env and .env; cannot submit Qwen-Image job"
            )

        model = payload.get("model") or DEFAULT_MODEL
        prompt = payload["prompt"]
        output_dir = Path(payload["output_dir"])
        attempt_id = int(payload["attempt_id"])
        negative_prompt = payload.get("negative_prompt") or ""
        aspect_ratio = payload.get("aspect_ratio", DEFAULT_ASPECT)
        seed = payload.get("seed")

        size = ASPECT_TO_SIZE.get(aspect_ratio)
        if size is None:
            raise ValueError(
                f"aspect_ratio {aspect_ratio!r} not supported by Qwen-Image; "
                f"use one of {sorted(ASPECT_TO_SIZE)}"
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{shot_id}_v{attempt_id}.png"

        started = time.time()
        poll_log: list[dict[str, Any]] = []

        body: dict[str, Any] = {
            "model": model,
            "input": {"prompt": prompt},
            "parameters": {
                "size": size,
                "n": 1,
                "prompt_extend": False,  # keep deterministic; don't let Qwen rewrite
                "watermark": False,      # Kling would animate a watermark otherwise
            },
        }
        if negative_prompt:
            body["input"]["negative_prompt"] = negative_prompt
        if seed is not None:
            body["parameters"]["seed"] = int(seed)

        # 1) submit
        try:
            submit_resp = self._post_json(
                self._submit_url,
                api_key,
                body,
                extra_headers={"X-DashScope-Async": "enable"},
            )
        except urllib.error.HTTPError as exc:
            body_text = _error_body(exc)
            stage = _submit_failure_stage(exc.code, body_text)
            return _failure(
                stage=stage,
                started=started,
                poll_log=poll_log,
                stderr=body_text,
                model=model,
                size=size,
            )

        task_id = submit_resp.get("output", {}).get("task_id")
        if not task_id:
            return _failure(
                stage="submit:malformed",
                started=started,
                poll_log=poll_log,
                stderr=f"submit missing task_id: {submit_resp!r}"[:500],
                model=model,
                size=size,
            )

        # 2) poll
        poll_url = self._poll_url_fmt.format(task_id=task_id)
        deadline = started + self._poll_timeout_s
        image_url: str | None = None
        actual_prompt: str | None = None
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
                results = output.get("results") or []
                if results and isinstance(results, list):
                    first = results[0] or {}
                    image_url = first.get("url")
                    actual_prompt = first.get("actual_prompt")
                break
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                code = str(output.get("code", ""))
                stage = (
                    "content_policy" if code in CONTENT_POLICY_CODES
                    else f"poll:{status}"
                )
                return _failure(
                    stage=stage,
                    started=started,
                    poll_log=poll_log,
                    stderr=(
                        output.get("message")
                        or json.dumps({"code": code, "output": output})
                    )[:500],
                    model=model,
                    task_id=task_id,
                    size=size,
                )
            self._sleep(self._poll_interval_s)

        if image_url is None:
            return _failure(
                stage="poll:timeout" if last_status != "SUCCEEDED" else "result_malformed",
                started=started,
                poll_log=poll_log,
                stderr=(
                    f"task {task_id}: last_status={last_status}; "
                    f"results[0].url not found"
                ),
                model=model,
                task_id=task_id,
                size=size,
            )

        # 3) download — OSS signed URL valid 24h; fetch immediately
        try:
            self._download(image_url, output_path)
        except urllib.error.HTTPError as exc:
            return _failure(
                stage="download",
                started=started,
                poll_log=poll_log,
                stderr=_error_body(exc),
                model=model,
                task_id=task_id,
                size=size,
            )

        size_bytes = output_path.stat().st_size
        if size_bytes == 0:
            return _failure(
                stage="download:empty",
                started=started,
                poll_log=poll_log,
                stderr=f"downloaded file is 0 bytes: {output_path}",
                model=model,
                task_id=task_id,
                size=size,
            )

        md5 = _md5_file(output_path)
        latency = round(time.time() - started, 3)

        result: dict[str, Any] = {
            "status": "ok",
            "provider": "dashscope_qwen_image",
            "model": model,
            "task_id": task_id,
            "image_path": str(output_path),
            "image_md5": md5,
            "output_size_bytes": size_bytes,
            "cost_usd": 0.0,          # free-tier quota meter; caller bumps quota counter
            "quota_cost": 1,
            "latency_s": latency,
            "size": size,
            "stdout_tail": json.dumps(poll_log)[-500:],
            "stderr_tail": "",
        }
        if actual_prompt and actual_prompt != prompt:
            # qwen's prompt-extend mode would rewrite — we turned it off so this
            # is unusual, but surface it if it ever happens.
            result["actual_prompt"] = actual_prompt
        return result

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
# Pure helpers
# ---------------------------------------------------------------------------


def _resolve_dashscope_key() -> str | None:
    """Resolve DASHSCOPE_API_KEY from shell env, then project .env."""
    v = os.environ.get("DASHSCOPE_API_KEY")
    if v:
        return v
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        env_path = parent / ".env"
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, vraw = line.partition("=")
                if k.strip() == "DASHSCOPE_API_KEY":
                    vraw = vraw.strip().strip('"').strip("'")
                    if vraw and not vraw.startswith("<"):
                        return vraw
            return None
    return None


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return (exc.read().decode("utf-8", errors="replace"))[:500]
    except Exception:
        return f"{exc.code} {exc.reason}"


def _submit_failure_stage(http_code: int, body_text: str) -> str:
    if any(code in body_text for code in CONTENT_POLICY_CODES):
        return "content_policy"
    if http_code == 429:
        return "rate_limit"
    if http_code in (401, 403):
        return "auth"
    if http_code == 400:
        return "validation"
    return f"submit:http_{http_code}"


def _failure(
    *,
    stage: str,
    started: float,
    poll_log: list[dict[str, Any]],
    stderr: str,
    model: str,
    task_id: str | None = None,
    size: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "failed",
        "failure_stage": stage,
        "provider": "dashscope_qwen_image",
        "model": model,
        "image_path": "",
        "image_md5": None,
        "output_size_bytes": 0,
        "cost_usd": 0.0,
        "quota_cost": 0,
        "latency_s": round(time.time() - started, 3),
        "size": size,
        "stdout_tail": json.dumps(poll_log)[-500:],
        "stderr_tail": stderr[-500:] if stderr else "",
    }
    if task_id:
        payload["task_id"] = task_id
    return payload
