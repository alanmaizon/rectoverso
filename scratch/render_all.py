import sys
import time
from pathlib import Path
from src.producer.veo import VeoRendererTool
from src.producer.audio import ElevenLabsAudioTool

veo_tool = VeoRendererTool()
audio_tool = ElevenLabsAudioTool()

base_out_dir = Path("artifacts/renders")
audio_dir = Path("artifacts/audio")
audio_dir.mkdir(parents=True, exist_ok=True)

# AI Prompt Smith Enhanced Prompts (Cinematic, Veo 3.1 style)
shots = [
    {
        "id": "sh_001", "tool": veo_tool,
        "payload": {
            "prompt": "CU, 85mm equivalent lens, f/2.0 shallow depth of field. A 7-year-old child's hand rests softly on a genuinely worn, textured brass door handle in a dim cottage hallway. The hand turns the brass handle downwards. Camera: very slow, smooth push-in dolly towards the hand. Lighting: side-lit by soft morning window light, 4000K, slightly desaturated. Palette: muted warm earth tones, deep shadow roll-off. Atmosphere: quiet, dust motes in the air, cinematic realism. Film: Kodak 500T 5219 emulation, fine grain. Mood: anticipation, grounded. No VFX, no magical sparkles, no fast motion, no modern elements.",
            "attempt_id": 1, "seed": 42
        }
    },
    {
        "id": "sh_002", "tool": veo_tool,
        "payload": {
            "prompt": "WS, 24mm equivalent lens, deep focus. An underwater coral reef, vibrant and alive, late afternoon sun shafting down through the water volume. A school of realistic parrotfish parts around an open, impossibly upright wooden cottage doorframe floating mid-water. Camera: slow, drifting forward tracking shot through the open wooden doorframe. Lighting: dappled underwater caustics, 6500K sunlight piercing from above, natural contrast. Palette: deep aquatic blues, vibrant coral accents, naturalism. Atmosphere: suspended gravity, majestic, clear water. Film: Arri Alexa LF natural LUT, sharp, high dynamic range. Mood: awe-inspiring, silent. No bubbles inside the doorframe, no fast cuts, no cartoonish fish.",
            "attempt_id": 1, "seed": 42
        }
    },
    {
        "id": "sh_003", "tool": veo_tool,
        "payload": {
            "prompt": "MLS, 35mm equivalent lens, f/4 natural depth of field. A battered metal service door in a hot Lagos alleyway swings open to reveal the Okavango Delta at flood stage during golden hour. Elephants are wading in the mirror-flat water, tall reeds parting. Camera: continuous, smooth forward dolly tracking straight through the doorway threshold from the alley into the expansive delta. Lighting: intense directional golden hour sun, 3200K, high contrast. Palette: hot rust and grey metal transitioning instantly to lush golden greens and water reflections. Atmosphere: heat shimmer in the foreground, vast coolness in the background, breathtaking scale. Film: Kodak Vision3 250D emulation, cinematic. Mood: expansive, awe. No portal VFX, no glowing edges, no cross-dissolve, clean practical threshold.",
            "attempt_id": 1, "seed": 42
        }
    },
    {
        "id": "sh_004", "tool": veo_tool,
        "payload": {
            "prompt": "EWS, 18mm equivalent lens, infinity focus. A heavy metal train-carriage door slides open left-to-right, revealing the vast Amazon rainforest canopy from 40 metres in the air, exactly at wingtip height. A vibrant red-and-blue macaw flies slowly across the frame from left to right. Camera: locked off inside the train for one second, then a smooth, dramatic forward push out the open door into the open air above the trees. Lighting: bright midday equatorial sun, 5500K, rich saturation. Palette: overwhelming, endless layers of vibrant canopy greens against a deep blue sky. Atmosphere: dizzying height, rushing wind, immense scale, hyper-realistic. Film: IMAX 70mm emulation, pristine clarity. Mood: breathtaking, flying, liberating. No visible train tracks below, no motion blur, no CGI look.",
            "attempt_id": 1, "seed": 42
        }
    },
    {
        "id": "sh_005", "tool": veo_tool,
        "payload": {
            "prompt": "WS, 50mm equivalent lens, deep focus. A traditional paper shoji door in a dim, wooden ryokan corridor slides open to reveal a frozen, snow-covered lake under a vivid green aurora borealis. Thick steam rises from a thermal vent in the ice. A solitary wild red fox walks across the ice, stops, turns its head, and stares directly into the camera lens. Camera: completely locked-off, static, observational. Lighting: moonlight and glowing overhead aurora, 8000K, extremely low saturation. Palette: monochromatic icy blues and blacks, pierced only by the bright red-orange fur of the fox. Atmosphere: freezing, absolute stillness, breath-mist in the air. Film: muted cinematic, Fujifilm Eterna 500T emulation. Mood: profound silence, connection, isolation. No camera movement, no fast fox movements, no bright artificial lights.",
            "attempt_id": 1, "seed": 42
        }
    },
    {
        "id": "sh_006", "tool": veo_tool,
        "payload": {
            "prompt": "CU, 85mm equivalent lens, f/1.8 razor-thin depth of field. A 7-year-old child stands in a doorway, seen in soft profile, looking directly into a normal, messy, everyday kitchen. The soft silhouette of a parent washing dishes is out of focus in the deep background. The child's expression is one of quiet, still recognition—not smiling, not disappointed, just realizing. Camera: very slow, almost imperceptible push-in on the child's face. Lighting: soft, diffused morning window light hitting the child's cheek, 4500K, naturalistic. Palette: warm, lived-in earth tones, mundane, domestic. Atmosphere: intimate, quiet, profoundly ordinary. Film: Arri Alexa Mini LF, soft highlight roll-off. Mood: grounding, realization, gentle. No dramatic lighting, no magical elements, no overt acting, pure naturalism.",
            "attempt_id": 1, "seed": 42
        }
    },
    {
        "id": "sh_007", "tool": veo_tool,
        "payload": {
            "prompt": "WS, 50mm equivalent lens, f/4 natural depth of field. A single, closed, beautifully textured wooden cottage door fills the center of the frame. An ungendered adult hand naturally reaches into the frame from the side and rests gently on the brass door handle. The hand remains completely still; it does not turn the handle. Camera: completely locked-off, static tripod shot. Lighting: soft ambient daylight, 5000K, even illumination. Palette: neutral, warm wood tones, honest color. Atmosphere: peaceful, resolved, final. Film: 35mm film emulation, subtle grain. Mood: closure, potential, stillness. No camera movement, no turning the handle, no camera shake.",
            "attempt_id": 1, "seed": 42
        }
    }
]

print("Starting bulk render of all AI-enhanced cinematic shots...\n")

for s in shots:
    shot_dir = base_out_dir / s["id"]
    shot_dir.mkdir(parents=True, exist_ok=True)
    s["payload"]["output_dir"] = str(shot_dir)

    print(f"--> Rendering {s['id']}...")
    try:
        res = s["tool"](shot_id=s["id"], payload=s["payload"])
        print(f"Success {s['id']}: {res['render_path']}")
    except Exception as e:
        print(f"FAILED {s['id']}: {e}")
    time.sleep(2)

print("\n--> Generating Audio (Voiceover)...")
try:
    res = audio_tool(shot_id="sh_006", payload={
        "mode": "tts",
        "text": "Oh. It was here too.",
        "voice_id": "EXAVITQu4vr4xnSDxMaL",
        "output_dir": str(audio_dir),
        "attempt_id": 1
    })
    print(f"Success Audio: {res['audio_path']}")
except Exception as e:
    print(f"FAILED Audio: {e}")

print("\nDone! Check artifacts/renders")
