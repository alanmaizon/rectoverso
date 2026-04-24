#!/usr/bin/env python3
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone

def main():
    fixtures_dir = Path("demo/fixtures")
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Generate golden manifest
    manifest = {
        "manifest_version": "1.0",
        "project_id": "demo_project_golden",
        "brief": {
            "title": "Here",
            "target_duration_s": 60,
            "artistic_style": "Warm-neutral color of honest light, slow steady drone/steadicam moves. No VFX, no transitions. Restrained documentary photography."
        },
        "script": {"summary": "A quiet montage of places most of us will never stand in — and the one we already do — carried by a single voice."},
        "shots": [],
        "audio": {
            "status": "ok",
            "music_path": "artifacts/audio/orchestral_score.wav",
            "dialogue": [
                {"voice_id": "child", "audio_path": "artifacts/audio/vo_002.wav", "duration_s": 4.0, "timing": {"shot_id": "sh_002", "offset_s": 1.0}, "text": "There are places on this planet you will never stand in."},
                {"voice_id": "child", "audio_path": "artifacts/audio/vo_003.wav", "duration_s": 3.0, "timing": {"shot_id": "sh_003", "offset_s": 2.0}, "text": "A flood at golden hour."},
                {"voice_id": "child", "audio_path": "artifacts/audio/vo_004.wav", "duration_s": 3.5, "timing": {"shot_id": "sh_004", "offset_s": 1.0}, "text": "A canopy you cannot reach except by wing."},
                {"voice_id": "child", "audio_path": "artifacts/audio/vo_006.wav", "duration_s": 5.0, "timing": {"shot_id": "sh_006", "offset_s": 1.0}, "text": "And a kitchen, in a quiet morning, where someone you love is making tea."},
                {"voice_id": "child", "audio_path": "artifacts/audio/vo_007.wav", "duration_s": 2.0, "timing": {"shot_id": "sh_007", "offset_s": 3.0}, "text": "All of it. Here."}
            ]
        },
        "edit": {
            "status": "approved",
            "renderer": "hyperframes",
            "composition_path": "artifacts/edit/composition.json",
            "render_path": "artifacts/edit/out.mp4",
            "render_md5": "demo_md5_hash",
            "total_duration_s": 60.0
        },
        "budget": {"cap_usd": 150, "spent_usd": 4.15},
        "run_state": {"resumable": True}
    }

    shots_data = [
        ("sh_001", 6.0, "Enniskerry, Ireland (interior cottage, dawn)", "Wide, still frame of a quiet hallway in soft morning light. Dust motes.", "veo", "veo-3.1-fast-generate-001"),
        ("sh_002", 10.0, "Great Barrier Reef, Australia (underwater)", "Slow drifting camera through the reef, mid-water, late afternoon sun shafting down from above.", "wan", "wan-2.7-plus"),
        ("sh_003", 10.0, "Okavango Delta, Botswana (golden hour)", "Aerial wide shot, drifting low over the flood at golden hour: elephants wading.", "wan", "wan-2.7-plus"),
        ("sh_004", 9.0, "Amazon canopy, Peru (aerial)", "Wingtip-height pass across the Amazon canopy from 40 metres up. A macaw crosses frame left-to-right.", "veo", "veo-3.1-fast-generate-001"),
        ("sh_005", 9.0, "Hokkaido, Japan (winter, night)", "A held wide shot of a frozen lake under aurora, steam rising from a thermal vent. A single red fox crosses the ice.", "wan", "wan-2.7-plus"),
        ("sh_006", 10.0, "Kitchen, Ireland (morning)", "Soft profile of a child, age ~7, standing in the doorway of their own kitchen in warm morning light.", "kling", "fal-ai/kling-video/v2.1/pro/image-to-video"),
        ("sh_007", 6.0, "Closing image — wide exterior, Ireland", "A clean, wide exterior shot of the cottage at dawn, smoke lifting from the chimney.", "wan", "wan-2.7-plus"),
    ]

    for shot_id, duration, location, desc, provider, model in shots_data:
        shot = {
            "shot_id": shot_id,
            "description": f"{location} - {desc}",
            "duration_s": duration,
            "continuity_refs": [],
            "prompt": {
                "primary": desc,
                "reference_subject_paths": []
            },
            "routing": {
                "chosen_provider": provider,
                "chosen_model": model,
                "estimated_cost_usd": 0.49 if provider == "kling" else (0.6 if provider == "veo" else 0.0),
                "reasoning": "Routed based on human presence and motion levels"
            },
            "attempts": [
                {
                    "attempt_id": 1,
                    "prompt_revision": False,
                    "render_path": f"artifacts/renders/{shot_id}.mp4",
                    "provider": provider,
                    "cost_usd": 0.49 if provider == "kling" else (0.6 if provider == "veo" else 0.0),
                    "latency_s": 45.0,
                    "outcome": "approved",
                    "judge_score": 0.88,
                    "judge_notes": "Beautifully composed, matches the restrained documentary style."
                }
            ],
            "final": {
                "render_path": f"artifacts/renders/{shot_id}.mp4",
                "normalized_path": f"artifacts/renders/{shot_id}_norm.mp4"
            },
            "status": "approved",
            "audio_status": "ok",
            "history": []
        }
        manifest["shots"].append(shot)
    
    manifest_path = fixtures_dir / "manifest_golden.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Generated {manifest_path}")

    # 2. Fake some events
    db_path = Path("state/events.db")
    if db_path.exists():
        db_path.unlink()
    
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute('''CREATE TABLE IF NOT EXISTS events (
        event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT    NOT NULL,
        kind         TEXT    NOT NULL,
        agent        TEXT,
        shot_id      TEXT,
        ref_event_id INTEGER,
        payload      TEXT    NOT NULL,
        FOREIGN KEY(ref_event_id) REFERENCES events(event_id)
    );''')
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO events (ts, kind, agent, payload) VALUES (?, ?, ?, ?)", (now, "run_start", "demo", "{}"))
    conn.commit()
    conn.close()
    
    print("Generated mock events.db")

    # 3. Create mock videos
    renders_dir = Path("artifacts/renders")
    renders_dir.mkdir(parents=True, exist_ok=True)
    for shot_id, _, _, _, _, _ in shots_data:
        (renders_dir / f"{shot_id}.mp4").write_text("fake video content")
        (renders_dir / f"{shot_id}_norm.mp4").write_text("fake normalized video content")
        
    edit_dir = Path("artifacts/edit")
    edit_dir.mkdir(parents=True, exist_ok=True)
    (edit_dir / "out.mp4").write_text("fake final film")
    (edit_dir / "composition.json").write_text("{}")
    
    # Fake audio
    audio_dir = Path("artifacts/audio")
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "orchestral_score.wav").write_text("fake music")
    for i in [2, 3, 4, 6, 7]:
        (audio_dir / f"vo_{i:03d}.wav").write_text("fake vo")
    
    print("Mock artifacts generated.")

if __name__ == "__main__":
    main()
