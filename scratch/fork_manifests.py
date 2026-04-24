#!/usr/bin/env python3
"""Fork state/manifest_wan.json into provider-specific variants.

Each variant routes every shot to the named provider (except Veo, where
sh_006 is dropped per humans_never_veo). Preserves prompts + schema validity.
Also clears any already-completed attempts so each variant starts clean.
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")
from src.producer import validate_manifest, save_manifest_atomic  # noqa: E402


NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

BASE = json.loads(Path("state/manifest_wan.json").read_text())


def fork(project_id: str, provider: str, model: str, drop_shots: list[str] | None = None) -> dict:
    m = deepcopy(BASE)
    m["project_id"] = project_id
    m["created_at"] = NOW
    m["updated_at"] = NOW
    m["run_state"] = {"current_stage": "make", "last_event_id": 0, "resumable": True}
    m["budget"] = {
        "cap_usd": 151.0,
        "spent_usd": 0.0,
        "by_provider": {},
        "alibaba_quota_remaining": 75,
        "elevenlabs_credits_remaining": 117999,
        "editor_estimate_usd": 0.0,
    }

    kept = []
    for shot in m["shots"]:
        if drop_shots and shot["shot_id"] in drop_shots:
            continue
        shot["routing"] = {
            "chosen_provider": provider,
            "chosen_model": model,
            "decided_at": NOW,
            "decided_by": "producer",
            "rationale": f"variant film — all shots routed to {provider}",
            "alternates": [],
        }
        shot["attempts"] = []
        shot["status"] = "prompted"
        shot["history"] = [
            {"ts": NOW, "event": "prompted", "by": "prompt_smith", "detail": "prompt authored"},
            {"ts": NOW, "event": "routed", "by": "producer", "detail": f"-> {provider}"},
        ]
        # Clear any final block and judge feedback
        shot.pop("final", None)
        shot["judge_feedback"] = []
        shot["creative_feedback"] = []
        kept.append(shot)
    m["shots"] = kept
    return m


variants = [
    ("proj_here_veo", "vertex_veo_3_1_fast", "veo-3.1-fast-generate-001", ["sh_006"], "state/manifest_veo.json"),
    ("proj_here_kling", "fal_kling_2_1_pro", "fal-ai/kling-video/v2.1/pro/image-to-video", None, "state/manifest_kling.json"),
    ("proj_here_seedance", "fal_bytedance_seedance_2_0_fast_i2v", "bytedance/seedance-2.0/fast/image-to-video", None, "state/manifest_seedance.json"),
]

for project_id, provider, model, drop, out_path in variants:
    m = fork(project_id, provider, model, drop)
    print(f"{project_id}: {len(m['shots'])} shots -> {provider}")
    validate_manifest(m)
    save_manifest_atomic(Path(out_path), m, last_event_id=0)
    print(f"  wrote {out_path}")

# Also reset Wan manifest to clean state (keeping sh_002's attempt since it rendered)
wan = deepcopy(BASE)
wan["project_id"] = "proj_here_wan"
print(f"\nproj_here_wan: {len(wan['shots'])} shots -> alibaba_wan_2_7_plus (sh_002 already rendered)")
validate_manifest(wan)
save_manifest_atomic(Path("state/manifest_wan.json"), wan, last_event_id=wan["run_state"]["last_event_id"])
print("  kept state/manifest_wan.json")
