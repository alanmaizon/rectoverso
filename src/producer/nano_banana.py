"""NanoBananaImageTool — Google Gemini image-generation adapter.

Day-4 fallback for Qwen-Image when Qwen's content filter refuses a shot.
Same Tool Protocol shape as QwenImageTool so `generate-ref` can swap them
behind a provider flag without the rest of the pipeline knowing.

Uses the **Gemini Developer API** (api-key header auth) rather than Vertex AI
ADC — we have a GEMINI_KEY in .env and no need to pull in google-auth for
this path. Pricing is per-image and separate from the Veo $15 cap (which
would apply if we went through Vertex).

Verified 2026-04-23 against the Gemini image-generation docs:
    - Endpoint: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
    - Auth: header `x-goog-api-key: $GEMINI_KEY`
    - Model: gemini-2.5-flash-image (Nano Banana, ~$0.039/image)
             or gemini-3-pro-image-preview (Nano Banana Pro, ~$0.134/image)
    - Body: Gemini contents/parts format with `responseModalities: ["IMAGE"]`
    - Response: candidates[0].content.parts[*].inlineData.{mimeType,data(base64)}
    - Sync — no polling, image arrives inline in the response (~3-8s typical)

Failure classes:
    - HTTP 400/401/403/429 → submit stage
    - candidates[].finishReason == "SAFETY" / "PROHIBITED_CONTENT" → content_policy
    - promptFeedback.blockReason present → content_policy
    - No inline image in response → result_malformed
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping


DEFAULT_MODEL = "gemini-2.5-flash-image"
GEMINI_ENDPOINT_FMT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
DEFAULT_HTTP_TIMEOUT_S = 120       # includes generation + response time (sync)

# Gemini image gen returns aspect through prompt hints primarily. Only a subset
# of aspect ratios ships as an explicit config parameter; we normalize them and
# always embed the hint in the prompt text too (belt + braces).
SUPPORTED_ASPECTS = ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9")
DEFAULT_ASPECT = "16:9"

# Candidate finish reasons that mean the model refused or was blocked.
BLOCKED_FINISH_REASONS = frozenset({
    "SAFETY",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "RECITATION",
})


class NanoBananaImageTool:
    """Gemini-based text-to-image adapter ("Nano Banana" family).

    Tool Protocol: `name == "image_generator"`, `__call__(shot_id, payload) -> dict`.
    Result dict shape matches QwenImageTool 1:1 so `generate-ref` can treat
    them interchangeably.

    Payload fields (all identical to QwenImageTool):
        prompt             : primary prompt text
        negative_prompt    : folded into the prompt as "Avoid: ..." — Gemini
                             image gen has no native negative_prompt field
        aspect_ratio       : "16:9" | "4:3" | "1:1" | "3:4" | "9:16" | "21:9"
        seed               : int — NOT USED (Gemini image gen has no seed
                             parameter in Apr 2026); accepted for interface
                             parity but ignored
        output_dir         : save directory
        attempt_id         : 1-indexed int for the output filename
        model              : override DEFAULT_MODEL
    """

    name = "image_generator"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint_fmt: str = GEMINI_ENDPOINT_FMT,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        urlopen: Any = None,
    ) -> None:
        self._api_key = api_key
        self._endpoint_fmt = endpoint_fmt
        self._http_timeout_s = http_timeout_s
        self._urlopen = urlopen or urllib.request.urlopen

    # -- Tool Protocol -----------------------------------------------------

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if shot_id is None:
            raise ValueError("NanoBananaImageTool requires a shot_id")

        api_key = self._api_key or _resolve_gemini_key()
        if not api_key:
            raise RuntimeError(
                "GEMINI_KEY missing from env and .env; cannot call Gemini image API"
            )

        model = payload.get("model") or DEFAULT_MODEL
        prompt = payload["prompt"]
        output_dir = Path(payload["output_dir"])
        attempt_id = int(payload["attempt_id"])
        negative_prompt = payload.get("negative_prompt") or ""
        aspect_ratio = payload.get("aspect_ratio", DEFAULT_ASPECT)

        if aspect_ratio not in SUPPORTED_ASPECTS:
            raise ValueError(
                f"aspect_ratio {aspect_ratio!r} not supported by Gemini image gen; "
                f"use one of {list(SUPPORTED_ASPECTS)}"
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{shot_id}_v{attempt_id}.png"

        started = time.time()

        # Gemini image gen has no native negative_prompt; fold it into the
        # prompt as an explicit constraint. Also surface the aspect hint in
        # the prompt text so the model leans toward the requested ratio even
        # if the imageConfig field is absent on an older model variant.
        composed_prompt = _compose_prompt(prompt, negative_prompt, aspect_ratio)

        body: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": composed_prompt}],
                }
            ],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": aspect_ratio},
            },
        }

        url = self._endpoint_fmt.format(model=model)

        try:
            resp = self._post_json(url, api_key, body)
        except urllib.error.HTTPError as exc:
            body_text = _error_body(exc)
            stage = _submit_failure_stage(exc.code, body_text)
            return _failure(
                stage=stage,
                started=started,
                stderr=body_text,
                model=model,
                aspect_ratio=aspect_ratio,
            )

        # Pre-generation safety block
        block = (resp.get("promptFeedback") or {}).get("blockReason")
        if block:
            return _failure(
                stage="content_policy",
                started=started,
                stderr=f"promptFeedback.blockReason={block}",
                model=model,
                aspect_ratio=aspect_ratio,
            )

        candidates = resp.get("candidates") or []
        if not candidates:
            return _failure(
                stage="result_malformed",
                started=started,
                stderr=f"no candidates in response: {json.dumps(resp)[:500]}",
                model=model,
                aspect_ratio=aspect_ratio,
            )

        # Post-generation safety block
        finish_reason = str(candidates[0].get("finishReason") or "")
        if finish_reason in BLOCKED_FINISH_REASONS:
            return _failure(
                stage="content_policy",
                started=started,
                stderr=f"finishReason={finish_reason}",
                model=model,
                aspect_ratio=aspect_ratio,
            )

        # Pull the first inline image part out.
        parts = (candidates[0].get("content") or {}).get("parts") or []
        image_b64 = None
        mime_type = "image/png"
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")  # camelCase + snake
            if inline and isinstance(inline, dict) and inline.get("data"):
                image_b64 = inline["data"]
                mime_type = inline.get("mimeType") or inline.get("mime_type") or mime_type
                break

        if not image_b64:
            return _failure(
                stage="result_malformed",
                started=started,
                stderr=(
                    f"candidate has no inline image data "
                    f"(finishReason={finish_reason!r}, parts={len(parts)})"
                ),
                model=model,
                aspect_ratio=aspect_ratio,
            )

        try:
            output_path.write_bytes(base64.b64decode(image_b64))
        except (ValueError, TypeError) as exc:
            return _failure(
                stage="result_malformed",
                started=started,
                stderr=f"base64 decode failed: {exc!r}",
                model=model,
                aspect_ratio=aspect_ratio,
            )

        size_bytes = output_path.stat().st_size
        if size_bytes == 0:
            return _failure(
                stage="download:empty",
                started=started,
                stderr=f"decoded file is 0 bytes: {output_path}",
                model=model,
                aspect_ratio=aspect_ratio,
            )

        md5 = _md5_file(output_path)
        latency = round(time.time() - started, 3)

        # Gemini Developer API returns usage metadata; surface it for the caller.
        usage = resp.get("usageMetadata") or {}
        cost = _cost_for(model)

        return {
            "status": "ok",
            "provider": "gemini_nano_banana",
            "model": model,
            "task_id": "",   # Gemini is sync; no async task id
            "image_path": str(output_path),
            "image_md5": md5,
            "output_size_bytes": size_bytes,
            "mime_type": mime_type,
            "cost_usd": cost,
            "quota_cost": 0,
            "latency_s": latency,
            "size": aspect_ratio,  # Gemini doesn't report exact pixel dims
            "usage": usage,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    # -- HTTP helper -------------------------------------------------------

    def _post_json(
        self, url: str, api_key: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        headers = {
            # Gemini Dev API: key goes in x-goog-api-key. `?key=` query param
            # also works but is more likely to leak into logs.
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with self._urlopen(req, timeout=self._http_timeout_s) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _compose_prompt(primary: str, negative: str, aspect_ratio: str) -> str:
    """Build the text prompt Gemini actually receives. Folds the negative
    prompt (Gemini has no native field) and an aspect hint into one string."""
    parts = [primary.strip()]
    if negative.strip():
        parts.append(f"Avoid: {negative.strip()}.")
    # Aspect hint as belt+braces. The imageConfig.aspectRatio field does
    # primary work but older model variants ignored it; the hint is cheap.
    parts.append(f"Aspect ratio: {aspect_ratio}.")
    return " ".join(parts)


def _cost_for(model: str) -> float:
    """Per-image pricing (Gemini Developer API, 2026-04-23).
    Costs are billed per successful generated image."""
    m = model.lower()
    if "3-pro-image" in m:
        return 0.134
    if "3.1-flash-image" in m:
        return 0.045
    # Default: gemini-2.5-flash-image (original Nano Banana)
    return 0.039


def _resolve_gemini_key() -> str | None:
    """Resolve GEMINI_KEY (our project convention) or GEMINI_API_KEY or
    GOOGLE_API_KEY from env, then project .env."""
    for name in ("GEMINI_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        v = os.environ.get(name)
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
                k = k.strip()
                if k in ("GEMINI_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
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
    lowered = body_text.lower()
    # Gemini often returns 400 with "prompt was blocked" or similar phrasing
    # for content-policy trips at the request level.
    if "blocked" in lowered or "safety" in lowered or "prohibited" in lowered:
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
    stderr: str,
    model: str,
    aspect_ratio: str,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "failure_stage": stage,
        "provider": "gemini_nano_banana",
        "model": model,
        "image_path": "",
        "image_md5": None,
        "output_size_bytes": 0,
        "cost_usd": 0.0,  # Gemini only bills on successful generation
        "quota_cost": 0,
        "latency_s": round(time.time() - started, 3),
        "size": aspect_ratio,
        "stdout_tail": "",
        "stderr_tail": stderr[-500:] if stderr else "",
    }
