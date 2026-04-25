#!/usr/bin/env python3
"""
Dragon and Tortoise Sketch Pipeline.
Uses FLUX for the starting frame, then iteratively uses Kling 2.1 Pro (I2V) to animate.
Style: Hand-drawn rough pencil sketch, as requested.
Reads scenes dynamically from storybook-part2.json.
"""
import base64
import json
import mimetypes
import subprocess
import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Add src to path to use our existing fal keys resolver
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.producer._fal import resolve_fal_keys

DEFAULT_STYLE = "Hand-drawn rough pencil sketch animation, messy lines, expressive charcoal and colored pencil, minimalist background, unfinished sketch aesthetic, flat 2D depth, traditional animation storyboard style."
DEFAULT_OUT_DIR = Path("artifacts/dragon_tortoise")

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


def _build_character_design(story: dict) -> str:
    chars = story.get("characters") or {}
    if not isinstance(chars, dict) or not chars:
        return "Two friendly storybook characters in a consistent hand-drawn style."
    entries: list[str] = []
    for name, cfg in chars.items():
        visual = ""
        if isinstance(cfg, dict):
            visual = str(cfg.get("visual") or "").strip()
        label = str(name).replace("_", " ")
        entries.append(f"{label}: {visual}" if visual else label)
    return "Characters: " + "; ".join(entries) + "."


def _scene_parts(scene: dict) -> list[dict]:
    raw_parts = scene.get("parts")
    if isinstance(raw_parts, list) and raw_parts:
        parts: list[dict] = []
        for idx, p in enumerate(raw_parts, start=1):
            if not isinstance(p, dict):
                continue
            parts.append(
                {
                    "part": int(p.get("part") or idx),
                    "camera": str(p.get("camera") or ""),
                    "description": str(p.get("description") or ""),
                    "animation": str(p.get("animation") or ""),
                }
            )
        if parts:
            return parts

    return [
        {
            "part": 1,
            "camera": str(scene.get("camera") or ""),
            "description": str(scene.get("description") or ""),
            "animation": str(scene.get("animation") or ""),
        }
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a storybook JSON via FLUX + Kling.")
    parser.add_argument(
        "--story-file",
        default="storybook-part2.json",
        help="Path to storybook JSON file (default: storybook-part2.json)",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help="Output directory for rendered clips",
    )
    return parser.parse_args()

def main():
    args = _parse_args()
    primary, backup = resolve_fal_keys()
    if not primary:
        print("Error: FAL_KEY environment variable not set.")
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Read the storybook configuration
    with open(args.story_file, "r") as f:
        story = json.load(f)

    scenes = story["scenes"]
    style = str(story.get("style") or DEFAULT_STYLE)
    character_design = _build_character_design(story)

    current_image_path = out_dir / "scene_00_initial.png"
    
    # Step 1: Generate initial frame via FLUX if it doesn't exist
    if not current_image_path.exists():
        print(f"Initialization: Generating initial establishing frame via FLUX...")
        first_scene = scenes[0]
        first_part = _scene_parts(first_scene)[0]
        flux_prompt = (
            f"{character_design} {first_part['camera']}. {first_part['description']} "
            f"{first_part['animation']}. {style} {first_scene.get('artistic_direction', '')}"
        )
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
    for scene in scenes:
        scene_num = scene["scene"]
        artistic_direction = scene.get("artistic_direction", "")
        
        print(f"\nScene {scene_num}: {scene.get('narration', '')}")
        
        for part_data in _scene_parts(scene):
            part = part_data["part"]
            scene_prompt = f"{part_data['camera']}. {part_data['description']} {part_data['animation']}."

            video_part_path = out_dir / f"scene_{scene_num:02d}_part{part}.mp4"
            next_part_image_path = out_dir / f"scene_{scene_num:02d}_part{part}_last_frame.png"
            
            if not video_part_path.exists():
                print(f"  -> Animating via Kling 2.1 Pro (Part {part}/3)...")
                kling_payload = {
                    "prompt": f"{character_design} {scene_prompt} {style} {artistic_direction}",
                    "image_url": get_data_uri(current_image_path),
                    "duration": "10",
                    "aspect_ratio": "16:9"
                }
                try:
                    vid_bytes = run_fal(
                        "fal-ai/kling-video/v2.1/pro/image-to-video",
                        kling_payload,
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
            
    print("\n=== Dragon & Tortoise Pipeline Complete ===")
    print(f"Check {out_dir} for your continuous animation clips!")
    
if __name__ == "__main__":
    sys.exit(main())
