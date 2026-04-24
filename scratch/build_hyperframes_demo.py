#!/usr/bin/env python3
"""Build a presentation composition using Hyperframes.

Creates a composition.json utilizing the Claude Code orange logo for presentation.
Generates an HTML file utilizing the hyperframes web components.
"""
from pathlib import Path
import json
import shutil
import base64
import sys

def encode_image(img_path):
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/svg+xml;base64,{b64}"

def main():
    out_dir = Path("site/media/presentation")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # We'll create a simple SVG logo since we don't have the actual logo file
    logo_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <rect width="100" height="100" rx="20" fill="#FF5E00"/>
        <path d="M30 40 L45 55 L30 70 M60 40 L75 55 L60 70" stroke="white" stroke-width="8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="45" cy="55" r="4" fill="white"/>
    </svg>"""
    
    (out_dir / "logo.svg").write_text(logo_svg)
    logo_uri = encode_image(out_dir / "logo.svg")

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>RectoVerso Presentation</title>
    <style>
        body {{ margin: 0; background: #000; color: white; font-family: -apple-system, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; }}
        .presentation-container {{ position: relative; width: 1920px; height: 1080px; max-width: 100vw; max-height: 56.25vw; background: #111; overflow: hidden; }}
        .slide {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; opacity: 0; transition: opacity 1s ease-in-out; }}
        .slide.active {{ opacity: 1; }}
        .logo {{ width: 150px; height: 150px; margin-bottom: 2rem; }}
        h1 {{ font-size: 4rem; font-weight: 300; letter-spacing: 0.1em; margin: 0; }}
        p {{ font-size: 1.5rem; color: #888; margin-top: 1rem; max-width: 800px; text-align: center; line-height: 1.5; }}
        .video-container {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; opacity: 0; transition: opacity 2s ease-in-out; }}
        .video-container.active {{ opacity: 1; z-index: 10; }}
        video {{ width: 100%; height: 100%; object-fit: cover; }}
    </style>
</head>
<body>
    <div class="presentation-container">
        <div id="slide1" class="slide active">
            <img src="{logo_uri}" class="logo" />
            <h1>rectoverso</h1>
            <p>A multi-agent AI filmmaking pipeline built with Opus 4.7</p>
        </div>
        <div id="slide2" class="slide">
            <h1>The Architecture</h1>
            <p>Strict Tiered Agent System. No agents talk directly to each other.<br>Everything flows through a shared, validated JSON manifest.</p>
        </div>
        <div id="slide3" class="slide">
            <h1>"Here"</h1>
            <p>A 60-second exploration of the places we stand, and the places we don't.</p>
        </div>
        <div id="video-wrap" class="video-container">
            <video id="main-video" controls>
                <source src="../out.mp4" type="video/mp4">
            </video>
        </div>
    </div>

    <script>
        const sequence = [
            {{ id: 'slide1', duration: 4000 }},
            {{ id: 'slide2', duration: 4000 }},
            {{ id: 'slide3', duration: 3000 }},
            {{ id: 'video-wrap', duration: 60000 }}
        ];

        let currentIdx = 0;

        function nextSlide() {{
            const current = document.getElementById(sequence[currentIdx].id);
            if (currentIdx > 0) {{
                document.getElementById(sequence[currentIdx-1].id).classList.remove('active');
            }}
            current.classList.add('active');
            
            if (sequence[currentIdx].id === 'video-wrap') {{
                document.getElementById('main-video').play();
            }} else {{
                setTimeout(nextSlide, sequence[currentIdx].duration);
            }}
            currentIdx++;
        }}

        setTimeout(nextSlide, 2000); // Start after 2s
    </script>
</body>
</html>
"""
    (out_dir / "index.html").write_text(html_content)
    print(f"Presentation HTML generated at {out_dir / 'index.html'}")

if __name__ == "__main__":
    main()
