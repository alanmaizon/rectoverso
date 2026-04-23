"""NormalizeTool — mechanical pre-pass that homogenizes provider MP4 outputs
into a single codec/resolution/fps/profile target before Hyperframes ingests
them.

Why this exists: our Wan / Kling / Seedance adapters produce MP4s with
heterogeneous codec shapes (verified 2026-04-23 via ffprobe on live renders):

    Wan 2.7      : h264 High  1280×720  24 or 30 fps  (AAC or silent)
    Kling 2.1 Pro: h264 Main  1928×1072 24 fps        (silent)
    Seedance 2.0 : h264 High  1280×720  24 fps        (silent)

Hyperframes invokes ffmpeg internally for composition assembly. Feeding it
heterogeneous inputs produces either copy-mode concat with broken timestamps
(`non monotonically increasing dts`) or silent re-encoding with unpredictable
results. Normalizing to a single target spec up-front removes the variable
from Hyperframes' path entirely.

The Editor Agent's bash tool invokes this normalizer once per shot before
authoring the Hyperframes `index.html` composition. Hyperframes sees only
normalized clips; the HTML-driven assembly architecture is preserved.

Tier-4 mechanical worker:
    - Pure subprocess wrapper around ffmpeg + ffprobe.
    - Bit-identical output across runs (libx264 `-threads 1` lock).
    - No LLM, no state, no network. One call = one normalized MP4 on disk.
    - Tool Protocol compliant for consistency with the rest of the adapters,
      even though no contracts fire on it (it's a sub-step of the Editor).

Target-spec defaults match the smoke-test findings: 1280×720 @ 24 fps,
h264 High L4.0, yuv420p, CRF 18, letterbox for non-matching aspect ratios.
Overridable via payload for 1080p finals or crop-instead-of-letterbox.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ._common import md5_file


# ---------------------------------------------------------------------------
# Target spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetSpec:
    """The normalized output spec. Every normalized clip has these properties
    exactly — that's the invariant that lets concat demuxer (or Hyperframes'
    internal ffmpeg) copy-mode assemble without timestamp corruption.

    Fields with rationale:
        width/height : 720p default matches Wan + Seedance; Kling downscales
                       from 1928×1072 via letterbox (see `scaling`). 1080p
                       is an operator override for finals.
        fps          : 24 — cinematic standard. Wan's 30fps renders drop
                       frames; everything else is 24 native.
        profile      : "high" — better compression at CRF 18 than Main.
                       Kling's Main-profile outputs re-encode without issue.
        level        : "4.0" — covers 1080p@30fps headroom; 720p needs 3.1
                       but 4.0 is harmless headroom.
        pix_fmt      : "yuv420p" — universal decoder compatibility.
        crf          : 18 — visually lossless for typical content; bumps
                       file size vs. 23 (default) but the cost at submission
                       scale (≤60s final) is negligible.
        preset       : "medium" — balanced encode time vs. compression.
                       "slow" for finals is a ~30% quality bump at 3x the
                       encode time; defer to operator override.
        scaling      : "letterbox" — pad to target aspect with black bars.
                       "crop" is the alternative for full-bleed aesthetic.
        threads      : 1 — libx264 is nondeterministic with multi-thread
                       encoding (macroblock assignment races). threads=1 is
                       bit-identical across runs. Slower but hashable, which
                       matters for regression tests on the final film.
    """

    width: int = 1280
    height: int = 720
    fps: int = 24
    profile: str = "high"
    level: str = "4.0"
    pix_fmt: str = "yuv420p"
    crf: int = 18
    preset: str = "medium"
    scaling: str = "letterbox"    # "letterbox" | "crop"
    threads: int = 1              # 1 = deterministic; bump for speed, lose determinism

    def validate(self) -> None:
        if self.scaling not in ("letterbox", "crop"):
            raise ValueError(
                f"scaling must be 'letterbox' or 'crop', got {self.scaling!r}"
            )
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width/height must be positive")
        if self.fps <= 0:
            raise ValueError("fps must be positive")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_HTTP_TIMEOUT_S = 300       # ffmpeg CPU-bound; normalize ~1s per render second
NORMALIZE_TIMEOUT_S = 120          # per-clip wall-clock cap

# Stage names the adapter surfaces in `failure_stage` on non-OK results.
# Kept enumerable so orchestrator/summary can filter by category.
FAILURE_STAGES = frozenset({
    "src_missing", "src_unreadable", "ffmpeg_missing",
    "ffprobe_failed", "encode_failed", "encode_timeout",
    "empty_output", "output_malformed",
})


class NormalizeTool:
    """Mechanical pre-pass: one source MP4 in, one normalized MP4 out.

    Tool Protocol: `name == "normalizer"`, `__call__(shot_id, payload) -> dict`.
    No contracts fire on this tool — it's a sub-step invoked by the Editor
    Agent's bash tool between the render adapter's output and the Hyperframes
    composition's input.

    Payload fields:
        src_path    : Path | str — input MP4 from the render adapter.
        output_dir  : Path       — directory to write the normalized MP4 into.
        attempt_id  : int        — suffix on output filename (1-indexed).
        target      : dict       — optional overrides of TargetSpec defaults.

    Output filename pattern: `{shot_id}_norm_v{attempt_id}.mp4`.

    Result dict (Tool Protocol shape):
        status              : "ok" | "failed"
        provider            : "ffmpeg_normalize"
        mode                : "normalize"
        shot_id             : echoed
        src_path            : echoed (absolute)
        src_md5             : md5 of the input MP4 (for provenance)
        output_path         : relative path to the normalized MP4
        output_md5          : md5 of the normalized MP4
        output_size_bytes
        target_spec         : dict — the spec actually applied (post-override merge)
        duration_s          : probed duration of the normalized MP4
        ffmpeg_command      : list[str] — exact argv used, for audit + replay
        latency_s           : wall-clock time
        cost_usd            : 0.0 (local CPU work; no API billing)
        quota_cost          : 0
        stdout_tail, stderr_tail : strings truncated to 500 chars on failure
    """

    name = "normalizer"

    def __init__(
        self,
        *,
        ffmpeg_bin: str | None = None,
        ffprobe_bin: str | None = None,
        timeout_s: float = NORMALIZE_TIMEOUT_S,
        # Injected for hermetic tests — default uses subprocess.run directly.
        run: Any = None,
    ) -> None:
        self._ffmpeg = ffmpeg_bin or shutil.which("ffmpeg")
        self._ffprobe = ffprobe_bin or shutil.which("ffprobe")
        self._timeout_s = timeout_s
        self._run = run or subprocess.run

    # -- Tool Protocol -----------------------------------------------------

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if shot_id is None:
            raise ValueError("NormalizeTool requires a shot_id")
        if not self._ffmpeg:
            return _failure(
                shot_id=shot_id,
                stage="ffmpeg_missing",
                stderr="ffmpeg not on PATH; set ffmpeg_bin= in constructor",
                started=time.time(),
            )

        src = Path(payload["src_path"])
        output_dir = Path(payload["output_dir"])
        attempt_id = int(payload["attempt_id"])

        # Merge operator overrides into the default spec.
        target_overrides = dict(payload.get("target") or {})
        target = TargetSpec(
            **{
                **TargetSpec().__dict__,
                **target_overrides,
            }
        )
        target.validate()

        started = time.time()

        if not src.exists():
            return _failure(
                shot_id=shot_id,
                stage="src_missing",
                stderr=f"source MP4 not found at {src}",
                started=started,
                src_path=str(src),
            )

        try:
            src_md5 = md5_file(src)
        except OSError as exc:
            return _failure(
                shot_id=shot_id,
                stage="src_unreadable",
                stderr=f"could not read source: {exc!r}",
                started=started,
                src_path=str(src),
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{shot_id}_norm_v{attempt_id}.mp4"

        cmd = _build_ffmpeg_cmd(
            ffmpeg=self._ffmpeg,
            src=src,
            dst=output_path,
            target=target,
        )

        try:
            proc = self._run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _failure(
                shot_id=shot_id,
                stage="encode_timeout",
                stderr=f"ffmpeg did not finish within {self._timeout_s}s",
                started=started,
                src_path=str(src),
                src_md5=src_md5,
                ffmpeg_command=list(cmd),
                target_spec=target.__dict__,
            )

        if proc.returncode != 0:
            return _failure(
                shot_id=shot_id,
                stage="encode_failed",
                stderr=(proc.stderr or "")[-500:],
                started=started,
                src_path=str(src),
                src_md5=src_md5,
                ffmpeg_command=list(cmd),
                target_spec=target.__dict__,
            )

        if not output_path.exists() or output_path.stat().st_size == 0:
            return _failure(
                shot_id=shot_id,
                stage="empty_output",
                stderr=f"ffmpeg succeeded but produced no output at {output_path}",
                started=started,
                src_path=str(src),
                src_md5=src_md5,
                ffmpeg_command=list(cmd),
                target_spec=target.__dict__,
            )

        # Probe the normalized output to sanity-check the spec took + surface
        # duration. Failure here doesn't kill the call — ffmpeg already
        # succeeded; we just can't fill in duration_s.
        duration_s = _probe_duration(self._ffprobe, output_path) if self._ffprobe else 0.0

        output_md5 = md5_file(output_path)
        latency = round(time.time() - started, 3)

        return {
            "status": "ok",
            "provider": "ffmpeg_normalize",
            "mode": "normalize",
            "shot_id": shot_id,
            "src_path": str(src),
            "src_md5": src_md5,
            "output_path": str(output_path),
            "output_md5": output_md5,
            "output_size_bytes": output_path.stat().st_size,
            "target_spec": target.__dict__,
            "duration_s": duration_s,
            "ffmpeg_command": list(cmd),
            "cost_usd": 0.0,
            "quota_cost": 0,
            "latency_s": latency,
            "stdout_tail": (proc.stdout or "")[-500:],
            "stderr_tail": (proc.stderr or "")[-500:],
        }


# ---------------------------------------------------------------------------
# ffmpeg command construction (pure)
# ---------------------------------------------------------------------------


def _build_ffmpeg_cmd(
    *,
    ffmpeg: str,
    src: Path,
    dst: Path,
    target: TargetSpec,
) -> list[str]:
    """Construct the ffmpeg argv for one normalize call.

    The video-filter graph handles two separate aspect-ratio cases:

    - `letterbox`: scale preserving aspect with `force_original_aspect_ratio=
      decrease` (never upscales past the smaller dim), then `pad` to the
      target with black bars. Preserves composition.
    - `crop`: scale with `force_original_aspect_ratio=increase` (fills both
      dims, may exceed target), then `crop` to center. Full-bleed; clips
      the edges of Kling's wider renders.

    Framerate is forced via the `fps` filter (drops or duplicates frames as
    needed — drops are the common case since we target 24 and Wan sometimes
    renders 30).

    Audio is stripped (`-an`) at this stage. The Editor Agent's composition
    step mixes audio separately from the normalized video track.
    """
    if target.scaling == "letterbox":
        vf = (
            f"scale={target.width}:{target.height}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={target.width}:{target.height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={target.fps}"
        )
    else:  # crop
        vf = (
            f"scale={target.width}:{target.height}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={target.width}:{target.height},"
            f"fps={target.fps}"
        )

    return [
        ffmpeg,
        "-v", "error",
        "-y",                              # overwrite dst
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264",
        "-profile:v", target.profile,
        "-level", target.level,
        "-pix_fmt", target.pix_fmt,
        "-preset", target.preset,
        "-crf", str(target.crf),
        "-threads", str(target.threads),   # 1 for deterministic output
        "-an",                             # no audio on normalize pass
        str(dst),
    ]


def _probe_duration(ffprobe: str, path: Path) -> float:
    """ffprobe a single file for duration. 0.0 on any failure — non-fatal."""
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return round(float(result.stdout.strip()), 3)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


# ---------------------------------------------------------------------------
# Failure helper
# ---------------------------------------------------------------------------


def _failure(
    *,
    shot_id: str,
    stage: str,
    stderr: str,
    started: float,
    src_path: str = "",
    src_md5: str | None = None,
    ffmpeg_command: list[str] | None = None,
    target_spec: dict | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "failed",
        "failure_stage": stage,
        "provider": "ffmpeg_normalize",
        "mode": "normalize",
        "shot_id": shot_id,
        "src_path": src_path,
        "src_md5": src_md5,
        "output_path": "",
        "output_md5": None,
        "output_size_bytes": 0,
        "cost_usd": 0.0,
        "quota_cost": 0,
        "latency_s": round(time.time() - started, 3),
        "stdout_tail": "",
        "stderr_tail": stderr[-500:] if stderr else "",
    }
    if ffmpeg_command is not None:
        payload["ffmpeg_command"] = ffmpeg_command
    if target_spec is not None:
        payload["target_spec"] = target_spec
    return payload
