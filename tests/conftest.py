"""Shared test fixtures for rectoverso.

Run: pip install -r tests/requirements.txt && pytest tests/
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "manifest.schema.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="session")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture(scope="session")
def validator(schema: dict) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture
def minimal_manifest() -> dict:
    """A manifest that validates against the schema with zero shots.

    Tests mutate a deepcopy to construct targeted scenarios.
    """
    ts = _iso_now()
    return {
        "manifest_version": "1.0",
        "project_id": "proj_test_fixture",
        "created_at": ts,
        "updated_at": ts,
        "brief": {
            "logline": "A test brief.",
            "target_duration_s": 30.0,
            "tone": ["terse"],
            "genre": "drama",
            "source_path": "inputs/brief.md",
        },
        "script": {"status": "draft", "version": 1, "path": "artifacts/script/v1.fountain"},
        "shots": [],
        "audio": {"dialogue": [], "sfx": []},
        "edit": {"status": "pending", "renderer": "hyperframes"},
        "budget": {
            "cap_usd": 151.0,
            "spent_usd": 0.0,
            "by_provider": {},
            "alibaba_quota_remaining": 72,
            "elevenlabs_credits_remaining": 117999,
        },
        "run_state": {"current_stage": "script", "last_event_id": 0, "resumable": True},
        "creative_decisions": [],
    }


@pytest.fixture
def make_shot() -> Callable[..., dict]:
    """Factory for a minimal valid shot object, overridable via kwargs."""

    def _factory(shot_id: str = "sh_001", **overrides: Any) -> dict:
        base = {
            "shot_id": shot_id,
            "scene": 1,
            "order": int(shot_id.split("_")[1]),
            "description": "A test shot.",
            "duration_s": 3.0,
            "has_humans": False,
            "is_hero": False,
            "motion_level": "low",
            "continuity_refs": [],
            "prompt": {"authored_by": "prompt_smith", "primary": "placeholder"},
            "routing": {
                "chosen_provider": "alibaba_wan_2_7_plus",
                "chosen_model": "wan2.7-t2v",
                "rationale": "test",
                "decided_by": "router",
                "decided_at": _iso_now(),
                "alternates": [],
            },
            "attempts": [],
            "status": "created",
            "history": [],
            "judge_feedback": [],
            "creative_feedback": [],
        }
        base.update(overrides)
        return base

    return _factory


@pytest.fixture
def deepcopy_() -> Callable[[Any], Any]:
    return copy.deepcopy
