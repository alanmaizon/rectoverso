#!/usr/bin/env python3
"""Build state/manifest.json from brief.md as Screenwriter+PromptSmith+Producer.

Run manually (no Anthropic API calls). Validates against schema before saving.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")
from producer import validate_manifest, save_manifest_atomic  # noqa: E402


NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

NEGATIVE = (
    "text, watermark, logo, timestamps, subtitle, caption, lens flare, sparkles, "
    "portal effect, morphing, doorway, doorframe in frame, cartoon, anime, "
    "oversaturation, fast camera whip pan, shaky cam, film grain overlay"
)

# Voiceover lines (me acting as Audio Agent dispatch planner).
# voice_id is the ElevenLabs "child" preset we'll pick at render time; leave a
# placeholder label here — the real voice_id gets resolved by the audio adapter
# at dispatch (it rewrites it to the ElevenLabs voice id).
VO_LINES = {
    "sh_002": "There are places on this planet you will never stand in.",
    "sh_003": "A flood at golden hour.",
    "sh_004": "A canopy you cannot reach except by wing.",
    "sh_006": "And a kitchen, in a quiet morning, where someone you love is making tea.",
    "sh_007": "All of it. Here.",
}

def vo_cues(shot_id: str) -> list[dict]:
    text = VO_LINES.get(shot_id)
    if not text:
        return []
    # Duration-estimate: 0.38s/word, floor 1.0s.
    est = max(1.0, 0.38 * len(text.split()))
    return [{
        "mode": "tts",
        "line_id": f"{shot_id}_vo_1",
        "text": text,
        "voice_id": "child_narrator",
        "model_id": "eleven_multilingual_v2",
        "language_code": "en",
        "duration_s": round(est, 2),
    }]

SHOTS = [
    dict(
        shot_id="sh_001",
        order=1,
        scene=1,
        duration_s=6.0,
        description="Enniskerry, Ireland — quiet hallway at dawn.",
        has_humans=False,
        is_hero=False,
        motion_level="low",
        provider="alibaba_wan_2_7_plus",
        model="wan-2.7-plus",
        prompt=(
            "Interior cinematic wide of a narrow hallway in a small Irish cottage at dawn. "
            "Warm soft morning light spills from a single window at the far end of the hall. "
            "Dust motes drift slowly through the shaft of light. Worn wooden floorboards, "
            "faded wallpaper, a framed photo on the wall. Camera is locked off — completely still. "
            "Warm-neutral color palette, honest golden tones, not postcard-saturated. "
            "35mm film aesthetic, documentary stillness. No people, no animals."
        ),
    ),
    dict(
        shot_id="sh_002",
        order=2,
        scene=2,
        duration_s=10.0,
        description="Great Barrier Reef, Australia — mid-water, late afternoon sun.",
        has_humans=False,
        is_hero=False,
        motion_level="medium",
        provider="alibaba_wan_2_7_plus",
        model="wan-2.7-plus",
        prompt=(
            "Underwater cinematic wide shot inside the Great Barrier Reef, mid-water at late "
            "afternoon. Sun shafts cut down through clear blue water. A living coral reef — "
            "healthy, colorful, vivid but not cartoonish. A school of tropical parrotfish slowly "
            "parts and reforms in the foreground. Camera drifts forward at constant gentle pace, "
            "honest diver-POV. Visible particulate and believable light refraction. BBC Blue "
            "Planet documentary reference. No text, no divers, no equipment."
        ),
    ),
    dict(
        shot_id="sh_003",
        order=3,
        scene=3,
        duration_s=10.0,
        description="Okavango Delta, Botswana — aerial at golden hour, elephants wading.",
        has_humans=False,
        is_hero=False,
        motion_level="medium",
        provider="alibaba_wan_2_7_plus",
        model="wan-2.7-plus",
        prompt=(
            "Aerial wide shot drifting low and slow over the Okavango Delta at golden hour. "
            "Mirror-flat shallow water catches the warm orange sunset; reeds cut the surface in "
            "long parallel lines. Three elephants wade in the frame, water dragging behind them, "
            "reflections doubled on the flat surface. Camera moves at a steady slow exhale pace — "
            "low altitude, no banking, no drama. Distant birds on the horizon. Warm-neutral grade. "
            "Photographed on a cinema drone. No text, no aircraft visible, no humans."
        ),
    ),
    dict(
        shot_id="sh_004",
        order=4,
        scene=4,
        duration_s=9.0,
        description="Amazon canopy, Peru — wingtip-height aerial tracking shot.",
        has_humans=False,
        is_hero=False,
        motion_level="high",
        provider="alibaba_wan_2_7_plus",
        model="wan-2.7-plus",
        prompt=(
            "Aerial wingtip-height tracking shot 40 metres above the Amazon rainforest canopy. "
            "Uninterrupted green canopy stretches to a misty horizon. The camera flies forward at "
            "a constant even speed — no tilt, no bank, no swoop. A single macaw with red and blue "
            "plumage crosses frame left-to-right in the mid-ground. Late morning diffuse light, "
            "occasional humidity haze rising from the trees. BBC Planet Earth nature-documentary "
            "reference. No text, no aircraft, no people."
        ),
    ),
    dict(
        shot_id="sh_005",
        order=5,
        scene=5,
        duration_s=9.0,
        description="Hokkaido, Japan — frozen lake at night under aurora, a red fox crossing.",
        has_humans=False,
        is_hero=False,
        motion_level="low",
        provider="alibaba_wan_2_7_plus",
        model="wan-2.7-plus",
        prompt=(
            "Wide locked-off shot of a frozen lake at night in Hokkaido, Japan, deep winter. "
            "Snow-dusted ice in the foreground, dark fir trees at the far shore, green and violet "
            "aurora borealis overhead with a subtle reflection on the ice. A single plume of white "
            "steam rises from a thermal vent at the shoreline. A red fox walks across the ice from "
            "mid-left toward the centre of frame, stops, turns its head to look directly at the "
            "camera, and holds the gaze for a long beat. Camera is completely still. Cold blue-"
            "silver colour palette. Long-exposure look, quiet stillness."
        ),
    ),
    dict(
        shot_id="sh_006",
        order=6,
        scene=6,
        duration_s=10.0,
        description="Kitchen, Ireland — child in profile in the doorway, morning.",
        has_humans=True,
        is_hero=False,
        motion_level="low",
        provider="fal_kling_2_1_pro",
        model="fal-ai/kling-video/v2.1/pro/image-to-video",
        prompt=(
            "Cinematic interior medium shot of a child aged about 7 standing in the doorway of a "
            "small Irish cottage kitchen in soft warm morning light. The child is in profile — we "
            "see the side of their face, calm and thoughtful, NOT smiling. Simple pyjamas. In the "
            "background, a parent silhouetted at the sink with back to camera, tap running. A "
            "tabby cat sits on a wooden chair. Camera starts medium wide and pushes in very "
            "slowly on the child's face over the full duration. Warm-neutral natural light, 35mm "
            "film look, documentary realism. No dialogue, no smiling, no eye contact with camera."
        ),
    ),
    dict(
        shot_id="sh_007",
        order=7,
        scene=7,
        duration_s=6.0,
        description="Closing image — exterior wide of the Enniskerry cottage at dawn.",
        has_humans=False,
        is_hero=False,
        motion_level="low",
        provider="alibaba_wan_2_7_plus",
        model="wan-2.7-plus",
        prompt=(
            "Wide exterior shot of a small whitewashed Irish cottage at dawn, seen from across a "
            "dewy field. Soft golden morning light hits the front of the cottage; a thin plume of "
            "woodsmoke rises straight up from the stone chimney into still air. Mossy stone walls "
            "in the foreground, distant green hills. Camera is locked off, completely still. "
            "Warm-neutral palette, golden hour. Large-format camera aesthetic, documentary feel. "
            "No people, no animals, no vehicles, no text."
        ),
    ),
]


def build_shot(spec: dict) -> dict:
    spec = {**spec, "audio_cues": vo_cues(spec["shot_id"])}
    return {
        "shot_id": spec["shot_id"],
        "order": spec["order"],
        "scene": spec["scene"],
        "description": spec["description"],
        "duration_s": spec["duration_s"],
        "has_humans": spec["has_humans"],
        "is_hero": spec["is_hero"],
        "motion_level": spec["motion_level"],
        "continuity_refs": [],
        "prompt": {
            "authored_by": "prompt_smith",
            "primary": spec["prompt"],
            "negative": NEGATIVE,
            "reference_subject_paths": [],
        },
        "routing": {
            "chosen_provider": spec["provider"],
            "chosen_model": spec["model"],
            "decided_at": NOW,
            "decided_by": "producer",
            "rationale": (
                "human shot → Kling per humans_never_veo policy"
                if spec["has_humans"]
                else "workhorse Wan 2.7 Plus for restrained documentary shot, free quota"
            ),
            "alternates": [],
        },
        "attempts": [],
        "audio_cues": spec.get("audio_cues", []),
        "audio_status": "pending",
        "creative_feedback": [],
        "judge_feedback": [],
        "status": "prompted",
        "history": [
            {"ts": NOW, "event": "prompted", "by": "prompt_smith", "detail": "prompt authored"},
            {"ts": NOW, "event": "routed", "by": "producer", "detail": f"-> {spec['provider']}"},
        ],
    }


manifest = {
    "manifest_version": "1.0",
    "project_id": "proj_here",
    "created_at": NOW,
    "updated_at": NOW,
    "brief": {
        "source_path": "brief.md",
        "logline": (
            "A quiet montage of places most of us will never stand in — and the one we already "
            "do — carried by a single voice remembering that all of them are the same planet."
        ),
        "target_duration_s": 60.0,
        "genre": "documentary",
        "tone": ["restrained", "cinematic", "warm-neutral", "documentary"],
    },
    "script": {
        "path": "brief.md",
        "status": "approved",
        "version": 1,
        "approved_at": NOW,
        "approved_by": "producer",
    },
    "shots": [build_shot(s) for s in SHOTS],
    "audio": {
        "dialogue": [],
        "sfx": [],
    },
    "edit": {
        "status": "pending",
        "renderer": "hyperframes",
        "renderer_version": "v1.0.0",
    },
    "budget": {
        "cap_usd": 151.0,
        "spent_usd": 0.0,
        "by_provider": {},
        "alibaba_quota_remaining": 75,
        "elevenlabs_credits_remaining": 117999,
        "editor_estimate_usd": 0.0,
    },
    "creative_decisions": [],
    "film_status": "pending",
    "run_state": {
        "current_stage": "make",
        "last_event_id": 0,
        "resumable": True,
    },
}


# Validate before writing.
print("Validating against schemas/manifest.schema.json ...")
validate_manifest(manifest)
print("  ok")

out_path = Path("state/manifest.json")
out_path.parent.mkdir(parents=True, exist_ok=True)
save_manifest_atomic(out_path, manifest, last_event_id=0)
print(f"Wrote {out_path}")
print(f"Shots: {len(manifest['shots'])} | target={manifest['brief']['target_duration_s']}s")
provider_counts: dict[str, int] = {}
for s in manifest["shots"]:
    p = s["routing"]["chosen_provider"]
    provider_counts[p] = provider_counts.get(p, 0) + 1
for p, n in sorted(provider_counts.items()):
    print(f"  {p}: {n}")
