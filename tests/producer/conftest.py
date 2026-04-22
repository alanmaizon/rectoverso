"""Shared fixtures for Producer runtime tests.

Provides:
    - FakeTool: minimal Tool implementation parameterized by name and result.
    - minimal_manifest: the smallest manifest that passes schema validation,
      used as a starting point for runtime tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


@dataclass
class FakeTool:
    """A Tool Protocol adapter for tests.

    Configure with a name (must match the agent string in contract dispatch)
    and either a static result or a callable that computes one. The adapter
    records every invocation in `calls` so tests can assert on dispatch order.
    """

    name: str
    result: Mapping[str, Any] | None = None
    result_fn: Callable[[str | None, Mapping[str, Any]], Mapping[str, Any]] | None = None
    raises: BaseException | None = None
    calls: list[tuple[str | None, Mapping[str, Any]]] = field(default_factory=list)

    def __call__(
        self, shot_id: str | None, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append((shot_id, dict(payload)))
        if self.raises is not None:
            raise self.raises
        if self.result_fn is not None:
            return self.result_fn(shot_id, payload)
        return dict(self.result or {})


def minimal_manifest() -> dict[str, Any]:
    """Smallest manifest that passes schema validation.

    Extended by tests as needed. Intentionally boring — the goal is to make
    the `was_dirty` / `last_event_id` / atomicity behaviors testable without
    schema fights distracting from the runtime logic.
    """
    return {
        "manifest_version": "1.0",
        "project_id": "proj_test",
        "created_at": "2026-04-22T00:00:00Z",
        "updated_at": "2026-04-22T00:00:00Z",
        "brief": {
            "logline": "Test film",
            "target_duration_s": 30.0,
            "tone": ["quiet"],
            "genre": "drama",
            "source_path": "inputs/brief.md",
        },
        "script": {"status": "draft", "version": 1, "path": "artifacts/script.md"},
        "shots": [],
        "audio": {"dialogue": [], "sfx": []},
        "edit": {"status": "pending", "renderer": "hyperframes"},
        "budget": {
            "cap_usd": 151.0,
            "spent_usd": 0.0,
            "by_provider": {},
            "alibaba_quota_remaining": 50,
            "elevenlabs_credits_remaining": 117999,
        },
        "run_state": {
            "current_stage": "script",
            "last_event_id": 0,
            "resumable": True,
        },
        "creative_decisions": [],
    }


def add_minimal_shot(manifest: dict[str, Any], shot_id: str = "sh_001") -> dict[str, Any]:
    """Append a minimal valid shot with status='created'. Returns the shot."""
    shot: dict[str, Any] = {
        "shot_id": shot_id,
        "scene": 1,
        "order": len(manifest["shots"]) + 1,
        "description": "test shot",
        "duration_s": 3.0,
        "has_humans": False,
        "is_hero": False,
        "motion_level": "low",
        "continuity_refs": [],
        "prompt": {"authored_by": "pending", "primary": "pending"},
        "routing": {
            "chosen_provider": "pending",
            "chosen_model": "pending",
            "rationale": "pending",
            "decided_by": "pending",
            "decided_at": "2026-04-22T00:00:00Z",
            "alternates": [],
        },
        "attempts": [],
        "status": "created",
        "history": [],
        "judge_feedback": [],
        "creative_feedback": [],
    }
    manifest["shots"].append(shot)
    return shot
