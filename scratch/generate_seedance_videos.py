#!/usr/bin/env python3
"""Generate videos via fal.ai ByteDance Seedance 2.0 from reference images.

Reads artifacts/refs/sh_*_v1.png and calls bytedance/seedance-2.0/image-to-video.
Saves outputs to artifacts/renders/sh_*.mp4.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, ".")
from src.producer._fal import resolve_fal_keys

FAL_MODEL = "bytedance/seedance-2.0/image-to-video"
FAL_BASE = f"https://queue.fal.run/{FAL_MODEL}"
REFS_DIR = Path("artifacts/refs")
OUT_DIR = Path("artifacts/renders")


def encode_image_as_data_uri(path: str) -> str:
    p = Path(path)
    mime_type, _ = mimetypes.guess_type(p.name)
    mime_type = mime_type or "application/octet-stream"
    b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
    return f"data:{mime_type};base64,{b64}"


def video_prompts(shot_id: str) -> str:
    # Using the rich descriptive prompts used for the images
    by_shot = {
        "sh_001": (
            "Interior photograph of a narrow hallway in a small Irish cottage at dawn. "
            "Warm soft morning light spilling through a single window at the far end. "
            "Dust motes visible in the shaft of light. Worn wooden floorboards, faded wallpaper, "
            "a framed photo on the wall. Warm-neutral honest color, 35mm film aesthetic, "
            "documentary photography. No people, no animals."
        ),
        "sh_002": (
            "Underwater wide-angle photograph inside the Great Barrier Reef, mid-water depth, "
            "late afternoon. Sun shafts cutting down through clear blue water. Colorful healthy "
            "coral reef in the mid-ground; a school of tropical parrotfish suspended in the "
            "foreground. Visible particulate and believable light refraction. BBC Blue Planet "
            "documentary aesthetic. No divers, no equipment, no text."
        ),
        "sh_003": (
            "Aerial photograph from 30 metres up over the Okavango Delta at golden hour. "
            "Mirror-flat shallow water catching warm orange sunset; long parallel lines of reeds "
            "cutting the surface. Three elephants wading in mid-frame, water dragging behind them "
            "with doubled reflections on the flat surface. Distant birds on the horizon. "
            "Warm-neutral grade, cinema-drone aesthetic. No people, no aircraft visible."
        ),
        "sh_004": (
            "Aerial wingtip-height photograph 40 metres above the Amazon rainforest canopy. "
            "Uninterrupted green canopy stretching to a misty horizon. A single macaw with red "
            "and blue plumage mid-flight against the canopy. Late-morning diffuse light, humidity "
            "haze rising. BBC Planet Earth nature-documentary aesthetic. No people, no aircraft."
        ),
        "sh_005": (
            "Wide landscape photograph of a frozen lake at night in Hokkaido, Japan, deep winter. "
            "Snow-dusted ice in the foreground; dark fir trees at the far shore; green and violet "
            "aurora borealis overhead with subtle reflection on the ice. A plume of white steam "
            "rising from a thermal vent at the shoreline. A single red fox standing on the ice "
            "mid-frame, head turned to look directly at the camera. Cold blue-silver palette, "
            "long-exposure look. No people."
        ),
        "sh_006": (
            "Wide panoramic photograph of a massive blue glacier in Iceland at dusk. "
            "Deep crevasses, ancient ice stretching to the horizon. "
            "Cold blue and silver palette, documentary realism, large-format photography. "
            "No people, no animals, pure nature."
        ),
        "sh_007": (
            "Wide exterior photograph of a small whitewashed Irish cottage at dawn, seen from "
            "across a dewy field. Soft golden morning light on the front of the cottage; a thin "
            "plume of woodsmoke rising straight up from the stone chimney into still air. Mossy "
            "stone walls in the foreground, distant green hills. Warm-neutral palette, golden "
            "hour, large-format camera aesthetic, documentary feel. No people, no animals."
        ),
    }
    return by_shot.get(shot_id, "")


def submit_and_wait(prompt: str, image_path: Path, api_key: str, duration: int) -> bytes:
    """Submit I2V job to Seedance, poll to completion, download the video bytes."""
    data_uri = encode_image_as_data_uri(str(image_path))
    
    # Seedance requires duration as string. Max is 15. No negative prompt.
    body = json.dumps({
        "prompt": prompt,
        "image_url": data_uri,
        "duration": str(duration),
        "generate_audio": False  # Saves tokens since we use ElevenLabs
    }).encode()
    
    req = urllib.request.Request(
        FAL_BASE,
        data=body,
        headers={"Authorization": f"Key {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        submit = json.load(r)
    request_id = submit["request_id"]
    status_url = submit["status_url"]
    response_url = submit["response_url"]

    print(f"    submitted: request_id={request_id[:8]}…")
    deadline = time.time() + 900  # 15 minutes max for longer generations
    while time.time() < deadline:
        time.sleep(5)
        req = urllib.request.Request(status_url, headers={"Authorization": f"Key {api_key}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            status = json.load(r)
        s = status.get("status")
        if s == "COMPLETED":
            break
        if s in ("IN_QUEUE", "IN_PROGRESS"):
            continue
        raise RuntimeError(f"unexpected status: {status}")
    else:
        raise TimeoutError("Seedance I2V didn't complete within 15 min")

    req = urllib.request.Request(response_url, headers={"Authorization": f"Key {api_key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        result = json.load(r)
        
    video_url = result["video"]["url"]
    print(f"    completed! downloading from {video_url[:30]}...")
    with urllib.request.urlopen(video_url, timeout=120) as r:
        return r.read()


def main() -> int:
    primary, backup = resolve_fal_keys()
    if not primary:
        print("error: FAL_KEY not resolved", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Durations from the brief for the 7 shots
    shot_durations = {
        "sh_001": 6,
        "sh_002": 10,
        "sh_003": 10,
        "sh_004": 9,
        "sh_005": 9,
        "sh_006": 10,
        "sh_007": 6,
    }

    shots = [f"sh_{i:03d}" for i in range(1, 8)]

    for shot_id in shots:
        ref_path = REFS_DIR / f"{shot_id}_v1.png"
        if not ref_path.exists():
            print(f"  {shot_id}: skipping, reference image not found at {ref_path}")
            continue
            
        out_path = OUT_DIR / f"{shot_id}.mp4"
        if out_path.exists() and out_path.stat().st_size > 100000:
            print(f"  {shot_id}: already have {out_path} ({out_path.stat().st_size} bytes); skipping")
            continue

        prompt = video_prompts(shot_id)
        duration = shot_durations.get(shot_id, 10) # default 10s if not mapped
        
        print(f"  {shot_id}: generating {duration}s video via {FAL_MODEL}")
        print(f"    prompt: {prompt}")
        try:
            mp4 = submit_and_wait(prompt, ref_path, primary, duration)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            if backup:
                print(f"    primary key failed ({e}); retrying with backup")
                mp4 = submit_and_wait(prompt, ref_path, backup, duration)
            else:
                print(f"    failed: {e}")
                continue
        except Exception as e:
            print(f"    failed: {e}")
            continue
            
        out_path.write_bytes(mp4)
        print(f"    -> {out_path} ({len(mp4)} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
