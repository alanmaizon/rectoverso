import hashlib
import json
import os
import tarfile
import tempfile
from pathlib import Path

import pytest

from src.producer.editor import EditorTool
from src.producer.editor_session_mock import MockEditorSession


@pytest.fixture
def dummy_fixture_dir(tmp_path):
    target_dir = tmp_path / "fixtures"
    target_dir.mkdir()
    
    # Create a dummy bundle
    bundle_name = "bundle_test"
    tar_path = target_dir / f"{bundle_name}.tar.gz"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        
        mp4_path = tmp / "out.mp4"
        mp4_path.write_bytes(b"test video")
        
        comp_path = tmp / "composition.json"
        comp_path.write_text(json.dumps({"duration_s": 5.0, "title": "Test Shot"}))

        zip_path = tmp / "composition.zip"
        zip_path.write_bytes(b"test zip")

        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(mp4_path, arcname="out.mp4")
            tar.add(comp_path, arcname="composition.json")
            tar.add(zip_path, arcname="composition.zip")
            
    return target_dir


def test_mock_editor_session_run(dummy_fixture_dir, tmp_path):
    session = MockEditorSession(fixture_dir=dummy_fixture_dir)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    
    # We call run directly
    result = session.run(
        system_prompt="...",
        skills=("skill1",),
        model="test-model",
        apt_packages=(),
        workspace_dir=workspace_dir,
        initial_message="test message",
        timeout_s=10.0,
    )
    
    # 1. EditorSessionResult shape has all required fields non-empty
    assert result.verdict == "ok"
    assert result.final_payload["verdict"] == "PASS"
    assert result.final_payload["composition_path"] == "composition.json"
    assert result.final_payload["render_path"] == "out.mp4"
    assert result.final_payload["composition_archive_path"] == "composition.zip"
    
    # 2. Fixture extraction populates workspace_dir
    extracted_mp4 = workspace_dir / "out.mp4"
    assert extracted_mp4.exists()
    assert extracted_mp4.read_bytes() == b"test video"
    
    extracted_comp = workspace_dir / "composition.json"
    assert extracted_comp.exists()
    
    # 3. render_md5 matches md5_file(extracted_mp4)
    expected_md5 = hashlib.md5(b"test video").hexdigest()
    assert result.final_payload["render_md5"] == expected_md5
    
    # 4. uploaded_sha256 matches sha256 of the bundle
    bundle_path = list(dummy_fixture_dir.glob("*.tar.gz"))[0]
    expected_sha256 = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    assert result.final_payload["uploaded_sha256"] == expected_sha256


def test_editor_tool_from_env_demo_mode(dummy_fixture_dir):
    # 5. EditorTool.from_env(demo_mode=True, fixture_dir=...) gives _session that is a MockEditorSession
    tool = EditorTool.from_env(demo_mode=True, fixture_dir=dummy_fixture_dir)
    assert isinstance(tool._session, MockEditorSession)
    assert tool._session.fixture_dir == dummy_fixture_dir
