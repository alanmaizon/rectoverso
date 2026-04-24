"""Cross-adapter helpers shared by the Tier-4 adapters.

Every renderer / image-gen adapter needed the same three things in isolation:

    - env-var lookup that also walks up to the project's .env file
    - md5 hash of a file (for render provenance + determinism signatures)
    - HTTPError body extraction that never propagates a decode exception

Kept each adapter self-contained by copy-paste during the exploration phase.
Once five adapters had ~identical copies the duplication was more than the
abstraction cost, so this module extracts the pure helpers.

Scope rule: only helpers that take the SAME arguments everywhere belong here.
Per-adapter "_failure" dicts, "_submit_failure_stage" classifiers, and URL
builders stay in their home modules because their signatures differ.
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
from pathlib import Path


def resolve_env_key(
    *names: str, env_file_start: Path | None = None
) -> str | None:
    """Look up the first non-empty value among `names` in shell env, then
    in the project's `.env` file.

    `.env` discovery walks up the directory tree starting from
    `env_file_start` (defaults to this module's directory), checking each
    parent for a `.env` file and reading any of `names` it defines there.

    Returns None if no value is found. Values starting with `<` (e.g.
    `<your-key-here>` placeholders in committed `.env.example` files) are
    skipped so placeholder strings never leak into API calls.
    """
    # 1) Shell env — try each candidate name in order.
    for name in names:
        v = os.environ.get(name)
        if v:
            return v

    # 2) Walk up from the start path looking for a .env file.
    start = (env_file_start or Path(__file__)).resolve()
    wanted = set(names)
    for parent in (start.parent, *start.parents):
        env_path = parent / ".env"
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, vraw = line.partition("=")
                if k.strip() not in wanted:
                    continue
                vraw = vraw.strip().strip('"').strip("'")
                if vraw and not vraw.startswith("<"):
                    return vraw
            # Found a .env but nothing matched — stop walking. Upstream .env
            # files are unlikely and keeping going would surprise operators.
            return None
    return None


def md5_file(path: Path) -> str:
    """Streaming md5 of a file. Used to stamp `render_md5` on the manifest
    so we can assert bit-identical output across retries."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    """Streaming sha256 of a file. Used for artifact upload integrity checks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def http_error_body(exc: urllib.error.HTTPError) -> str:
    """Read the response body out of an HTTPError, handle decode failures
    gracefully, truncate to 500 chars. Downstream adapters surface this as
    `stderr_tail` in their failure payloads."""
    try:
        return (exc.read().decode("utf-8", errors="replace"))[:500]
    except Exception:
        return f"{exc.code} {exc.reason}"


# ---------------------------------------------------------------------------
# Opus 4.7 session cost — post-probe shape
# ---------------------------------------------------------------------------
#
# Managed Agents session.usage shape confirmed by probe (2026-04-23), see
# research/managed_agents_platform_api.md § Day 5 probe findings:
#
#   usage = {
#       "input_tokens": int,
#       "output_tokens": int,
#       "cache_read_input_tokens": int,
#       "cache_creation": {
#           "ephemeral_5m_input_tokens": int,
#           "ephemeral_1h_input_tokens": int,
#       },
#   }
#
# Asymmetric naming (`cache_creation` vs `cache_read_input_tokens`) is not
# a typo on our end; it's how the SDK returns it. The cache_creation dict
# breaks out by TTL so we can attribute which TTL tier the spend went to,
# though we collapse them here since pricing is the same.
#
# Public pricing for Opus 4.7 (per MTok):
#   input                               $15.00
#   output                              $75.00
#   cache creation (5m)                 $18.75 (1.25x input, 5-minute TTL)
#   cache creation (1h)                 $30.00 (2.00x input, 1-hour TTL)
#   cache read                           $1.50 (0.10x input)

_OPUS_47_PRICES_PER_MTOK = {
    "input": 15.00,
    "output": 75.00,
    "cache_creation_5m": 18.75,
    "cache_creation_1h": 30.00,
    "cache_read": 1.50,
}


def compute_opus_47_cost(usage: "dict | object | None") -> float:
    """Compute the USD cost of an Opus-4.7 Managed Agents session from the
    session's `usage` object.

    Accepts either:
        - a dict (model_dump shape), OR
        - an SDK object that we coerce via model_dump/dict/vars

    Unknown or missing fields count as zero — keeps the function defensive
    against SDK shape drift without raising. The caller (orchestrator's
    editor trigger) compares this number against `estimated_editor_cost_usd`
    for budget accounting; returning 0.0 on a degenerate shape is safer
    than propagating an error that would masks a real session cost.

    Returns rounded USD to 4 decimals.
    """
    d = _usage_to_dict(usage)
    if not d:
        return 0.0

    input_tokens = int(d.get("input_tokens") or 0)
    output_tokens = int(d.get("output_tokens") or 0)
    cache_read = int(d.get("cache_read_input_tokens") or 0)

    cache_creation = d.get("cache_creation") or {}
    if not isinstance(cache_creation, dict):
        cache_creation = _usage_to_dict(cache_creation)
    cache_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
    cache_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)

    cost = (
        (input_tokens * _OPUS_47_PRICES_PER_MTOK["input"] / 1_000_000)
        + (output_tokens * _OPUS_47_PRICES_PER_MTOK["output"] / 1_000_000)
        + (cache_5m * _OPUS_47_PRICES_PER_MTOK["cache_creation_5m"] / 1_000_000)
        + (cache_1h * _OPUS_47_PRICES_PER_MTOK["cache_creation_1h"] / 1_000_000)
        + (cache_read * _OPUS_47_PRICES_PER_MTOK["cache_read"] / 1_000_000)
    )
    return round(cost, 4)


def _usage_to_dict(usage: "dict | object | None") -> dict:
    """Coerce an Anthropic SDK usage object (Pydantic BaseModel, or dict,
    or arbitrary object) into a plain dict. Returns {} on None or failure.

    Tries in order:
        1. isinstance(usage, dict) — pass through
        2. usage.model_dump() — Pydantic v2
        3. usage.dict() — Pydantic v1
        4. usage.to_dict() — Anthropic SDK convention
        5. vars(usage) — plain object
        6. {} — give up
    """
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(usage, attr, None)
        if callable(fn):
            try:
                result = fn()
                if isinstance(result, dict):
                    return result
            except Exception:
                pass
    try:
        return {k: v for k, v in vars(usage).items() if not k.startswith("_")}
    except TypeError:
        return {}
