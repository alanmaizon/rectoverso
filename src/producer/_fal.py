"""Shared fal.ai queue-API helpers used by every fal-hosted renderer adapter.

fal.ai exposes a single queue API shape that all their hosted models use:

    POST   https://queue.fal.run/{model_id}                 → request_id + status_url + response_url
    GET    https://queue.fal.run/{...}/requests/{id}/status → status: IN_QUEUE|IN_PROGRESS|COMPLETED
    GET    https://queue.fal.run/{...}/requests/{id}        → result payload with {video: {url}}
    GET    <video.url>                                       → the actual MP4 bytes

Every fal adapter we write follows that exact three-step flow and only differs
in the submit body shape + cost calculation + model-specific failure mapping.
This module owns the pattern; adapters configure it with their body/cost/etc.

Auth: every fal model accepts `Authorization: Key <FAL_KEY>` (literal word
`Key`, NOT `Bearer` — historically easy to get wrong).

Two-key failover: fal accounts commonly hold a primary + backup key pair so
iteration doesn't stall on single-key 401/403/429. `submit_with_failover()`
encapsulates the one-shot retry against the backup.

Scope rule: code that's Kling-specific or Seedance-specific lives in those
adapters, not here. The helpers here only operate in the "fal queue +
authorization header + video.url" space that's stable across all fal models.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from ._common import http_error_body, resolve_env_key


FAL_QUEUE_BASE = "https://queue.fal.run"


def resolve_fal_keys(
    primary_override: str | None = None,
    backup_override: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve primary + backup FAL keys. Constructor args win over env/.env.

    Looks up multiple env var names — fal's canonical `FAL_KEY` plus this
    project's `FAL_KEY_PRIMARY`/`FAL_KEY_SECONDARY` convention — so both
    conventions work without renaming .env files."""
    primary = primary_override or resolve_env_key("FAL_KEY", "FAL_KEY_PRIMARY")
    backup = backup_override or resolve_env_key(
        "FAL_KEY_2", "FAL_KEY_BACKUP", "FAL_KEY_SECONDARY"
    )
    return primary, backup


def post_json(
    urlopen: Callable[..., Any],
    url: str,
    api_key: str,
    body: Mapping[str, Any],
    *,
    http_timeout_s: float,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Key {api_key}",  # literal 'Key', not Bearer
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urlopen(req, timeout=http_timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(
    urlopen: Callable[..., Any],
    url: str,
    api_key: str,
    *,
    http_timeout_s: float,
) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Key {api_key}"},
        method="GET",
    )
    with urlopen(req, timeout=http_timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(
    urlopen: Callable[..., Any],
    url: str,
    dest: Path,
    *,
    http_timeout_s: float,
) -> None:
    """Stream a fal video URL to disk. No auth header — fal videos are signed
    ephemeral URLs that don't want the API key."""
    with urlopen(url, timeout=http_timeout_s) as resp, open(dest, "wb") as fh:
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            fh.write(chunk)


def is_content_policy_violation(body_text: str) -> bool:
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


def submit_with_failover(
    urlopen: Callable[..., Any],
    url: str,
    body: Mapping[str, Any],
    primary_key: str,
    backup_key: str | None,
    *,
    http_timeout_s: float,
) -> tuple[dict[str, Any], str, dict[str, str] | None]:
    """Submit on primary; on 401/403/429 (if backup exists), swap and retry once.

    Returns (response_body, used_key, None) on success, or
    ({}, used_key, {stage, stderr}) on terminal failure. `stage` is already
    shaped as the `failure_stage` value adapters put into their result dict:
    `"content_policy"`, `"submit:http_<code>"`, or
    `"submit:http_<code>:backup_also_failed"`.
    """
    try:
        resp = post_json(urlopen, url, primary_key, body, http_timeout_s=http_timeout_s)
        return resp, primary_key, None
    except urllib.error.HTTPError as exc:
        body_text = http_error_body(exc)
        if is_content_policy_violation(body_text):
            return {}, primary_key, {
                "stage": "content_policy",
                "stderr": body_text,
            }
        retryable = exc.code in (401, 403, 429) and backup_key is not None
        if not retryable:
            return {}, primary_key, {
                "stage": f"submit:http_{exc.code}",
                "stderr": body_text,
            }
        # One-shot retry against the backup key.
        try:
            resp = post_json(
                urlopen, url, backup_key, body, http_timeout_s=http_timeout_s  # type: ignore[arg-type]
            )
            return resp, backup_key, None  # type: ignore[return-value]
        except urllib.error.HTTPError as exc2:
            body_text2 = http_error_body(exc2)
            if is_content_policy_violation(body_text2):
                return {}, backup_key or primary_key, {  # type: ignore[return-value]
                    "stage": "content_policy",
                    "stderr": body_text2,
                }
            return {}, backup_key or primary_key, {  # type: ignore[return-value]
                "stage": f"submit:http_{exc2.code}:backup_also_failed",
                "stderr": body_text2,
            }


def poll_until_completed(
    urlopen: Callable[..., Any],
    status_url: str,
    api_key: str,
    *,
    sleep: Callable[[float], None],
    poll_interval_s: float,
    started: float,
    poll_timeout_s: float,
    http_timeout_s: float,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """Poll fal's status_url until terminal state or timeout.

    Returns (final_status_resp, poll_log, error_stage). Exactly one of:
      - (status_resp, log, None)       — COMPLETED; caller fetches response_url
      - (status_resp, log, "poll:ERROR"|"poll:FAILED"|"poll:CANCELLED")
                                        — terminal non-success from fal
      - (None, log, "poll:timeout")    — timeout before any terminal state
    """
    poll_log: list[dict[str, Any]] = []
    deadline = started + poll_timeout_s
    last_status: str | None = None
    while time.time() < deadline:
        try:
            status_resp = get_json(
                urlopen, status_url, api_key, http_timeout_s=http_timeout_s
            )
        except urllib.error.HTTPError as exc:
            poll_log.append({"t": round(time.time() - started, 2), "http_error": exc.code})
            sleep(poll_interval_s)
            continue

        status = status_resp.get("status")
        last_status = status
        poll_log.append({"t": round(time.time() - started, 2), "status": status})
        if status == "COMPLETED":
            return status_resp, poll_log, None
        if status in ("ERROR", "FAILED", "CANCELLED"):
            return status_resp, poll_log, f"poll:{status}"
        sleep(poll_interval_s)
    _ = last_status  # kept for possible future diagnostic inclusion
    return None, poll_log, "poll:timeout"
