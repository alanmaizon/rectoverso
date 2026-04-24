"""MockEditorSession — demo-mode stub for the EditorSession Protocol.

Extracts a pre-authored tarball into workspace_dir instead of running a real
Managed Agents session. All downstream integrity checks (render_md5,
uploaded_sha256) are computed from the actual extracted bytes so EditorTool's
_project_session_result validates the result without special-casing.
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

from ._common import md5_file, sha256_file
from .editor import EditorSession, EditorSessionResult


@dataclass(frozen=True)
class MockEditorSession:
    """Fixture-backed EditorSession stub.

    Picks the first *.tar.gz in fixture_dir, extracts it into workspace_dir,
    and returns a fully-populated EditorSessionResult with integrity fields
    computed from the real extracted bytes.
    """

    fixture_dir: Path

    def run(
        self,
        *,
        system_prompt: str,
        skills: tuple[str, ...],
        model: str,
        apt_packages: tuple[str, ...],
        workspace_dir: Path,
        initial_message: str,
        timeout_s: float,
    ) -> EditorSessionResult:
        fixtures = sorted(self.fixture_dir.glob("*.tar.gz"))
        if not fixtures:
            return EditorSessionResult(
                verdict="failed",
                failure_stage="mock_fixture_missing",
                final_payload={},
                cost_usd=0.0,
                latency_s=0.1,
                stderr_tail=f"no .tar.gz fixtures found in {self.fixture_dir}",
            )

        bundle_path = fixtures[0]
        _safe_extract(bundle_path, workspace_dir)

        mp4_path = workspace_dir / "out.mp4"
        render_md5 = md5_file(mp4_path) if mp4_path.exists() else "0" * 32
        uploaded_sha256 = sha256_file(bundle_path)
        duration_s = _read_duration(workspace_dir / "composition.json")

        final_payload = {
            "verdict": "PASS",
            "composition_path": "composition.json",
            "composition_archive_path": "composition.zip",
            "render_path": "out.mp4",
            "render_md5": render_md5,
            "duration_s": duration_s,
            "renderer_version": "1.0.0-mock",
            "uploaded_sha256": uploaded_sha256,
            "notes": "mocked successfully",
        }

        return EditorSessionResult(
            verdict="ok",
            failure_stage=None,
            final_payload=final_payload,
            cost_usd=0.0,
            latency_s=0.1,
        )


def _safe_extract(bundle_path: Path, dest: Path) -> None:
    """Extract a tar.gz into dest, skipping any member that would escape dest."""
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with tarfile.open(bundle_path, "r:gz") as tar:
        for member in tar.getmembers():
            member_path = (dest / member.name).resolve()
            if dest_resolved not in member_path.parents and dest_resolved != member_path:
                continue
            tar.extract(member, dest)


def _read_duration(composition_path: Path) -> float:
    try:
        return float(json.loads(composition_path.read_text())["duration_s"])
    except Exception:
        return 2.0
