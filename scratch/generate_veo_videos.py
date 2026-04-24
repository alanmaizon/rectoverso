#!/usr/bin/env python3
"""Generate videos via Vertex AI Veo 3.1 Fast from reference images.

Reads artifacts/refs/sh_*_v1.png and calls veo-3.1-fast-generate-001.
Saves outputs to artifacts/renders/sh_*.mp4.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Need google-auth for GCP ADC
try:
    import google.auth
    from google.auth.transport.requests import Request
except ImportError:
    print("error: google-auth and requests are required. Run: pip install google-auth requests", file=sys.stderr)
    sys.exit(1)

FAL_MODEL = "veo-3.1-fast-generate-001"
LOCATION = "us-central1"
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "rectoverso-demo") # fallback project

# Veo endpoint
VEO_BASE = f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{FAL_MODEL}"

REFS_DIR = Path("artifacts/refs")
OUT_DIR = Path("artifacts/renders")


def get_gcp_token_and_project() -> tuple[str, str]:
    """Get ADC bearer token and project."""
    credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(Request())
    return credentials.token, project


def encode_image_as_base64(path: str) -> str:
    p = Path(path)
    b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
    return b64


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


def submit_and_wait(prompt: str, image_path: Path, token: str, project_id: str) -> bytes:
    """Submit Veo job, poll to completion, download the video bytes."""
    
    b64_image = encode_image_as_base64(str(image_path))
    mime_type, _ = mimetypes.guess_type(image_path.name)
    mime_type = mime_type or "image/png"
    
    veo_base = f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{LOCATION}/publishers/google/models/{FAL_MODEL}"
    
    # Veo payload format
    payload = {
        "instances": [
            {
                "prompt": prompt,
                "negativePrompt": "fast motion, people, humans, text, watermark, cartoon",
                "image": {
                    "bytesBase64Encoded": b64_image,
                    "mimeType": mime_type
                }
            }
        ],
        "parameters": {
            "generateAudio": True, # Enabled to generate native audio/voiceover
            "personGeneration": "disallow",
            "duration": 8, # Veo 3.x supports 4, 6, 8
        }
    }
    
    body = json.dumps(payload).encode()
    
    req = urllib.request.Request(
        f"{veo_base}:predictLongRunning",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        submit = json.load(r)
        
    operation_name = submit["name"]
    print(f"    submitted: operation={operation_name.split('/')[-1]}…")
    
    deadline = time.time() + 600  # 10 minutes max
    while time.time() < deadline:
        time.sleep(10)
        
        # Veo polling
        poll_body = json.dumps({"operationName": operation_name}).encode()
        req = urllib.request.Request(
            f"{veo_base}:fetchPredictOperation",
            data=poll_body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        
        with urllib.request.urlopen(req, timeout=10) as r:
            status = json.load(r)
            
        if "done" in status and status["done"]:
            if "error" in status:
                raise RuntimeError(f"Veo error: {status['error']}")
                
            response = status.get("response", {})
            videos = response.get("videos", [])
            
            # Check content policy filter
            filtered_count = response.get("raiMediaFilteredCount", 0)
            if filtered_count > 0 or not videos:
                 raise RuntimeError(f"Veo content policy blocked generation or no video returned")
                 
            video_b64 = videos[0].get("bytesBase64Encoded")
            if video_b64:
                 return base64.b64decode(video_b64)
                 
            raise RuntimeError("Veo completed but no base64 video was returned")
            
    else:
        raise TimeoutError("Veo didn't complete within 10 min")


def main() -> int:
    try:
        token, project_id = get_gcp_token_and_project()
        if not project_id:
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project_id:
             print("error: Could not determine GCP project. Set GOOGLE_CLOUD_PROJECT.", file=sys.stderr)
             return 2
    except Exception as e:
        print(f"error: Could not get GCP ADC token. Are you authenticated? Run: gcloud auth application-default login\nDetails: {e}", file=sys.stderr)
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
        print(f"  {shot_id}: generating video via {FAL_MODEL} (8s duration)")
        print(f"    prompt: {prompt}")
        try:
            mp4 = submit_and_wait(prompt, ref_path, token, project_id)
            out_path.write_bytes(mp4)
            print(f"    -> {out_path} ({len(mp4)} bytes)")
        except Exception as e:
            print(f"    failed: {e}")
            continue

    return 0


if __name__ == "__main__":
    sys.exit(main())
