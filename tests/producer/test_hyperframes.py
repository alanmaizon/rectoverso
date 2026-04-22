"""Unit tests for src.producer.hyperframes — the HyperframesTool adapter.

Intent:        dispatch_result payload shape is stable; lint gates render
Architecture:  subprocess surface is injectable (monkeypatch) so tests are
               hermetic — no npx, no Chrome, no sandbox
Edge cases:    lint failure, render non-zero exit, empty output, missing
               project_dir, subprocess timeout
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.producer.hyperframes import HyperframesTool


def _lint_ok_payload() -> str:
    return json.dumps(
        {
            "ok": True,
            "errorCount": 0,
            "warningCount": 0,
            "infoCount": 1,
            "findings": [],
            "filesScanned": 1,
            "_meta": {"version": "0.4.12", "latestVersion": "0.4.12"},
        }
    )


def _lint_bad_payload() -> str:
    return json.dumps(
        {
            "ok": False,
            "errorCount": 1,
            "warningCount": 0,
            "findings": [
                {"severity": "error", "message": "missing data-start", "file": "index.html"}
            ],
            "_meta": {"version": "0.4.12"},
        }
    )


class FakeProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_subprocess_run(
    *,
    lint: FakeProcess,
    render: FakeProcess,
    create_output: bool,
    output_bytes: bytes = b"fakemp4data",
):
    """Return a subprocess.run stand-in that routes lint vs render calls."""
    call_log: list[dict] = []

    def _run(cmd, **kwargs):
        call_log.append({"cmd": list(cmd), "kwargs": dict(kwargs)})
        if len(cmd) >= 3 and cmd[2] == "lint":
            return lint
        if len(cmd) >= 3 and cmd[2] == "render":
            if create_output:
                out_idx = cmd.index("--output") + 1
                output_path = Path(kwargs["cwd"]) / cmd[out_idx]
                output_path.write_bytes(output_bytes)
            return render
        raise AssertionError(f"unexpected cmd: {cmd}")

    _run.calls = call_log  # type: ignore[attr-defined]
    return _run


# -- happy path ------------------------------------------------------------


def test_happy_path_lints_then_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "probe"
    project.mkdir()
    (project / "index.html").write_text("<!doctype html><html></html>")

    fake = _fake_subprocess_run(
        lint=FakeProcess(returncode=0, stdout=_lint_ok_payload()),
        render=FakeProcess(returncode=0, stdout="Render complete"),
        create_output=True,
        output_bytes=b"\x00" * 2048,
    )
    monkeypatch.setattr(subprocess, "run", fake)

    tool = HyperframesTool(default_project_dir=project)
    result = tool(shot_id=None, payload={})

    assert tool.name == "editor_agent"
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["output_size_bytes"] == 2048
    assert result["output_md5"] is not None
    assert len(result["output_md5"]) == 32
    assert result["renderer"] == "hyperframes"
    assert result["renderer_version"] == "0.4.12"
    assert result["lint"]["ok"] is True
    assert "_stdout_tail" not in result["lint"]  # runner metadata stripped
    # Two subprocess calls — lint then render
    assert len(fake.calls) == 2
    assert fake.calls[0]["cmd"][2] == "lint"
    assert fake.calls[1]["cmd"][2] == "render"


def test_payload_override_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ctx['project_dir'] overrides the constructor default."""
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / "index.html").write_text("")

    fake = _fake_subprocess_run(
        lint=FakeProcess(returncode=0, stdout=_lint_ok_payload()),
        render=FakeProcess(returncode=0, stdout=""),
        create_output=True,
    )
    monkeypatch.setattr(subprocess, "run", fake)

    tool = HyperframesTool(default_project_dir=tmp_path / "ignored")
    result = tool(None, {"project_dir": explicit})
    assert result["status"] == "ok"
    assert fake.calls[0]["kwargs"]["cwd"] == str(explicit)


def test_custom_output_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "probe"
    project.mkdir()
    fake = _fake_subprocess_run(
        lint=FakeProcess(returncode=0, stdout=_lint_ok_payload()),
        render=FakeProcess(returncode=0, stdout=""),
        create_output=True,
        output_bytes=b"abc",
    )
    monkeypatch.setattr(subprocess, "run", fake)

    tool = HyperframesTool(default_project_dir=project)
    result = tool(None, {"output_name": "custom.mp4"})
    assert result["output_path"] == "custom.mp4"
    assert "--output" in fake.calls[1]["cmd"]
    out_idx = fake.calls[1]["cmd"].index("--output")
    assert fake.calls[1]["cmd"][out_idx + 1] == "custom.mp4"


# -- lint failure blocks render -------------------------------------------


def test_lint_failure_refuses_to_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "probe"
    project.mkdir()
    fake = _fake_subprocess_run(
        lint=FakeProcess(returncode=1, stdout=_lint_bad_payload()),
        render=FakeProcess(returncode=0, stdout=""),  # should never be called
        create_output=False,
    )
    monkeypatch.setattr(subprocess, "run", fake)

    tool = HyperframesTool(default_project_dir=project)
    result = tool(None, {})

    assert result["status"] == "lint_failed"
    assert result["exit_code"] == 1
    assert result["output_size_bytes"] == 0
    assert result["output_md5"] is None
    assert result["lint"]["errorCount"] == 1
    # Only one subprocess call — lint; render was skipped
    assert len(fake.calls) == 1
    assert fake.calls[0]["cmd"][2] == "lint"


def test_lint_unparseable_stdout_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "probe"
    project.mkdir()
    fake = _fake_subprocess_run(
        lint=FakeProcess(returncode=1, stdout="oops not json"),
        render=FakeProcess(returncode=0, stdout=""),
        create_output=False,
    )
    monkeypatch.setattr(subprocess, "run", fake)

    tool = HyperframesTool(default_project_dir=project)
    result = tool(None, {})
    assert result["status"] == "lint_failed"
    # Parse error marker not leaked to the outer payload, but lint.ok=False.
    assert result["lint"]["ok"] is False


# -- render failure paths --------------------------------------------------


def test_render_nonzero_exit_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "probe"
    project.mkdir()
    fake = _fake_subprocess_run(
        lint=FakeProcess(returncode=0, stdout=_lint_ok_payload()),
        render=FakeProcess(returncode=2, stdout="", stderr="something broke"),
        create_output=False,
    )
    monkeypatch.setattr(subprocess, "run", fake)

    tool = HyperframesTool(default_project_dir=project)
    result = tool(None, {})
    assert result["status"] == "render_failed"
    assert result["exit_code"] == 2
    assert "something broke" in result["stderr_tail"]
    assert result["output_size_bytes"] == 0


def test_render_empty_output_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Render exits 0 but produces a 0-byte file — treated as failure."""
    project = tmp_path / "probe"
    project.mkdir()
    fake = _fake_subprocess_run(
        lint=FakeProcess(returncode=0, stdout=_lint_ok_payload()),
        render=FakeProcess(returncode=0, stdout=""),
        create_output=True,
        output_bytes=b"",
    )
    monkeypatch.setattr(subprocess, "run", fake)

    tool = HyperframesTool(default_project_dir=project)
    result = tool(None, {})
    assert result["status"] == "render_failed"
    assert result["output_size_bytes"] == 0
    assert result["output_md5"] is None


# -- configuration & failure modes ----------------------------------------


def test_missing_project_dir_raises(tmp_path: Path) -> None:
    tool = HyperframesTool()  # no default
    with pytest.raises(ValueError) as excinfo:
        tool(None, {})
    assert "project_dir" in str(excinfo.value)


def test_render_timeout_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "probe"
    project.mkdir()

    def _timeout_on_render(cmd, **kwargs):
        if cmd[2] == "lint":
            return FakeProcess(returncode=0, stdout=_lint_ok_payload())
        raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", _timeout_on_render)
    tool = HyperframesTool(default_project_dir=project)
    with pytest.raises(subprocess.TimeoutExpired):
        tool(None, {"render_timeout_s": 0.001})


def test_stdout_tail_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "probe"
    project.mkdir()
    big_stdout = "x" * 10_000
    fake = _fake_subprocess_run(
        lint=FakeProcess(returncode=0, stdout=_lint_ok_payload()),
        render=FakeProcess(returncode=0, stdout=big_stdout),
        create_output=True,
        output_bytes=b"ok",
    )
    monkeypatch.setattr(subprocess, "run", fake)

    tool = HyperframesTool(default_project_dir=project)
    result = tool(None, {})
    assert len(result["stdout_tail"]) == 500  # cap from _tail()


# -- dispatcher integration -----------------------------------------------


def test_adapter_works_through_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through src.producer.dispatch — EventLog captures payload."""
    from src.producer import dispatch, open_event_log
    from tests.producer.conftest import minimal_manifest

    project = tmp_path / "probe"
    project.mkdir()

    fake = _fake_subprocess_run(
        lint=FakeProcess(returncode=0, stdout=_lint_ok_payload()),
        render=FakeProcess(returncode=0, stdout="Render complete"),
        create_output=True,
        output_bytes=b"\x00" * 4096,
    )
    monkeypatch.setattr(subprocess, "run", fake)

    tool = HyperframesTool(default_project_dir=project)
    manifest = minimal_manifest()

    with open_event_log(tmp_path / "events.db") as log:
        result = dispatch(
            agent="editor_agent",
            shot_id=None,
            manifest=manifest,
            ctx={},
            tool=tool,
            events=log,
        )
        assert result.result["status"] == "ok"
        assert result.result["renderer"] == "hyperframes"
        # Event log captures both events
        events = log.recent()
        kinds = [e.kind for e in events]
        assert "dispatch_intent" in kinds
        assert "dispatch_result" in kinds
        # Result event carries the adapter payload
        result_event = next(e for e in events if e.kind == "dispatch_result")
        assert result_event.payload["result"]["renderer_version"] == "0.4.12"
        assert result_event.payload["result"]["output_size_bytes"] == 4096
