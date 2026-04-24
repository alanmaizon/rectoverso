import sys
from pathlib import Path
from src.producer.veo import VeoRendererTool

tool = VeoRendererTool()
out_dir = Path("artifacts/renders")
out_dir.mkdir(parents=True, exist_ok=True)

print("Let's make some video with Veo...")
res = tool(shot_id="sh_004", payload={
    "prompt": "A train-carriage door slides open to reveal the Amazon canopy from 40 metres up, wingtip-height, a macaw crossing frame left-to-right. Smooth forward push out the door.",
    "output_dir": str(out_dir),
    "attempt_id": 1,
    "seed": 42
})
print("SUCCESS!")
print(res)
