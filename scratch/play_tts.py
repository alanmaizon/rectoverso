import sys
from pathlib import Path
from src.producer.audio import ElevenLabsAudioTool

tool = ElevenLabsAudioTool()
out_dir = Path("artifacts/audio")
out_dir.mkdir(parents=True, exist_ok=True)

print("Let's make some audio...")
res = tool(shot_id="sh_006", payload={
    "mode": "tts",
    "text": "Oh. It was here too.",
    "voice_id": "EXAVITQu4vr4xnSDxMaL",
    "output_dir": str(out_dir),
    "attempt_id": 1
})
print("SUCCESS!")
print(res)
