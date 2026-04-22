"""Hyperframes CLI adapter — lint + render a composition, emit a DispatchResult.

This is the Tool-Protocol-compliant wrapper around `npx hyperframes` that the
Editor Agent (Tier-2 Managed Agent) invokes through its bash tool. Locally,
the same adapter works for pipeline tests and DEMO_MODE=0 dev runs; inside a
Managed Agents session the bash tool runs identical commands.

Proved viable end-to-end in both environments by:
    scratch/hyperframes-probe/         (local Mac, deterministic MD5 match)
    scratch/managed_agents_hyperframes_probe.py  (Anthropic cloud sandbox)
See docs/contracts.md § Contract 1 (Audio -> Editor) for the preconditions the
Producer enforces around invoking this adapter.

Intent:
    - Project directory (Hyperframes `init` output) is the Editor's workspace.
    - `npx hyperframes lint --json` is the cheap preflight gate.
    - `npx hyperframes render --output ...` is the expensive, deterministic
      projection.
    - Result payload is shaped for `dispatch_result` EventLog capture.

Architecture:
    - Pure subprocess: no SDK, no network, no mutation of the manifest.
    - Returns a plain dict matching the Tool Protocol. Producer projects it
      into `shots[i].edit.*` fields.
    - Errors at any step surface as non-PASS outcomes with stdout/stderr tails
      in the payload; the caller decides whether to retry or escalate.

Edge cases:
    - Node/ffmpeg not on PATH -> LintFailure / RenderFailure with a clear message.
    - Composition lint errors -> returned as structured lint JSON; adapter
      refuses to render.
    - Render produces an empty file -> treated as failure (size sanity check).
    - Render timeout -> subprocess.TimeoutExpired surfaces as failure.

Hyperframes is the sole renderer — there is no fallback format. If the render
loop exhausts, the caller (Editor Agent) escalates to the Producer rather than
silently shipping an alternate artifact.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


# Default ceilings. Callers can override via ctx.
DEFAULT_LINT_TIMEOUT_S = 60
DEFAULT_RENDER_TIMEOUT_S = 600
CANONICAL_OUTPUT_NAME = "out.mp4"


@dataclass(frozen=True)
class HyperframesResult:
    """Typed mirror of the dict this adapter returns. Not exposed publicly —
    the Tool Protocol passes dicts — but useful for documenting the shape."""

    status: str          # "ok" | "lint_failed" | "render_failed"
    exit_code: int       # last subprocess exit code
    duration_s: float    # wall-clock time across lint+render
    output_path: str     # relative to project_dir
    output_size_bytes: int
    output_md5: str | None
    renderer: str        # "hyperframes"
    renderer_version: str | None  # from lint --json _meta.version
    lint: Mapping[str, Any]       # parsed JSON from `lint --json`
    stdout_tail: str
    stderr_tail: str


class HyperframesTool:
    """Tool-Protocol adapter. `name == "editor_agent"` — matches the contract
    registry entry. Constructed with a base `project_dir`; each dispatch reads
    `ctx["project_dir"]` if provided, else falls back to the constructor value.
    """

    name = "editor_agent"

    def __init__(
        self,
        default_project_dir: Path | str | None = None,
        npx_bin: str = "npx",
    ) -> None:
        self._default_dir = Path(default_project_dir) if default_project_dir else None
        self._npx = npx_bin

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        project_dir = self._resolve_project_dir(payload)
        output_name = str(payload.get("output_name") or CANONICAL_OUTPUT_NAME)
        lint_timeout = float(payload.get("lint_timeout_s") or DEFAULT_LINT_TIMEOUT_S)
        render_timeout = float(payload.get("render_timeout_s") or DEFAULT_RENDER_TIMEOUT_S)

        started = time.time()
        lint_result = _run_lint(self._npx, project_dir, lint_timeout)

        if not lint_result.get("ok", False):
            # Lint failed — refuse to render. Cheap failure, clear cause.
            return _build_result(
                status="lint_failed",
                exit_code=lint_result.get("_exit_code", 1),
                duration_s=time.time() - started,
                output_path="",
                output_size_bytes=0,
                output_md5=None,
                renderer_version=_renderer_version(lint_result),
                lint=lint_result,
                stdout_tail=lint_result.get("_stdout_tail", ""),
                stderr_tail=lint_result.get("_stderr_tail", ""),
            )

        render_proc = _run_render(
            self._npx, project_dir, output_name, render_timeout
        )
        output_path = project_dir / output_name
        size = output_path.stat().st_size if output_path.exists() else 0
        md5 = _md5_file(output_path) if size else None

        status = "ok" if render_proc.returncode == 0 and size > 0 else "render_failed"

        return _build_result(
            status=status,
            exit_code=render_proc.returncode,
            duration_s=time.time() - started,
            output_path=str(output_path.relative_to(project_dir))
            if status == "ok"
            else output_name,
            output_size_bytes=size,
            output_md5=md5,
            renderer_version=_renderer_version(lint_result),
            lint=lint_result,
            stdout_tail=_tail(render_proc.stdout),
            stderr_tail=_tail(render_proc.stderr),
        )

    # -- internals -----------------------------------------------------------

    def _resolve_project_dir(self, payload: Mapping[str, Any]) -> Path:
        raw = payload.get("project_dir")
        if raw:
            return Path(raw)
        if self._default_dir is not None:
            return self._default_dir
        raise ValueError(
            "HyperframesTool requires project_dir in payload or constructor"
        )


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _run_lint(npx: str, project_dir: Path, timeout_s: float) -> dict[str, Any]:
    """Run `npx hyperframes lint --json`. Returns the parsed JSON augmented
    with runner metadata under `_` keys."""
    proc = subprocess.run(
        [npx, "hyperframes", "lint", "--json"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    stdout = proc.stdout or ""
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        parsed = {"ok": False, "errorCount": 1, "_parse_error": True}
    parsed["_exit_code"] = proc.returncode
    parsed["_stdout_tail"] = _tail(stdout)
    parsed["_stderr_tail"] = _tail(proc.stderr or "")
    return parsed


def _run_render(
    npx: str, project_dir: Path, output_name: str, timeout_s: float
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [npx, "hyperframes", "render", "--output", output_name],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tail(text: str, n: int = 500) -> str:
    return (text or "")[-n:]


def _renderer_version(lint_result: Mapping[str, Any]) -> str | None:
    """Extract the hyperframes package version from lint --json's _meta block."""
    meta = lint_result.get("_meta") or {}
    v = meta.get("version")
    return str(v) if v else None


def _build_result(
    *,
    status: str,
    exit_code: int,
    duration_s: float,
    output_path: str,
    output_size_bytes: int,
    output_md5: str | None,
    renderer_version: str | None,
    lint: Mapping[str, Any],
    stdout_tail: str,
    stderr_tail: str,
) -> dict[str, Any]:
    """Serialize the HyperframesResult dataclass shape to a plain dict."""
    # Strip runner metadata from the surfaced lint payload — it's captured
    # separately in exit_code/stdout/stderr. Keep the substantive lint findings.
    clean_lint = {k: v for k, v in lint.items() if not k.startswith("_")}
    return {
        "status": status,
        "exit_code": exit_code,
        "duration_s": round(duration_s, 3),
        "output_path": output_path,
        "output_size_bytes": output_size_bytes,
        "output_md5": output_md5,
        "renderer": "hyperframes",
        "renderer_version": renderer_version,
        "lint": clean_lint,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }
