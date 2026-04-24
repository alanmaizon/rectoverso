#!/usr/bin/env python3
"""Generate 7 videos via fal.ai Kling 2.1 Pro from reference images.

Reads artifacts/refs/sh_*_v1.png and calls fal-ai/kling-video/v2.1/pro/image-to-video.
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

FAL_MODEL = "fal-ai/kling-video/v2.1/pro/image-to-video"
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
    # We want naturalistic, restrained camera moves for the videos,
    # avoiding the descriptive still-frame text.
    by_shot = {
        "sh_001": "Slow, steady push-in on the quiet hallway. Dust motes drifting in the morning light.",
        "sh_002": "Slow, graceful drift forward through the coral reef. Parrotfish swimming naturally.",
        "sh_003": "Slow, sweeping aerial drift over the flooded delta. Elephants wading peacefully.",
        "sh_004": "Smooth wingtip-height forward flight over the Amazon canopy. A macaw flies across.",
        "sh_005": "Still, held wide shot. Aurora shimmers subtly overhead. Steam rises slowly. The fox holds perfectly still, looking at camera.",
        "sh_006": "Slow, sweeping panoramic drone shot over the blue glacier. Shadows shifting subtly in the fading light.",
        "sh_007": "Wide exterior shot. Completely still except for the thin plume of woodsmoke rising into the morning air.",
    }
    return by_shot.get(shot_id, "")


def submit_and_wait(prompt: str, image_path: Path, api_key: str) -> bytes:
    """Submit I2V job, poll to completion, download the video bytes."""
    data_uri = encode_image_as_data_uri(str(image_path))
    body = json.dumps({
        "prompt": prompt,
        "image_url": data_uri,
        "duration": "5",
        "aspect_ratio": "16:9"
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
    deadline = time.time() + 600  # 10 minutes for video
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
        raise TimeoutError("fal I2V didn't complete within 10 min")

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
        print(f"  {shot_id}: generating video via {FAL_MODEL}")
        print(f"    prompt: {prompt}")
        try:
            mp4 = submit_and_wait(prompt, ref_path, primary)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            if backup:
                print(f"    primary key failed ({e}); retrying with backup")
                mp4 = submit_and_wait(prompt, ref_path, backup)
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
