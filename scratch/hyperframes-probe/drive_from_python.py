"""Simulate Editor-Agent-driven Hyperframes render via subprocess.

This is the shape the Producer's Editor tool adapter would take:
  - Write composition.html (agent-generated)
  - subprocess.run(['npx', 'hyperframes', 'render', '--output', ...])
  - Capture stdout, stderr, exit code, duration
  - Return as the Tool's result payload; dispatch() wraps with EventLog writes

We also run lint first (the bundled CLAUDE.md's "always lint after changes"
rule), treating it as a cheap validation gate before the expensive render.

This script deliberately uses nothing outside the Python stdlib — it's a
reference implementation for what the Producer adapter will do.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
COMPOSITION = HERE / "index.html"


def run_lint() -> dict:
    """Non-interactive machine-readable lint."""
    t0 = time.time()
    proc = subprocess.run(
        ["npx", "hyperframes", "lint", "--json"],
        cwd=str(HERE),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return {
        "stage": "lint",
        "exit_code": proc.returncode,
        "duration_s": round(time.time() - t0, 3),
        "stdout_tail": proc.stdout[-500:],
        "stderr_tail": proc.stderr[-500:],
    }


def run_render(output: Path) -> dict:
    """Render to MP4. Captures full stdout/stderr for the EventLog payload."""
    t0 = time.time()
    proc = subprocess.run(
        ["npx", "hyperframes", "render", "--output", str(output)],
        cwd=str(HERE),
        capture_output=True,
        text=True,
        timeout=300,
    )
    size_bytes = output.stat().st_size if output.exists() else 0
    md5 = None
    if size_bytes:
        h = hashlib.md5()
        with open(output, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        md5 = h.hexdigest()
    return {
        "stage": "render",
        "exit_code": proc.returncode,
        "duration_s": round(time.time() - t0, 3),
        "output_path": str(output),
        "output_size_bytes": size_bytes,
        "output_md5": md5,
        "stdout_tail": proc.stdout[-500:],
        "stderr_tail": proc.stderr[-500:],
    }


def main() -> int:
    if not COMPOSITION.exists():
        print(json.dumps({"error": f"composition not found: {COMPOSITION}"}))
        return 2

    events: list[dict] = []

    lint = run_lint()
    events.append(lint)
    if lint["exit_code"] != 0:
        print(json.dumps({"failed_at": "lint", "events": events}, indent=2))
        return 1

    render = run_render(HERE / "render_from_python.mp4")
    events.append(render)

    success = lint["exit_code"] == 0 and render["exit_code"] == 0
    print(json.dumps({"success": success, "events": events}, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
