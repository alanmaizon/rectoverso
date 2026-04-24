#!/usr/bin/env python3
"""
rectoverso — demo theater
═══════════════════════════════════════════════════════════════════

3-minute CLI demonstration of the multi-agent film pipeline.
No live API calls. Reads from demo/fixtures/. Runs real contract code.

What this shows
───────────────
  Act 1  Producer reads brief, routes 7 shots across Veo / Wan / Kling
  Act 2  Creative Director feedback changes sh_004.artistic_direction;
         Contract 3 (cd_to_prompt_smith) blocks a creative-driven re-render
         until the translation step is complete, then passes cleanly
  Act 3  Contract 6 (normalize_to_editor) catches a silent-drift scenario:
         an approved shot without final.normalized_path would corrupt the
         Hyperframes concat muxer — block fires before Editor dispatches
  Act 4  Manifest repaired, Editor dispatches (DEMO_MODE), film assembled

Run from repo root:
    python scripts/demo_theater.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── path setup ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from contracts import ContractViolation, validate_before_dispatch  # noqa: E402

# ── ANSI palette ─────────────────────────────────────────────────────────────
RST   = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"
RED   = "\033[31m"
GRN   = "\033[32m"
YEL   = "\033[33m"
BLU   = "\033[34m"
MAG   = "\033[35m"
CYN   = "\033[36m"
WHT   = "\033[37m"
DGRY  = "\033[90m"

# ── helpers ───────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def _print(*args, delay: float = 0.0, **kwargs):
    print(*args, **kwargs)
    sys.stdout.flush()
    if delay:
        time.sleep(delay)

def _pause(secs: float = 1.2):
    time.sleep(secs)

def _rule(char: str = "─", width: int = 68):
    _print(DGRY + char * width + RST)

def _header(label: str, color: str = CYN):
    _print()
    _rule("─")
    _print(f"{color}{BOLD}  {label}{RST}")
    _rule("─")

def _agent(name: str) -> str:
    colours = {
        "producer":         BLU,
        "shot_judge":       YEL,
        "prompt_smith":     MAG,
        "creative_director": CYN,
        "audio_agent":      GRN,
        "editor_agent":     WHT,
        "router":           DGRY,
        "contracts":        RED,
    }
    c = colours.get(name, WHT)
    return f"{c}{BOLD}[{name}]{RST}"

def _ok(msg: str):
    _print(f"  {GRN}✓{RST}  {msg}")

def _warn(msg: str):
    _print(f"  {YEL}⚠{RST}  {msg}")

def _block(msg: str):
    _print(f"  {RED}{BOLD}✗  BLOCK{RST}  {msg}")

def _info(msg: str, indent: int = 5):
    _print(" " * indent + DGRY + msg + RST)

def _json_fragment(d: dict, indent: int = 4):
    raw = json.dumps(d, indent=2)
    for line in raw.splitlines():
        _print(" " * indent + DGRY + line + RST)

# ── load fixture ─────────────────────────────────────────────────────────────

def _load_manifest() -> dict:
    path = ROOT / "demo" / "fixtures" / "manifest_golden.json"
    return json.loads(path.read_text())

def _find_shot(manifest: dict, shot_id: str) -> dict:
    for s in manifest["shots"]:
        if s["shot_id"] == shot_id:
            return s
    raise KeyError(shot_id)

# ═══════════════════════════════════════════════════════════════════════════
# ACT 0 — BANNER
# ═══════════════════════════════════════════════════════════════════════════

def act_banner():
    _print()
    _print(f"{CYN}{BOLD}{'═' * 68}{RST}")
    _print(f"{CYN}{BOLD}  RECTOVERSO{RST}{WHT}  —  multi-agent AI film pipeline{RST}")
    _print(f"{DGRY}  Built with Claude Opus 4.7  ·  Anthropic Hackathon Apr 2026{RST}")
    _print(f"{CYN}{BOLD}{'═' * 68}{RST}")
    _pause(1.5)

# ═══════════════════════════════════════════════════════════════════════════
# ACT 1 — BRIEF → MANIFEST → ROUTING
# ═══════════════════════════════════════════════════════════════════════════

_PROVIDER_LABEL = {
    "veo":  f"{MAG}Vertex Veo 3.1{RST}",
    "wan":  f"{BLU}Alibaba Wan 2.7+{RST}",
    "kling": f"{YEL}Kling 2.1 Pro{RST}  {DGRY}(I2V — human shot){RST}",
}
_COST_LABEL = {
    "veo":   f"{MAG}$0.60{RST}",
    "wan":   f"{GRN}$0.00{RST} {DGRY}(free-quota){RST}",
    "kling": f"{YEL}$0.49{RST}",
}

def act_routing(manifest: dict):
    _header("ACT 1  —  BRIEF → ROUTING DECISIONS", BLU)
    _pause(0.6)

    brief = manifest["brief"]
    _print(f"  {BOLD}Project:{RST}  {brief.get('title', 'Here')}")
    _print(f"  {BOLD}Duration:{RST} {brief.get('target_duration_s', 60)}s")
    _print(f"  {BOLD}Style:{RST}    {brief.get('artistic_style', '')[:64]}…")
    _print(f"  {BOLD}Budget:{RST}   ${manifest['budget']['cap_usd']} cap  "
           f"({len(manifest['shots'])} shots)")
    _pause(1.0)

    _print()
    _print(f"  {_agent('router')} routing {len(manifest['shots'])} shots:", delay=0.3)
    _print()

    for shot in manifest["shots"]:
        sid      = shot["shot_id"]
        prov     = shot["routing"]["chosen_provider"]
        dur      = shot["duration_s"]
        desc     = shot["description"][:52].rstrip()
        label    = _PROVIDER_LABEL.get(prov, prov)
        cost     = _COST_LABEL.get(prov, "")
        note = ""
        if prov == "kling":
            note = f"  {RED}humans_never_veo{RST} hard-rule"
        elif prov == "veo":
            note = f"  {DGRY}hero / cinematic{RST}"
        _print(f"  {DGRY}{sid}{RST}  {dur:4.0f}s  {label}  {cost}{note}", delay=0.18)
        _info(f"     {desc}…", indent=0)

    _pause(1.2)
    budget = manifest["budget"]
    _print()
    _print(f"  {_agent('producer')} manifest locked — "
           f"{GRN}7 shots{RST}, ${budget['spent_usd']:.2f} / ${budget['cap_usd']}")

# ═══════════════════════════════════════════════════════════════════════════
# ACT 2 — PRODUCER WALKS SHOTS + CD INTERVENTION
# ═══════════════════════════════════════════════════════════════════════════

_CD_TS       = "2026-04-24T14:31:00Z"
_UPDATE_TS   = "2026-04-24T14:33:00Z"
_CD_FEEDBACK = {
    "ts":         _CD_TS,
    "from_agent": "creative_director",
    "priority":   "high",
    "feedback": (
        "sh_004 Amazon canopy: the macaw crosses left→right "
        "while the camera drifts east (right→left in screen space). "
        "Opposing vectors break the restrained, single-direction pull "
        "that holds this film together."
    ),
    "suggestion": (
        "Constrain the macaw to right→left entry/exit to match camera "
        "drift direction. Add explicit vector note to artistic_direction."
    ),
    "addressed": False,
}
_ARTISTIC_DIRECTION_NEW = (
    "Camera drifts east-to-west (right→left in screen space). "
    "Macaw must enter from screen-right and exit screen-left — "
    "matching the dominant motion vector. No opposing elements."
)


def act_production(manifest: dict):
    _header("ACT 2  —  PRODUCER COORDINATES SPECIALISTS", GRN)
    _pause(0.5)

    # Walk the first three shots as a fast-forward
    for i, shot in enumerate(manifest["shots"][:3]):
        sid   = shot["shot_id"]
        prov  = shot["routing"]["chosen_provider"]
        score = shot["attempts"][0]["judge_score"]
        _print(f"  {_agent('producer')} dispatch render   {DGRY}{sid}{RST} → {prov}", delay=0.1)
        _print(f"  {_agent('shot_judge')} score {GRN}{score:.2f}{RST}  → {GRN}approved{RST}", delay=0.25)

    _pause(0.4)
    _print(f"  {DGRY}… (3 approved shots — Creative Director mid-film trigger fires){RST}")
    _pause(1.0)

    # ── CD reads approved judge feedback ──────────────────────────────────
    _print()
    _print(f"  {_agent('creative_director')} reading approved judge_feedback across film…", delay=0.6)
    _print(f"  {_agent('creative_director')} {YEL}FLAG{RST}  sh_004 — motion vector conflict", delay=0.5)
    _print()
    _json_fragment({
        "shot_id":   "sh_004",
        "priority":  "high",
        "feedback":  _CD_FEEDBACK["feedback"][:88] + "…",
        "suggestion": _CD_FEEDBACK["suggestion"][:72] + "…",
    })
    _pause(1.2)

    # Inject CD feedback into in-memory manifest
    sh004 = _find_shot(manifest, "sh_004")
    sh004.setdefault("creative_feedback", []).append(_CD_FEEDBACK)

    # ── Contract 3 — WITHOUT artistic_direction update ────────────────────
    _print()
    _print(f"  {_agent('producer')} will dispatch {MAG}PromptSmith{RST} creative re-render on sh_004")
    _pause(0.5)
    _print(f"  {_agent('contracts')} validate_before_dispatch("
           f"'prompt_smith', 'sh_004', ctx={{'creative_driven': True}})", delay=0.4)

    try:
        validate_before_dispatch("prompt_smith", "sh_004", manifest,
                                 {"creative_driven": True, "revision": False})
        _warn("no violation — unexpected")
    except ContractViolation as exc:
        v = exc.violations[0]
        _block(f"{RED}{v.contract.value}{RST}")
        _info(v.reason, indent=7)
        _info(f"detail: {v.detail}", indent=7)

    _pause(1.0)

    # ── Producer translates CD feedback → artistic_direction ──────────────
    _print()
    _print(f"  {_agent('producer')} addressing CD feedback — updating artistic_direction…", delay=0.5)
    sh004["artistic_direction"] = _ARTISTIC_DIRECTION_NEW
    sh004.setdefault("history", []).append({
        "ts":    _UPDATE_TS,
        "event": "artistic_direction_updated",
        "by":    "producer",
        "detail": "cd_feedback addressed: macaw direction corrected to match camera drift",
    })
    # NOTE: do NOT mark feedback addressed yet — Contract 3 needs to see unaddressed
    # CD feedback + a post-dated history event to confirm the translation step ran.

    _json_fragment({
        "shot_id":           "sh_004",
        "artistic_direction": _ARTISTIC_DIRECTION_NEW[:72] + "…",
        "history[-1]":       {"event": "artistic_direction_updated", "ts": _UPDATE_TS},
    })
    _pause(0.8)

    # ── Contract 3 — WITH artistic_direction update (CD feedback still unaddressed)
    # The contract checks: unaddressed CD feedback exists → history has
    # artistic_direction_updated at/after that ts → artistic_direction non-empty.
    # All three conditions are now met → clean PASS.
    _print()
    _print(f"  {_agent('contracts')} re-validate after update…", delay=0.4)
    try:
        warns = validate_before_dispatch("prompt_smith", "sh_004", manifest,
                                         {"creative_driven": True, "revision": False})
        if warns:
            _warn(f"{warns[0].reason}")
        else:
            _ok(f"Contract 3 (cd_to_prompt_smith)  {GRN}PASS{RST}  "
                f"{DGRY}— translation step complete{RST}")
    except ContractViolation as exc:
        _block(str(exc))

    # Mark feedback addressed now that we know the translation step is verified.
    sh004["creative_feedback"][0]["addressed"]    = True
    sh004["creative_feedback"][0]["addressed_at"] = _UPDATE_TS
    sh004["creative_feedback"][0]["addressed_by"] = "producer"

    _print()
    _print(f"  {_agent('prompt_smith')} re-prompt  sh_004  incorporating artistic_direction", delay=0.35)
    _print(f"  {_agent('producer')} dispatch render   sh_004 → Veo 3.1 (creative re-render)", delay=0.3)
    _print(f"  {_agent('shot_judge')} score {GRN}0.91{RST}  → {GRN}approved{RST}", delay=0.3)
    _pause(0.5)

    # ── Fast-forward remaining shots ──────────────────────────────────────
    _print()
    _print(f"  {DGRY}… shots 5–7 render, judge, approve{RST}", delay=0.3)
    for shot in manifest["shots"][4:]:
        sid   = shot["shot_id"]
        prov  = shot["routing"]["chosen_provider"]
        score = shot["attempts"][0]["judge_score"]
        _print(f"  {DGRY}{sid}{RST}  {prov}  → {GRN}approved{RST}  score {score:.2f}", delay=0.15)

    _pause(0.6)
    _print()
    _print(f"  {_agent('audio_agent')} TTS + SFX — 5 dialogue lines, orchestral score", delay=0.4)
    _ok(f"audio phase complete  {DGRY}(ElevenLabs / 117,999 credits remaining){RST}")

    _pause(0.8)
    _print()
    _print(f"  {_agent('producer')} normalize pass — ffmpeg codec homogenization…", delay=0.4)
    for shot in manifest["shots"]:
        sid = shot["shot_id"]
        _ok(f"{sid}  normalized_path → artifacts/renders/{sid}_norm.mp4  "
            f"{DGRY}md5=a3f8…{RST}")

# ═══════════════════════════════════════════════════════════════════════════
# ACT 3 — CONTRACT 6: SILENT-DRIFT SCENARIO
# ═══════════════════════════════════════════════════════════════════════════

def act_silent_drift(manifest: dict):
    _header("ACT 3  —  CONTRACT CATCHES SILENT DRIFT  (normalize_to_editor)", RED)
    _pause(0.6)

    _print(f"  {BOLD}Scenario:{RST}  all 7 shots approved  →  Editor dispatch triggered")
    _pause(0.5)
    _print(f"  {DGRY}Simulating a partial-recovery scenario:{RST}")
    _print(f"  {DGRY}sh_005 final.normalized_path was not written (ffmpeg died mid-run){RST}")
    _pause(0.8)

    # Corrupt: strip normalized_path from sh_005
    sh005 = _find_shot(manifest, "sh_005")
    original_norm_path = sh005.get("final", {}).get("normalized_path", "")
    original_norm_md5  = sh005.get("final", {}).get("normalized_md5", "")
    sh005.setdefault("final", {}).pop("normalized_path", None)
    sh005["final"].pop("normalized_md5", None)

    _print()
    _print(f"  {_agent('contracts')} validate_before_dispatch('editor_agent', None, manifest)", delay=0.5)
    _pause(0.5)

    try:
        validate_before_dispatch("editor_agent", None, manifest, {})
        _warn("no violation — unexpected (fixture may need normalized_path stripped)")
    except ContractViolation as exc:
        for v in exc.violations:
            _block(f"{RED}{v.contract.value}{RST}  shot={v.shot_id}")
            _info(v.reason, indent=7)

    _pause(1.2)

    _print()
    _print(f"  {BOLD}Without this contract:{RST}")
    _info("  Editor feeds sh_005 raw Wan h264/High render into Hyperframes concat")
    _info("  Mixed codecs → ffmpeg concat: \"non monotonically increasing dts\"")
    _info("  Hyperframes exits 1 — composition silently broken, film not shipped")
    _info(f"  Verified 2026-04-23 across Wan / Kling Pro / Seedance  (scratch/hyperframes-probe/)")
    _pause(1.2)

    # ── Fix ───────────────────────────────────────────────────────────────
    _print()
    _print(f"  {_agent('producer')} resuming normalize pass for sh_005…", delay=0.5)
    sh005["final"]["normalized_path"] = original_norm_path or f"artifacts/renders/sh_005_norm.mp4"
    sh005["final"]["normalized_md5"]  = original_norm_md5  or "a3f8b2c1d4e5f678" * 2
    _ok(f"sh_005  normalized_path restored")
    _pause(0.4)

    _print()
    _print(f"  {_agent('contracts')} re-validate…", delay=0.4)
    try:
        warns = validate_before_dispatch("editor_agent", None, manifest, {})
        if warns:
            _warn(str(warns[0].reason))
        else:
            _ok(f"Contract 6 (normalize_to_editor)  {GRN}PASS{RST}  — all 7 shots normalized")
        _ok(f"Contract 5 (cd_editor_authority)  {GRN}PASS{RST}  — no unaddressed CD feedback")
        _ok(f"Contract 1 (audio_to_editor)      {GRN}PASS{RST}  — compressibility_s present")
    except ContractViolation as exc:
        _block(str(exc))

# ═══════════════════════════════════════════════════════════════════════════
# ACT 4 — EDITOR DISPATCH + FINAL SUMMARY
# ═══════════════════════════════════════════════════════════════════════════

def act_editor_and_summary(manifest: dict):
    _header("ACT 4  —  EDITOR ASSEMBLES FILM  (Hyperframes, DEMO_MODE)", WHT)
    _pause(0.6)

    _print(f"  {_agent('editor_agent')} session started  {DGRY}(MockEditorSession — zero API calls){RST}", delay=0.5)
    _print(f"  {_agent('editor_agent')} npx hyperframes init  …  {GRN}ok{RST}", delay=0.4)
    _print(f"  {_agent('editor_agent')} composing 7 shots  →  composition.json", delay=0.4)

    for shot in manifest["shots"]:
        sid = shot["shot_id"]
        dur = shot["duration_s"]
        _print(f"    {DGRY}+ {sid}  {dur:4.0f}s  {shot['routing']['chosen_provider']}{RST}", delay=0.12)

    _print(f"  {_agent('editor_agent')} npx hyperframes lint --json  …  {GRN}0 errors{RST}", delay=0.4)
    _print(f"  {_agent('editor_agent')} npx hyperframes render  …  {GRN}ok{RST}", delay=0.5)
    _ok(f"artifacts/edit/out.mp4  {DGRY}md5=demo_md5_hash{RST}")
    _pause(0.8)

    # ── Summary table ─────────────────────────────────────────────────────
    _print()
    _rule("═")
    _print(f"{GRN}{BOLD}  FILM COMPLETE — 'Here'  ({manifest['brief'].get('title','Here')}){RST}")
    _rule("═")
    budget = manifest["budget"]
    shots  = manifest["shots"]
    _print(f"  {'Shots':20s} {len(shots)} / {len(shots)} approved")
    _print(f"  {'Providers':20s} Veo 3.1 · Wan 2.7+ · Kling 2.1 Pro")
    _print(f"  {'Budget':20s} ${budget['spent_usd']:.2f} / ${budget['cap_usd']}  "
           f"{GRN}under cap{RST}")
    _print(f"  {'Audio':20s} 5 dialogue lines · orchestral score  {GRN}ok{RST}")
    _print(f"  {'Renderer':20s} Hyperframes → {manifest['edit']['render_path']}")
    _print(f"  {'Contracts fired':20s} 3  (cd_to_prompt_smith ✗→✓, "
           f"normalize_to_editor ✗→✓, cd_editor_authority ✓)")
    _rule("═")
    _print()


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    manifest = _load_manifest()

    # Ensure the in-memory manifest has all the fields contracts expect
    manifest.setdefault("creative_decisions", [])
    for shot in manifest["shots"]:
        shot.setdefault("creative_feedback", [])
        shot.setdefault("judge_feedback", [])
        shot.setdefault("history", [])
        # Ensure every approved shot has BOTH normalized_path + normalized_md5.
        # The golden fixture has normalized_path from make_golden_demo.py but
        # not normalized_md5 — Contract 6 checks both (audit-chain integrity).
        final = shot.setdefault("final", {})
        if shot.get("status") == "approved":
            if not final.get("normalized_path"):
                final["normalized_path"] = f"artifacts/renders/{shot['shot_id']}_norm.mp4"
            if not final.get("normalized_md5"):
                final["normalized_md5"] = "a3f8b2c1d4e5f6789abc0123456789ab"
    # Audio contract needs compressibility_s on each dialogue line
    for dlg in manifest.get("audio", {}).get("dialogue", []):
        dlg.setdefault("compressibility_s", 0.3)
        timing = dlg.setdefault("timing", {})
        dur = float(dlg.get("duration_s", 3.0))
        timing.setdefault("in_s", 0.0)
        timing.setdefault("out_s", dur)

    act_banner()
    act_routing(manifest)
    _pause(1.4)
    act_production(manifest)
    _pause(1.4)
    act_silent_drift(manifest)
    _pause(1.4)
    act_editor_and_summary(manifest)


if __name__ == "__main__":
    main()
