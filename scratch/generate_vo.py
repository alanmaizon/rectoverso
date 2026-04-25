import sys
import json
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.producer.audio import ElevenLabsAudioTool

def main():
    tool = ElevenLabsAudioTool()
    out_dir = Path("artifacts/audio")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    manifest_path = Path("state/manifest.json")
    if not manifest_path.exists():
        print("Manifest not found at state/manifest.json")
        return 1
        
    with open(manifest_path, "r") as f:
        manifest = json.load(f)
        
    dialogue = manifest.get("audio", {}).get("dialogue", [])
    
    if not dialogue:
        print("No dialogue found in manifest.")
        return 0
        
    print(f"Generating {len(dialogue)} voiceovers...")
    
    for i, line in enumerate(dialogue):
        # Extract vo id from audio_path like "artifacts/audio/sh_002_vo_002_v1.mp3"
        # Or just use the original number
        line_id = f"vo_00{i+2}" # just to match roughly the old wav names if needed
        shot_id = line["timing"]["shot_id"]
        
        print(f"Generating for {shot_id}: '{line['text']}'")
        res = tool(shot_id=shot_id, payload={
            "mode": "tts",
            "text": line["text"],
            "voice_id": line["voice_id"],
            "line_id": line_id,
            "output_dir": str(out_dir),
            "attempt_id": 1
        })
        print(f"SUCCESS: {res['audio_path']}")
        
    print("Done!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
