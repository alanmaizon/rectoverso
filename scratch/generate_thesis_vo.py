"""Generate the thesis-presentation voiceover via ElevenLabs.

Pairs with site/media/thesis/The Multi-Agent Pipeline.html — a 46s scripted
animation of the rectoverso pipeline. This script renders a single MP3 VO
that overlays the animation when stitched in post.

Usage:
    # 1. Pick an Irish male voice from the Voice Library:
    #    https://elevenlabs.io/app/voice-library  (search "Irish male")
    #    Copy its voice_id.
    # 2. Run:
    python scratch/generate_thesis_vo.py --voice-id <voice_id>

    # Optional flags:
    #   --speed 0.85         (default; lower = slower)
    #   --stability 0.75     (default; higher = steadier cadence)
    #   --out path/to.mp3    (default: site/media/thesis/thesis_vo.mp3)

Requires ELEVENLABS_API_KEY in env or .env.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.producer.audio import ElevenLabsAudioTool


# Three chunks across the 2-min submission video:
#   opening  — 0:00 → 0:48  (48s)
#   pipeline — 0:48 → 1:25  (37s)  ← the multi-agent thesis animation
#   closing  — 1:25 → 2:00  (35s)
#
# Slow Irish read at this voice/speed lands ~15.7 chars/sec.
# Targets: opening ≈ 750 chars, pipeline ≈ 580 chars, closing ≈ 550 chars.

OPENING_SCRIPT = (
    "It starts with a brief. "
    "A short film. Sixty seconds. "
    "Warm-neutral light, slow steady moves — no VFX, no transitions. "
    "Places most of us will never stand in, and the one we already do. "
    "The work that follows is not a single prompt. "
    "It is a small studio, working in turns. "
    "A Producer holds the camera. "
    "A Screenwriter, a Prompt Smith, a Router, a Renderer, a Shot Judge, "
    "an Audio agent, an Editor, a Creative Director — "
    "eight specialists, coordinated by Anthropic's Claude Opus four point seven. "
    "This is rectoverso. "
    "A multi-agent film pipeline, built for the Built-with-Opus four point seven hackathon. "
    "What you are about to see is how it thinks."
)

PIPELINE_SCRIPT = (
    "A human writes a brief. "
    "A logline, a duration, a few notes on tone. "
    "The pipeline never starts itself. "
    "The Producer is the only voice that issues work. "
    "Seven specialists wait — the Producer speaks; they answer. "
    "No agent talks to another. They write to the page. "
    "The Manifest is the only shared memory — coordination becomes archive. "
    "A render lands. The Judge reads it. "
    "A verdict is not advice — it is a hard constraint. "
    "The Creative Director can override, "
    "to keep the film, not just the shot, intact. "
    "The Editor reads the final rows. The film assembles. "
    "Magic Doors. "
    "Brief on one side. Film on the other."
)

CLOSING_SCRIPT = (
    "Eight shots. Six providers. One orchestral score. "
    "Twelve approved attempts, and four creative overrides — "
    "every one of them recorded as a row in the Manifest. "
    "Readable. Auditable. Replayable. "
    "The film is named Here. "
    "A quiet montage of places most of us will never stand in, "
    "and the one we already do, carried by a single voice. "
    "Produced by rectoverso. Anthropic Claude Opus four point seven. "
    "Kling. Wan. Veo. ElevenLabs. "
    "Brief on one side. Film on the other."
)

CHUNKS = {
    "opening":  {"text": OPENING_SCRIPT,  "target_s": 48.0},
    "pipeline": {"text": PIPELINE_SCRIPT, "target_s": 37.0},
    "closing":  {"text": CLOSING_SCRIPT,  "target_s": 35.0},
}

# Default for back-compat with earlier runs.
SCRIPT = PIPELINE_SCRIPT


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--voice-id", required=True,
                   help="ElevenLabs voice_id (paste from Voice Library)")
    p.add_argument("--chunk", choices=list(CHUNKS.keys()),
                   help="Named script chunk to render (opening/pipeline/closing). "
                        "Sets script, target duration, and default --out.")
    p.add_argument("--speed", type=float, default=1.0,
                   help="Speech rate, 0.7–1.2 (default 1.0; "
                        "atempo will fine-tune to --target-s afterwards)")
    p.add_argument("--target-s", type=float, default=None,
                   help="If set, atempo-stretch the result to this exact duration. "
                        "Overrides the chunk's default target.")
    p.add_argument("--stability", type=float, default=0.75,
                   help="Voice stability 0.0–1.0 (default 0.75, steady)")
    p.add_argument("--similarity", type=float, default=0.80,
                   help="Similarity boost 0.0–1.0 (default 0.80)")
    p.add_argument("--style", type=float, default=0.0,
                   help="Style exaggeration 0.0–1.0 (default 0.0, neutral)")
    p.add_argument("--model", default="eleven_multilingual_v2",
                   help="TTS model (default eleven_multilingual_v2 — "
                        "stable; v3 sometimes ignores speed).")
    p.add_argument("--out", default=None,
                   help="Output path (defaults to site/media/thesis/{chunk}_vo.mp3, "
                        "or thesis_vo.mp3 if no --chunk given)")
    p.add_argument("--seed", type=int, default=None,
                   help="Optional seed for reproducible takes")
    args = p.parse_args()

    if args.chunk:
        text = CHUNKS[args.chunk]["text"]
        target_s = args.target_s if args.target_s is not None else CHUNKS[args.chunk]["target_s"]
        out_default = f"site/media/thesis/{args.chunk}_vo.mp3"
    else:
        text = SCRIPT
        target_s = args.target_s
        out_default = "site/media/thesis/thesis_vo.mp3"
    if args.out is None:
        args.out = out_default

    out_path = (REPO / args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    voice_settings = {
        "stability": args.stability,
        "similarity_boost": args.similarity,
        "style": args.style,
        "use_speaker_boost": True,
        "speed": args.speed,
    }

    tool = ElevenLabsAudioTool()
    line_id = f"{args.chunk}_vo" if args.chunk else "thesis_vo"
    payload = {
        "mode": "tts",
        "text": text,
        "voice_id": args.voice_id,
        "line_id": line_id,
        "model_id": args.model,
        "voice_settings": voice_settings,
        "output_dir": str(out_path.parent),
        "attempt_id": 1,
        "output_format": "mp3_44100_192",
    }
    if args.seed is not None:
        payload["seed"] = args.seed

    print(f"[vo] dispatching {len(text)} chars at speed={args.speed}"
          f"{' (target ' + str(target_s) + 's)' if target_s else ''}…")
    res = tool(shot_id="thesis", payload=payload)

    if res.get("status") != "ok":
        print(f"[vo] FAILED: {res.get('failure_stage')} — "
              f"{res.get('stderr_tail')}", file=sys.stderr)
        return 1

    written = Path(res["audio_path"])
    if written.resolve() != out_path:
        written.replace(out_path)

    raw_duration = res["duration_s"]
    print(f"[vo] raw       → {out_path} ({raw_duration:.2f}s, "
          f"{res['quota_cost']} credits, md5 {res['audio_md5'][:8]})")

    if target_s and abs(raw_duration - target_s) > 0.05:
        import subprocess
        atempo = raw_duration / target_s
        # atempo accepts 0.5–2.0 in a single pass; for 0.7–1.2 we're well in range.
        if not (0.5 <= atempo <= 2.0):
            print(f"[vo] atempo {atempo:.4f} out of [0.5, 2.0] — skipping fit",
                  file=sys.stderr)
            return 0
        fitted = out_path.with_name(out_path.stem + "_fitted.mp3")
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(out_path),
            "-filter:a", f"atempo={atempo:.4f}",
            str(fitted),
        ], check=True)
        # Replace the unfitted file with the fitted one for clean handoff.
        fitted.replace(out_path)
        print(f"[vo] fitted    → {out_path} (atempo={atempo:.4f}, target {target_s}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
