"""CLI-boundary tests for `rectoverso generate-ref`.

Hermetic: QwenImageTool is monkeypatched inside generate_ref_cmd to a fake
returning a canned result. Mirrors the pattern used in test_judge.py /
test_revise.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.rectoverso.cli import main
from tests.producer.conftest import minimal_manifest, add_minimal_shot


def _write_manifest(path: Path, manifest: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest))
    return path


def _manifest_with_shot(tmp_path: Path) -> Path:
    m = minimal_manifest()
    m["brief"]["artistic_style"] = "naturalistic, cold color palette, handheld"
    m["brief"]["tone"] = ["quiet", "solitary"]
    shot = add_minimal_shot(m, "sh_006")
    shot["description"] = (
        "A solitary figure in a dark weatherproof coat walks down a misty forest path."
    )
    return _write_manifest(tmp_path / "state/manifest.json", m)


class _FakeQwenTool:
    name = "image_generator"

    def __init__(self, *, result: dict, **_: Any) -> None:
        self._result = result
        self.calls: list[tuple] = []

    def __call__(self, shot_id, payload):
        self.calls.append((shot_id, dict(payload)))
        return dict(self._result)


def _patch_qwen(monkeypatch: pytest.MonkeyPatch, result: dict) -> list:
    captured: list = []

    def _factory(*_a, **_kw):
        fake = _FakeQwenTool(result=result)
        captured.append(fake)
        return fake

    monkeypatch.setattr(
        "src.rectoverso.generate_ref_cmd.QwenImageTool", _factory
    )
    return captured


def _patch_nano(monkeypatch: pytest.MonkeyPatch, result: dict) -> list:
    """Same pattern as _patch_qwen but for NanoBananaImageTool."""
    captured: list = []

    def _factory(*_a, **_kw):
        fake = _FakeQwenTool(result=result)   # same stub shape works
        captured.append(fake)
        return fake

    monkeypatch.setattr(
        "src.rectoverso.generate_ref_cmd.NanoBananaImageTool", _factory
    )
    return captured


def _ok_result(
    tmp_path: Path,
    shot_id: str = "sh_006",
    *,
    provider: str = "dashscope_qwen_image",
    model: str = "qwen-image-plus",
    cost_usd: float = 0.0,
) -> dict:
    """Simulate a successful image-gen call: PNG already on disk."""
    out = tmp_path / "artifacts/refs" / f"{shot_id}_v1.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"\x89PNG\r\n\x1a\n_fakepng_" + b"\x00" * 512)
    return {
        "status": "ok",
        "provider": provider,
        "model": model,
        "task_id": "img_task_1",
        "image_path": str(out),
        "image_md5": "deadbeef" * 4,
        "output_size_bytes": out.stat().st_size,
        "cost_usd": cost_usd,
        "quota_cost": 0 if "qwen" not in provider else 1,
        "latency_s": 8.2,
        "size": "1664*928",
    }


def _failed_result(stage: str = "content_policy", provider: str = "dashscope_qwen_image") -> dict:
    return {
        "status": "failed",
        "failure_stage": stage,
        "provider": provider,
        "model": "qwen-image-plus" if "qwen" in provider else "gemini-2.5-flash-image",
        "image_path": "",
        "image_md5": None,
        "output_size_bytes": 0,
        "cost_usd": 0.0,
        "quota_cost": 0,
        "latency_s": 1.5,
        "size": "1664*928",
        "stderr_tail": "blocked by content inspector",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_generate_ref_ok_projects_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _manifest_with_shot(tmp_path)
    monkeypatch.chdir(tmp_path)
    _patch_qwen(monkeypatch, _ok_result(tmp_path))

    code = main([
        "generate-ref",
        "--shot", "sh_006",
        "--events-db", str(tmp_path / "state/events.db"),
        "--output-root", str(tmp_path / "artifacts/refs"),
        "--json",
        str(manifest_path),
    ])
    assert code == 0

    m = json.loads(manifest_path.read_text())
    shot = m["shots"][0]
    refs = shot["prompt"].get("reference_subject_paths") or []
    assert len(refs) == 1
    assert refs[0].endswith("sh_006_v1.png")
    # History carries the audit row
    events = [h["event"] for h in shot["history"]]
    assert "reference_generated" in events


def test_generate_ref_passes_composed_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_with_shot(tmp_path)
    monkeypatch.chdir(tmp_path)
    captured = _patch_qwen(monkeypatch, _ok_result(tmp_path))

    main([
        "generate-ref",
        "--shot", "sh_006",
        "--events-db", str(tmp_path / "state/events.db"),
        "--output-root", str(tmp_path / "artifacts/refs"),
        "--aspect-ratio", "16:9",
        "--seed", "42",
        "--json",
        str(manifest_path),
    ])

    fake = captured[0]
    assert len(fake.calls) == 1
    shot_id, payload = fake.calls[0]
    assert shot_id == "sh_006"
    # Composed prompt should mention the shot description + brief style
    assert "solitary figure" in payload["prompt"]
    assert "cold color palette" in payload["prompt"] or "cold" in payload["prompt"].lower()
    # Defaults + overrides honored
    assert payload["aspect_ratio"] == "16:9"
    assert payload["seed"] == 42
    # Default negatives applied
    assert "watermark" in payload["negative_prompt"]


def test_generate_ref_prompt_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_with_shot(tmp_path)
    monkeypatch.chdir(tmp_path)
    captured = _patch_qwen(monkeypatch, _ok_result(tmp_path))

    main([
        "generate-ref",
        "--shot", "sh_006",
        "--events-db", str(tmp_path / "state/events.db"),
        "--output-root", str(tmp_path / "artifacts/refs"),
        "--prompt-override", "Exact override, no composition",
        "--json",
        str(manifest_path),
    ])

    _, payload = captured[0].calls[0]
    assert payload["prompt"] == "Exact override, no composition"


def test_generate_ref_appends_to_existing_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = minimal_manifest()
    shot = add_minimal_shot(m, "sh_006")
    shot["description"] = "a forest path"
    shot["prompt"]["reference_subject_paths"] = ["artifacts/refs/existing.png"]
    manifest_path = _write_manifest(tmp_path / "state/manifest.json", m)
    monkeypatch.chdir(tmp_path)
    _patch_qwen(monkeypatch, _ok_result(tmp_path))

    main([
        "generate-ref",
        "--shot", "sh_006",
        "--events-db", str(tmp_path / "state/events.db"),
        "--output-root", str(tmp_path / "artifacts/refs"),
        "--json",
        str(manifest_path),
    ])

    saved = json.loads(manifest_path.read_text())
    refs = saved["shots"][0]["prompt"]["reference_subject_paths"]
    assert len(refs) == 2
    assert refs[0] == "artifacts/refs/existing.png"
    assert refs[1].endswith("sh_006_v1.png")


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


def test_generate_ref_missing_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_qwen(monkeypatch, _ok_result(tmp_path))
    code = main([
        "generate-ref",
        "--shot", "sh_006",
        "--events-db", str(tmp_path / "events.db"),
        str(tmp_path / "nope.json"),
    ])
    assert code == 2


def test_generate_ref_shot_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _manifest_with_shot(tmp_path)
    _patch_qwen(monkeypatch, _ok_result(tmp_path))
    code = main([
        "generate-ref",
        "--shot", "sh_nope",
        "--events-db", str(tmp_path / "state/events.db"),
        str(manifest_path),
    ])
    assert code == 2


def test_generate_ref_failure_logs_reference_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_with_shot(tmp_path)
    monkeypatch.chdir(tmp_path)
    _patch_qwen(monkeypatch, _failed_result("content_policy"))

    # Pin to qwen — don't auto-fall-through to nano-banana, so this test
    # exercises the single-provider-failure code path.
    code = main([
        "generate-ref",
        "--shot", "sh_006",
        "--events-db", str(tmp_path / "state/events.db"),
        "--output-root", str(tmp_path / "artifacts/refs"),
        "--provider", "qwen",
        "--json",
        str(manifest_path),
    ])
    assert code == 11

    m = json.loads(manifest_path.read_text())
    shot = m["shots"][0]
    events = [h["event"] for h in shot["history"]]
    assert "reference_failed" in events
    # reference_subject_paths must NOT have grown
    refs = shot["prompt"].get("reference_subject_paths") or []
    assert refs == []


# ---------------------------------------------------------------------------
# --provider flag + auto fallback
# ---------------------------------------------------------------------------


def test_provider_nano_banana_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_with_shot(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Only nano-banana is patched. If the CLI tried Qwen, it would hit the
    # real (unmocked) tool and fail.
    nano_captured = _patch_nano(monkeypatch, _ok_result(
        tmp_path, provider="gemini_nano_banana", model="gemini-2.5-flash-image",
        cost_usd=0.039,
    ))
    # Defensive: if Qwen gets instantiated (shouldn't), raise loudly.
    _patch_qwen(monkeypatch, {"status": "failed", "failure_stage": "should_not_be_called"})

    code = main([
        "generate-ref",
        "--shot", "sh_006",
        "--events-db", str(tmp_path / "state/events.db"),
        "--output-root", str(tmp_path / "artifacts/refs"),
        "--provider", "nano-banana",
        "--json",
        str(manifest_path),
    ])
    assert code == 0
    assert len(nano_captured) == 1
    payload = json.loads(_stdout(tmp_path, "generate-ref")) if False else None  # placeholder

    saved = json.loads(manifest_path.read_text())
    # History row attributes the generation to nano-banana
    events = [h for h in saved["shots"][0]["history"] if h["event"] == "reference_generated"]
    assert events
    assert events[-1]["by"] == "gemini_nano_banana"
    assert "provider=gemini_nano_banana" in events[-1]["detail"]


def test_provider_auto_falls_back_on_qwen_content_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_with_shot(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Qwen refuses; nano-banana succeeds.
    qwen_captured = _patch_qwen(monkeypatch, _failed_result("content_policy"))
    nano_captured = _patch_nano(monkeypatch, _ok_result(
        tmp_path, provider="gemini_nano_banana", model="gemini-2.5-flash-image",
        cost_usd=0.039,
    ))

    code = main([
        "generate-ref",
        "--shot", "sh_006",
        "--events-db", str(tmp_path / "state/events.db"),
        "--output-root", str(tmp_path / "artifacts/refs"),
        "--provider", "auto",
        "--json",
        str(manifest_path),
    ])
    assert code == 0
    # Both got called, in order
    assert len(qwen_captured) == 1
    assert len(nano_captured) == 1

    saved = json.loads(manifest_path.read_text())
    shot = saved["shots"][0]
    assert len(shot["prompt"]["reference_subject_paths"]) == 1
    # History records nano-banana as the generator + the fallback origin
    gens = [h for h in shot["history"] if h["event"] == "reference_generated"]
    assert gens[-1]["by"] == "gemini_nano_banana"
    assert "fallback_from=dashscope_qwen_image" in gens[-1]["detail"]


def test_provider_auto_does_not_fall_back_on_non_content_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth / rate_limit / validation failures are NOT retried with the other
    provider — same prompt will probably fail the other API too, and we'd
    rather surface the root cause."""
    manifest_path = _manifest_with_shot(tmp_path)
    monkeypatch.chdir(tmp_path)
    qwen_captured = _patch_qwen(monkeypatch, _failed_result("rate_limit"))
    nano_captured = _patch_nano(monkeypatch, _ok_result(
        tmp_path, provider="gemini_nano_banana", model="gemini-2.5-flash-image",
    ))

    code = main([
        "generate-ref",
        "--shot", "sh_006",
        "--events-db", str(tmp_path / "state/events.db"),
        "--output-root", str(tmp_path / "artifacts/refs"),
        "--provider", "auto",
        "--json",
        str(manifest_path),
    ])
    assert code == 11
    # Qwen tried once, nano-banana never touched
    assert len(qwen_captured) == 1
    assert len(nano_captured) == 0


def test_provider_auto_both_refuse_returns_content_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_with_shot(tmp_path)
    monkeypatch.chdir(tmp_path)
    qwen_captured = _patch_qwen(monkeypatch, _failed_result("content_policy"))
    nano_captured = _patch_nano(
        monkeypatch,
        _failed_result("content_policy", provider="gemini_nano_banana"),
    )

    code = main([
        "generate-ref",
        "--shot", "sh_006",
        "--events-db", str(tmp_path / "state/events.db"),
        "--output-root", str(tmp_path / "artifacts/refs"),
        "--provider", "auto",
        "--json",
        str(manifest_path),
    ])
    assert code == 11
    # Both tried
    assert len(qwen_captured) == 1
    assert len(nano_captured) == 1
    saved = json.loads(manifest_path.read_text())
    # Final reference_failed row attributes to nano-banana (last one we tried)
    fails = [h for h in saved["shots"][0]["history"] if h["event"] == "reference_failed"]
    assert fails[-1]["by"] == "gemini_nano_banana"


def _stdout(tmp_path: Path, _name: str) -> str:  # pragma: no cover — helper stub
    return ""
