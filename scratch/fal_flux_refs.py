#!/usr/bin/env python3
"""Generate 7 reference images via fal.ai FLUX Pro.

Each shot gets a still-frame prompt derived from its video prompt. Output
saves to artifacts/refs/{shot_id}_v1.png and updates both the Kling and
Seedance manifests' shot.prompt.reference_subject_paths.

Why fal FLUX Pro: higher photorealism than Qwen/Gemini for documentary-
realism refs; uses the fal credits the operator has budgeted.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, ".")
from src.producer._common import resolve_env_key  # noqa: E402
from src.producer._fal import resolve_fal_keys  # noqa: E402
from src.producer import validate_manifest, save_manifest_atomic  # noqa: E402


FAL_MODEL = "fal-ai/flux-pro/v1.1-ultra"
FAL_BASE = f"https://queue.fal.run/{FAL_MODEL}"
OUT_DIR = Path("artifacts/refs")


def still_frame_prompt(video_prompt: str, shot_id: str) -> str:
    """Rewrite a motion-heavy video prompt into a single still-frame prompt.

    The video prompts mention camera motion ("pushes in", "drifts forward")
    which FLUX doesn't understand. Strip the verbs; describe the decisive
    frame. Pre-authored per shot for quality.
    """
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
    return by_shot.get(shot_id, video_prompt)


def submit_and_wait(prompt: str, api_key: str) -> bytes:
    """Submit T2I job, poll to completion, download the image bytes."""
    body = json.dumps({
        "prompt": prompt,
        "aspect_ratio": "16:9",
        "num_images": 1,
        "enable_safety_checker": True,
        "output_format": "png",
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
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(2)
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
        raise TimeoutError("fal T2I didn't complete within 2 min")

    req = urllib.request.Request(response_url, headers={"Authorization": f"Key {api_key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        result = json.load(r)
    img_url = result["images"][0]["url"]
    with urllib.request.urlopen(img_url, timeout=60) as r:
        return r.read()


def main() -> int:
    primary, backup = resolve_fal_keys()
    if not primary:
        print("error: FAL_KEY not resolved", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load one manifest to get the shot list.
    src_manifest_path = Path("state/manifest_kling.json")
    manifest = json.loads(src_manifest_path.read_text())

    generated: dict[str, str] = {}
    for shot in manifest["shots"]:
        shot_id = shot["shot_id"]
        out_path = OUT_DIR / f"{shot_id}_v1.png"
        if out_path.exists() and out_path.stat().st_size > 10000:
            print(f"  {shot_id}: already have {out_path} ({out_path.stat().st_size} bytes); skipping")
            generated[shot_id] = str(out_path)
            continue

        prompt = still_frame_prompt(shot["prompt"]["primary"], shot_id)
        print(f"  {shot_id}: generating via {FAL_MODEL}")
        try:
            png = submit_and_wait(prompt, primary)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            if backup:
                print(f"    primary key failed ({e}); retrying with backup")
                png = submit_and_wait(prompt, backup)
            else:
                raise
        out_path.write_bytes(png)
        print(f"    -> {out_path} ({len(png)} bytes)")
        generated[shot_id] = str(out_path)

    # Update Kling + Seedance manifests with reference paths.
    for manifest_path in ("state/manifest_kling.json", "state/manifest_seedance.json"):
        m = json.loads(Path(manifest_path).read_text())
        for shot in m["shots"]:
            ref = generated.get(shot["shot_id"])
            if ref:
                shot["prompt"]["reference_subject_paths"] = [ref]
        validate_manifest(m)
        save_manifest_atomic(Path(manifest_path), m, last_event_id=m["run_state"]["last_event_id"])
        print(f"  updated {manifest_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
