#!/usr/bin/env python3
"""
Children's Book Deterministic Pipeline.
Uses FLUX for the starting frame, then iteratively uses MiniMax (I2V) to animate.
Extracts the last frame of each shot to feed into the next shot, creating a continuous sequence.
"""
import base64
import json
import mimetypes
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Add src to path to use our existing fal keys resolver
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.producer._fal import resolve_fal_keys

STYLE = "Classic vintage children's storybook illustration, hand-drawn 2D animation style, traditional cel animation, soft watercolor and ink, nostalgic, gentle pencil outlines, flat pastel colors, magical but flat 2D depth."
CHARACTER_DESIGN = "The main character is 'Ferret Man', a cute hand-drawn little ferret superhero with bright expressive dotted eyes, wearing a simple purple cape and a tiny black domino mask."

# The Story Sequence - Enhanced with artistic direction and camera moves for MiniMax
SCENES = [
    "Slow, gentle pan. A cute little hand-drawn ferret superhero standing proudly on a simple cobblestone street. The town behind him is painted in soft watercolors. Gentle morning light.",
    "Still frame, delicate movements. The ferret superhero seated at a tiny wooden table inside a cozy, sketched burrow-style home, holding a comically oversized walnut. A young rabbit child, drawn with soft pencil lines, waves frantically outside.",
    "Slow vertical pan up. The ferret superhero gripping the bark of a massive, watercolor oak tree, climbing upwards with his cape fluttering in the breeze. A small rabbit child watches hopefully from the sketched forest floor below.",
    "Gentle push-in. The ferret superhero perched on a high branch, face-to-face with a deeply unimpressed, ruffled owl drawn with ink and wash. A red diamond kite is hopelessly tangled around the branch. Soft, flat lighting.",
    "Intimate, slow push-in. The ferret superhero peering into a small, mysterious crack in the tree trunk. A warm, flat amber glow comes from inside the crack, illuminating his excited face.",
    "Bouncy, hand-drawn motion. The ferret superhero bursting out of the top of the branch like a cork, his purple cape streaming. The owl blinks in startled surprise. Bright, clear watercolor sky.",
    "Wide, triumphant framing. The ferret superhero standing victoriously on the branch with his arms spread wide as a bright red kite soars freely into a gorgeous, fluffy painted blue sky.",
    "Warm, simple low-angle shot. The little sketched rabbit hugging the ferret superhero tightly around his middle. The ferret looks flustered but pleased. Soft pastel colors.",
    "Wide, bustling tracking shot. Dozens of woodland animals, drawn in classic 2D animation style, gathered in a colorful town square, pointing excitedly and cheering as the ferret superhero walks down the street.",
    "Slow pan across a painted feast. A long wooden table overflowing with simple, colorful snacks, berries, and cakes. The ferret superhero stands at the end, paws clasped to his cheeks in pure joy.",
    "Cozy, flatly lit medium shot. The ferret superhero seated at the feast table surrounded by happy, hand-drawn townsfolk, passing plates and laughing together. A warm, nostalgic feeling.",
    "Slow, peaceful pull-back. Night time. The ferret superhero silhouetted against a massive, painted full moon on a rooftop, his cape billowing softly. A small walnut sits beside him. Deep blue watercolor sky."
]

OUT_DIR = Path("artifacts/storybook")

def get_data_uri(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(path.name)
    mime_type = mime_type or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime_type};base64,{b64}"

def _is_auth_or_quota_error(code: int, body: str) -> bool:
    if code in (401, 403, 429):
        return True
    lowered = body.lower()
    return "exhausted balance" in lowered or "user is locked" in lowered


def run_fal(
    model: str,
    payload: dict,
    api_key: str,
    backup_api_key: str | None = None,
    max_retries: int = 3,
) -> bytes:
    url = f"https://queue.fal.run/{model}"
    active_key = api_key
    switched_to_backup = False
    
    # Retry loop for submission
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"Authorization": f"Key {active_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                submit = json.load(r)
            break # Success, exit retry loop
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ""
            print(f"    HTTP Error submitting to {model} (Attempt {attempt+1}/{max_retries}): {e.code} - {error_body}")
            if (
                backup_api_key
                and not switched_to_backup
                and _is_auth_or_quota_error(e.code, error_body)
            ):
                active_key = backup_api_key
                switched_to_backup = True
                print("    Switching to backup fal key and retrying submit...")
                continue
            if attempt == max_retries - 1:
                raise RuntimeError(f"fal API error after {max_retries} attempts: {e.code} {e.reason} - {error_body}") from e
            time.sleep(5)
        except Exception as e:
            print(f"    Error submitting to {model} (Attempt {attempt+1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(5)
            
    print(f"    submitted to {model}: request_id={submit['request_id'][:8]}…")
    
    deadline = time.time() + 900
    while time.time() < deadline:
        time.sleep(5)
        req = urllib.request.Request(submit["status_url"], headers={"Authorization": f"Key {active_key}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status = json.load(r)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ""
            if (
                backup_api_key
                and not switched_to_backup
                and _is_auth_or_quota_error(e.code, error_body)
            ):
                active_key = backup_api_key
                switched_to_backup = True
                print("    Switching to backup fal key and retrying status poll...")
                continue
            print(f"    Warning: status poll failed: {e.code} - {error_body}. Retrying...")
            continue
        except Exception as e:
            print(f"    Warning: status poll failed: {e}. Retrying...")
            continue
            
        s = status.get("status")
        if s == "COMPLETED":
            break
        if s in ("IN_QUEUE", "IN_PROGRESS"):
            continue
        raise RuntimeError(f"unexpected status: {status}")
        
    # Retry loop for downloading result
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(submit["response_url"], headers={"Authorization": f"Key {active_key}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                result = json.load(r)
            break
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ""
            print(f"    HTTP Error downloading result (Attempt {attempt+1}/{max_retries}): {e.code} - {error_body}")
            if (
                backup_api_key
                and not switched_to_backup
                and _is_auth_or_quota_error(e.code, error_body)
            ):
                active_key = backup_api_key
                switched_to_backup = True
                print("    Switching to backup fal key and retrying result download...")
                continue
            if attempt == max_retries - 1:
                raise RuntimeError(f"fal API error downloading result after {max_retries} attempts: {e.code} - {error_body}") from e
            time.sleep(5)
        except Exception as e:
            print(f"    Error downloading result (Attempt {attempt+1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(5)
        
    # Handle both image (FLUX) and video (MiniMax) responses
    if "video" in result:
        media_url = result["video"]["url"]
    elif "images" in result:
        media_url = result["images"][0]["url"]
    else:
        raise ValueError(f"Unknown response format: {result}")
        
    print(f"    completed! downloading from {media_url[:40]}...")
    with urllib.request.urlopen(media_url, timeout=120) as r:
        return r.read()

def extract_last_frame(video_path: Path, output_image_path: Path):
    print(f"    Extracting last frame from {video_path.name} via ffmpeg...")
    # Get duration
    cmd_duration = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)
    ]
    res = subprocess.run(cmd_duration, capture_output=True, text=True, check=True)
    duration = float(res.stdout.strip())
    
    # Extract frame near the very end (duration - 0.2s to be safe from empty frames)
    target_time = max(0, duration - 0.2)
    cmd_extract = [
        "ffmpeg", "-y", "-ss", str(target_time), "-i", str(video_path),
        "-vframes", "1", "-q:v", "2", str(output_image_path)
    ]
    subprocess.run(cmd_extract, capture_output=True, check=True)
    print(f"    Saved last frame to {output_image_path.name}")

def main():
    primary, backup = resolve_fal_keys()
    if not primary:
        print("Error: FAL_KEY environment variable not set.")
        return 1
        
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    
    current_image_path = OUT_DIR / "scene_00_initial.png"
    
    # Step 1: Generate initial frame via FLUX if it doesn't exist
    if not current_image_path.exists():
        print(f"Initialization: Generating initial establishing frame via FLUX...")
        flux_prompt = f"{CHARACTER_DESIGN} {SCENES[0]} {STYLE}"
        img_bytes = run_fal(
            "fal-ai/flux-pro/v1.1-ultra",
            {"prompt": flux_prompt, "aspect_ratio": "16:9"},
            primary,
            backup,
        )
        current_image_path.write_bytes(img_bytes)
    else:
        print(f"Initialization: Using existing initial frame {current_image_path.name}")
        
    # Step 2: Animate sequentially
    for i, scene_prompt in enumerate(SCENES):
        scene_num = i + 1
        
        print(f"\nScene {scene_num}: {scene_prompt}")
        
        # Animate with MiniMax (Note: Minimax produces ~5s video. To get 15s per scene, we generate 3 clips per scene, chaining them!)
        
        # We will loop 3 times per scene to get ~15 seconds of video
        for part in range(1, 4):
            video_part_path = OUT_DIR / f"scene_{scene_num:02d}_part{part}.mp4"
            next_part_image_path = OUT_DIR / f"scene_{scene_num:02d}_part{part}_last_frame.png"
            
            if not video_part_path.exists():
                print(f"  -> Animating via MiniMax Video (Part {part}/3)...")
                minimax_payload = {
                    "prompt": f"{CHARACTER_DESIGN} {scene_prompt} {STYLE}",
                    "image_url": get_data_uri(current_image_path)
                }
                try:
                    vid_bytes = run_fal(
                        "fal-ai/minimax/video-01/image-to-video",
                        minimax_payload,
                        primary,
                        backup,
                    )
                    video_part_path.write_bytes(vid_bytes)
                except Exception as e:
                    print(f"  -> Fatal error generating scene {scene_num} part {part}: {e}")
                    return 1
            else:
                print(f"  -> Video {video_part_path.name} already exists, skipping generation.")
                
            # Extract last frame to feed either the next part of this scene, or the next scene
            if not next_part_image_path.exists():
                extract_last_frame(video_part_path, next_part_image_path)
                
            # Move the reference forward
            current_image_path = next_part_image_path
        
    print("\n=== Storybook Pipeline Complete ===")
    print(f"Check {OUT_DIR} for your continuous animation clips!")
    
if __name__ == "__main__":
    sys.exit(main())
