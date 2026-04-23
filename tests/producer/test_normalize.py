"""Hermetic tests for NormalizeTool.

Injects a fake subprocess runner so tests don't actually invoke ffmpeg.
The argv construction is the main contract; the ffmpeg-level behavior is
covered separately by the live smoke test (pre-flight before Editor wiring).

Coverage:
    - happy path: successful encode → result dict has expected keys
    - argv shape: letterbox + crop vf graphs, threads=1, -an
    - default target spec matches the smoke-test findings
    - operator overrides merge cleanly
    - failure stages: ffmpeg missing, src missing, encode nonzero exit,
                      encode timeout, zero-byte output
    - determinism: same call produces same argv (trivially — pure function)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.producer import NormalizeTool, TargetSpec
from src.producer.normalize import _build_ffmpeg_cmd


# ---------------------------------------------------------------------------
# Fake subprocess runner
# ---------------------------------------------------------------------------


class _FakeCompleted:
    """Mimics the subset of subprocess.CompletedProcess the tool reads."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_fake_run(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    write_output: bool = True,
    output_bytes: bytes = b"\x00" * 1024,
    raises: type[BaseException] | None = None,
):
    """Build a fake subprocess.run. Writes a non-empty file at the argv's
    last positional (the ffmpeg dst path) when write_output=True, to
    simulate a successful encode. raises: set to subprocess.TimeoutExpired
    to simulate a timeout."""
    calls: list[dict[str, Any]] = []

    def _run(cmd, capture_output=False, text=False, timeout=None, check=False):
        calls.append({"cmd": list(cmd), "timeout": timeout})
        if raises is not None:
            if raises is subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(cmd, timeout)
            raise raises("simulated")
        if write_output:
            # cmd's last entry is the dst path per _build_ffmpeg_cmd shape
            dst = Path(cmd[-1])
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(output_bytes)
        return _FakeCompleted(returncode, stdout, stderr)

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


# ---------------------------------------------------------------------------
# Argv construction (pure, no subprocess needed)
# ---------------------------------------------------------------------------


def test_default_target_spec_matches_smoke_test_findings() -> None:
    """The adapter's target defaults must match the post-smoke-test decision:
    1280×720 @ 24fps, h264 High L4.0, yuv420p, CRF 18, letterbox, threads=1.
    Changing any of these is a production decision, not a refactor — locked
    by this test so regressions surface immediately."""
    s = TargetSpec()
    assert s.width == 1280
    assert s.height == 720
    assert s.fps == 24
    assert s.profile == "high"
    assert s.level == "4.0"
    assert s.pix_fmt == "yuv420p"
    assert s.crf == 18
    assert s.preset == "medium"
    assert s.scaling == "letterbox"
    assert s.threads == 1  # determinism


def test_letterbox_vf_graph_shape() -> None:
    cmd = _build_ffmpeg_cmd(
        ffmpeg="ffmpeg",
        src=Path("/tmp/src.mp4"),
        dst=Path("/tmp/dst.mp4"),
        target=TargetSpec(),
    )
    # -vf arg is 2 positions after "-vf"
    vf_idx = cmd.index("-vf")
    vf = cmd[vf_idx + 1]
    # Letterbox: scale with decrease + pad to fill
    assert "force_original_aspect_ratio=decrease" in vf
    assert "pad=1280:720:(ow-iw)/2:(oh-ih)/2" in vf
    assert "fps=24" in vf
    # -an so we strip audio from the normalize pass
    assert "-an" in cmd
    # threads=1 for determinism
    assert cmd[cmd.index("-threads") + 1] == "1"


def test_crop_vf_graph_shape() -> None:
    cmd = _build_ffmpeg_cmd(
        ffmpeg="ffmpeg",
        src=Path("/tmp/src.mp4"),
        dst=Path("/tmp/dst.mp4"),
        target=TargetSpec(scaling="crop"),
    )
    vf = cmd[cmd.index("-vf") + 1]
    # Crop: scale with increase + crop to center
    assert "force_original_aspect_ratio=increase" in vf
    assert "crop=1280:720" in vf
    # No pad filter in crop path
    assert "pad=" not in vf


def test_target_spec_rejects_invalid_scaling() -> None:
    with pytest.raises(ValueError, match="scaling"):
        TargetSpec(scaling="zoom").validate()


def test_target_spec_rejects_nonpositive_dims() -> None:
    with pytest.raises(ValueError):
        TargetSpec(width=0).validate()
    with pytest.raises(ValueError):
        TargetSpec(height=-1).validate()
    with pytest.raises(ValueError):
        TargetSpec(fps=0).validate()


def test_1080p_override_merges_cleanly() -> None:
    """Operator overrides a single field — the rest of the default spec
    should stick."""
    cmd = _build_ffmpeg_cmd(
        ffmpeg="ffmpeg",
        src=Path("/tmp/x.mp4"),
        dst=Path("/tmp/y.mp4"),
        target=TargetSpec(width=1920, height=1080),
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1920:1080" in vf
    assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2" in vf
    # Rest of spec unchanged
    assert cmd[cmd.index("-profile:v") + 1] == "high"
    assert cmd[cmd.index("-crf") + 1] == "18"


# ---------------------------------------------------------------------------
# Happy path via fake subprocess
# ---------------------------------------------------------------------------


def test_happy_path_returns_expected_result_dict(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"fake source mp4 bytes - md5'd by the tool")
    fake = _make_fake_run(returncode=0, output_bytes=b"\x00" * 2048)

    tool = NormalizeTool(ffmpeg_bin="/fake/ffmpeg", ffprobe_bin=None, run=fake)
    result = tool(
        "sh_005",
        {
            "src_path": src,
            "output_dir": tmp_path / "normalized",
            "attempt_id": 1,
        },
    )

    assert result["status"] == "ok"
    assert result["provider"] == "ffmpeg_normalize"
    assert result["mode"] == "normalize"
    assert result["shot_id"] == "sh_005"
    assert result["src_path"] == str(src)
    assert len(result["src_md5"]) == 32   # md5 hex
    assert result["output_path"].endswith("sh_005_norm_v1.mp4")
    assert len(result["output_md5"]) == 32
    assert result["output_size_bytes"] == 2048
    # target_spec defaults reflected in the result
    assert result["target_spec"]["width"] == 1280
    assert result["target_spec"]["threads"] == 1
    # Command is captured for audit
    assert isinstance(result["ffmpeg_command"], list)
    assert "-threads" in result["ffmpeg_command"]
    assert "-c:v" in result["ffmpeg_command"]
    assert "libx264" in result["ffmpeg_command"]
    assert result["cost_usd"] == 0.0
    assert result["quota_cost"] == 0


def test_operator_target_override_applies_to_command(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    fake = _make_fake_run()
    tool = NormalizeTool(ffmpeg_bin="/fake/ffmpeg", ffprobe_bin=None, run=fake)
    tool(
        "sh_001",
        {
            "src_path": src,
            "output_dir": tmp_path / "out",
            "attempt_id": 1,
            "target": {"width": 1920, "height": 1080, "crf": 14, "scaling": "crop"},
        },
    )
    cmd = fake.calls[0]["cmd"]
    vf = cmd[cmd.index("-vf") + 1]
    # 1080p crop
    assert "scale=1920:1080" in vf
    assert "crop=1920:1080" in vf
    # CRF 14 (near-lossless override)
    assert cmd[cmd.index("-crf") + 1] == "14"


def test_threads_1_is_default_for_determinism(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    fake = _make_fake_run()
    tool = NormalizeTool(ffmpeg_bin="/fake/ffmpeg", ffprobe_bin=None, run=fake)
    tool("sh_001", {
        "src_path": src,
        "output_dir": tmp_path / "out",
        "attempt_id": 1,
    })
    cmd = fake.calls[0]["cmd"]
    assert cmd[cmd.index("-threads") + 1] == "1"


# ---------------------------------------------------------------------------
# Failure stages
# ---------------------------------------------------------------------------


def test_ffmpeg_missing_fails_loud(tmp_path: Path, monkeypatch) -> None:
    """No ffmpeg on PATH + no ffmpeg_bin override → failure stage reports it
    without calling anything."""
    import shutil as _shutil
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    # Simulate no ffmpeg anywhere on PATH so the constructor falls through
    # to a None/empty resolution.
    monkeypatch.setattr(_shutil, "which", lambda _name: None)
    tool = NormalizeTool(ffmpeg_bin="", ffprobe_bin=None, run=_make_fake_run())
    result = tool("sh_001", {
        "src_path": src,
        "output_dir": tmp_path / "out",
        "attempt_id": 1,
    })
    assert result["status"] == "failed"
    assert result["failure_stage"] == "ffmpeg_missing"


def test_src_missing_fails_without_running_ffmpeg(tmp_path: Path) -> None:
    fake = _make_fake_run()
    tool = NormalizeTool(ffmpeg_bin="/fake/ffmpeg", ffprobe_bin=None, run=fake)
    result = tool("sh_001", {
        "src_path": tmp_path / "does_not_exist.mp4",
        "output_dir": tmp_path / "out",
        "attempt_id": 1,
    })
    assert result["status"] == "failed"
    assert result["failure_stage"] == "src_missing"
    # ffmpeg must not have been invoked
    assert len(fake.calls) == 0


def test_encode_nonzero_exit_surfaces_stderr(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"bad mp4")
    fake = _make_fake_run(
        returncode=1,
        stderr="[libx264 @ 0x...] invalid level for profile high",
        write_output=False,
    )
    tool = NormalizeTool(ffmpeg_bin="/fake/ffmpeg", ffprobe_bin=None, run=fake)
    result = tool("sh_001", {
        "src_path": src,
        "output_dir": tmp_path / "out",
        "attempt_id": 1,
    })
    assert result["status"] == "failed"
    assert result["failure_stage"] == "encode_failed"
    assert "invalid level" in result["stderr_tail"]
    # Command retained for post-hoc replay
    assert "ffmpeg_command" in result


def test_encode_timeout_surfaces_stage(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    fake = _make_fake_run(raises=subprocess.TimeoutExpired)
    tool = NormalizeTool(
        ffmpeg_bin="/fake/ffmpeg", ffprobe_bin=None, run=fake, timeout_s=1.0
    )
    result = tool("sh_001", {
        "src_path": src,
        "output_dir": tmp_path / "out",
        "attempt_id": 1,
    })
    assert result["status"] == "failed"
    assert result["failure_stage"] == "encode_timeout"


def test_empty_output_fails_even_on_success_return(tmp_path: Path) -> None:
    """ffmpeg exit 0 but no output file = failure. Guards against the edge
    case where ffmpeg silently writes nothing (disk full, bad path, etc.)."""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    fake = _make_fake_run(returncode=0, write_output=False)
    tool = NormalizeTool(ffmpeg_bin="/fake/ffmpeg", ffprobe_bin=None, run=fake)
    result = tool("sh_001", {
        "src_path": src,
        "output_dir": tmp_path / "out",
        "attempt_id": 1,
    })
    assert result["status"] == "failed"
    assert result["failure_stage"] == "empty_output"


def test_zero_byte_output_fails(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    fake = _make_fake_run(returncode=0, output_bytes=b"")
    tool = NormalizeTool(ffmpeg_bin="/fake/ffmpeg", ffprobe_bin=None, run=fake)
    result = tool("sh_001", {
        "src_path": src,
        "output_dir": tmp_path / "out",
        "attempt_id": 1,
    })
    assert result["status"] == "failed"
    assert result["failure_stage"] == "empty_output"


def test_requires_shot_id() -> None:
    tool = NormalizeTool(ffmpeg_bin="/fake/ffmpeg", run=_make_fake_run())
    with pytest.raises(ValueError, match="shot_id"):
        tool(None, {"src_path": "x", "output_dir": "y", "attempt_id": 1})
