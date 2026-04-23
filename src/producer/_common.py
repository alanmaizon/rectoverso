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


def http_error_body(exc: urllib.error.HTTPError) -> str:
    """Read the response body out of an HTTPError, handle decode failures
    gracefully, truncate to 500 chars. Downstream adapters surface this as
    `stderr_tail` in their failure payloads."""
    try:
        return (exc.read().decode("utf-8", errors="replace"))[:500]
    except Exception:
        return f"{exc.code} {exc.reason}"
