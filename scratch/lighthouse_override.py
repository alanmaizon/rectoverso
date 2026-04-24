#!/usr/bin/env python3
import json
import shutil
from pathlib import Path

def main():
    print("Preparing Lighthouse Override Demo...")
    
    # We're going to generate a mini-manifest specifically for the lighthouse override
    manifest = {
        "manifest_version": "1.0",
        "project_id": "demo_lighthouse_override",
        "brief": {
            "title": "Lighthouse Override",
            "target_duration_s": 5,
            "artistic_style": "Moody cinematic"
        },
        "script": {"summary": "A lighthouse keeper pushes the door open."},
        "shots": [
            {
                "shot_id": "sh_001",
                "description": "Lighthouse keeper pushes the door open and steps inside.",
                "duration_s": 5.0,
                "continuity_refs": [],
                "prompt": {
                    "primary": "Lighthouse keeper pushes the door open and steps inside.",
                    "reference_subject_paths": []
                },
                "routing": {
                    "chosen_provider": "seedance",
                    "chosen_model": "fal-ai/seedance-2.0-i2v",
                    "estimated_cost_usd": 3.024,
                    "reasoning": "Specialty override request"
                },
                "attempts": [
                    {
                        "attempt_id": 1,
                        "prompt_revision": False,
                        "render_path": "artifacts/renders/lighthouse_sh_001.mp4",
                        "provider": "seedance",
                        "cost_usd": 0.0,
                        "latency_s": 1.0,
                        "outcome": "approved",
                        "judge_score": 0.91,
                        "judge_notes": "Perfect execution of the brief."
                    }
                ],
                "final": {
                    "render_path": "artifacts/renders/lighthouse_sh_001.mp4",
                    "normalized_path": "artifacts/renders/lighthouse_sh_001_norm.mp4"
                },
                "status": "approved",
                "audio_status": "ok",
                "history": []
            }
        ],
        "audio": {"status": "ok", "dialogue": []},
        "edit": {
            "status": "approved",
            "renderer": "hyperframes",
            "composition_path": "artifacts/edit/lighthouse_comp.json",
            "render_path": "artifacts/edit/lighthouse_out.mp4",
            "render_md5": "a78f586460429482b42ffb",
            "total_duration_s": 5.0
        },
        "budget": {"cap_usd": 150, "spent_usd": 3.024},
        "run_state": {"resumable": True}
    }
    
    # Save the manifest
    manifest_path = Path("site/data/manifest_lighthouse.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
        
    print(f"Generated {manifest_path}")

if __name__ == "__main__":
    main()
