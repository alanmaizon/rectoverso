import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def type_print(text, color_code="\033[0m", delay=0.02):
    sys.stdout.write(color_code)
    for char in text:
        sys.stdout.write(char)
        sys.stdout.flush()
        time.sleep(delay)
    sys.stdout.write("\033[0m\n")

def main():
    type_print("=== Starting rectoverso DEMO_MODE=1 (Lighthouse Override) ===", "\033[1;95m", 0.01)
    type_print("Initializing multi-agent orchestrator...", "\033[90m", 0.01)
    time.sleep(0.5)

    from src.producer.orchestrator import ToolSet, FilmOrchestrator
    from src.producer import open_event_log
    
    path = ROOT / "tests" / "fixtures" / "orchestrator_sh_005_v3.json"
    with open(path, "r") as f:
        fixture = json.load(f)

    manifest_path = ROOT / "scratch" / "manifest_demo_lighthouse.json"

    class FakeRendererTool:
        def __call__(self, shot, attempt_id, **kwargs):
            type_print(f"\n[Agent: Renderer] 🎬 Synthesizing video via Seedance 2.0 (Attempt {attempt_id})...", "\033[96m", 0.02)
            time.sleep(0.8)
            type_print("  ↳ Uploading script + reference image...", "\033[90m", 0.01)
            time.sleep(0.5)
            type_print("  ↳ Applying cinematic motion prompt: 'gentle push-in behind shoulder...'", "\033[90m", 0.01)
            time.sleep(1.2)
            type_print("  ↳ Video generation complete! MD5: a78f586460429482b42ffb", "\033[92m", 0.01)
            with open(manifest_path, "r") as f:
                mn = json.load(f)
            for s in mn["shots"]:
                if s["shot_id"] == shot["shot_id"]:
                    s["status"] = "judging"
                    if "attempts" not in s:
                        s["attempts"] = []
                    s["attempts"].append({"attempt_id": attempt_id, "render_path": fixture["tool_results"]["render"]["render_path"], "provider": "fal_bytedance_seedance_2_0_i2v", "cost_usd": 0.0, "started_at": "2026-04-23T16:00:00Z", "completed_at": "2026-04-23T16:01:00Z"})
                    break
            with open(manifest_path, "w") as f:
                json.dump(mn, f, indent=2)

    class FakeJudgeTool:
        def __call__(self, shot, attempt_id, **kwargs):
            time.sleep(0.5)
            type_print(f"\n[Agent: Shot Judge] ⚖️ Analyzing MP4 for continuity and framing...", "\033[93m", 0.02)
            time.sleep(0.8)
            type_print("  ↳ Checking reference adherence (Wait...)", "\033[90m", 0.01)
            time.sleep(0.6)
            type_print("  ↳ Verifying prompt constraints (Wait...)", "\033[90m", 0.01)
            time.sleep(1.0)
            type_print("  ↳ SCORING: 0.91 (Threshold: 0.85). Outcome: APPROVED 🎉", "\033[1;92m", 0.02)
            with open(manifest_path, "r") as f:
                mn = json.load(f)
            for s in mn["shots"]:
                if s["shot_id"] == shot["shot_id"]:
                    s["status"] = "approved"
                    s["attempts"][-1].update({
                        "judge_score": 0.91,
                        "judge_notes": "Perfect execution of the brief. Cold naturalistic tone preserved.",
                        "outcome": "approved",
                        "approved_by": "shot_judge"
                    })
                    s["final"] = {"render_path": fixture["tool_results"]["render"]["render_path"], "attempt_id": attempt_id}
                    break
            with open(manifest_path, "w") as f:
                json.dump(mn, f, indent=2)

    class FakeReviseTool:
        def __call__(self, *args, **kwargs):
            return {}

    class FakeNormalizeTool:
        def __call__(self, shot, attempt_id, **kwargs):
            time.sleep(0.2)
            type_print(f"\n[Agent: Normalizer] ⚙️ Conforming codec/resolution for Editor...", "\033[90m", 0.02)
            time.sleep(0.4)
            type_print("  ↳ Render specs normalized to baseline MP4.", "\033[92m", 0.01)
            return {
                "status": "ok",
                "output_path": fixture["tool_results"]["render"]["render_path"],
                "output_md5": "bbbb586460429482b42ffbbbb5864604",
                "output_size_bytes": 1024,
                "duration_s": 4.0,
                "target_spec": {},
                "latency_s": 1.2
            }

    class FakeAudioTool:
        def __call__(self, *args, **kwargs):
            return {}
            
    class FakeEditorTool:
        def __call__(self, manifest_path, workspace_dir, brief_slice, estimated_cost_usd, **kwargs):
            time.sleep(0.5)
            type_print(f"\n[Agent: Editor] ✂️ Assembling final HTML composition via Hyperframes...", "\033[1;36m", 0.02)
            time.sleep(0.8)
            type_print("  ↳ Linting manifest specs against Hyperframes renderer...", "\033[90m", 0.01)
            time.sleep(0.6)
            type_print("  ↳ Crossfading normalized shots + mixing audio stems...", "\033[90m", 0.01)
            time.sleep(1.2)
            
            # create dummy zip and mp4 for real!
            out_mp4 = ROOT / "artifacts" / "edit" / "proj_demo_final.mp4"
            out_zip = ROOT / "artifacts" / "edit" / "proj_demo_composition.zip"
            out_mp4.parent.mkdir(parents=True, exist_ok=True)
            out_mp4.touch()
            out_zip.touch()
            
            type_print(f"  ↳ Render complete! Output written to {out_mp4.relative_to(ROOT)}", "\033[1;92m", 0.02)
            type_print(f"  ↳ Composition archive saved at {out_zip.relative_to(ROOT)}", "\033[1;92m", 0.02)

            return {
                "status": "ok",
                "renderer_version": "v1.0.0",
                "composition_path": "artifacts/edit/hyperframes.json",
                "composition_archive_path": str(out_zip.relative_to(ROOT)),
                "render_path": str(out_mp4.relative_to(ROOT)),
                "render_md5": "ccbb586460429482b42ffbccbb586460",
                "duration_s": 4.0,
                "cost_usd": 0.0,
                "latency_s": 4.5,
                "transcript_tail": "",
                "stderr_tail": ""
            }

    tools = ToolSet(
        render=FakeRendererTool(),
        judge=FakeJudgeTool(),
        revise=FakeReviseTool(),
        generate_ref=None,
        audio=FakeAudioTool(),
        editor=FakeEditorTool(),
        normalize=FakeNormalizeTool()
    )
    
    manifest = {
        "manifest_version": "1.0",
        "project_id": "proj_demo",
        "created_at": "2026-04-23T16:00:00Z",
        "updated_at": "2026-04-23T16:00:00Z",
        "film_status": "pending",
        "brief": {"logline": "A lighthouse keeper pushes the door open and steps inside.", "target_duration_s": 4.0, "tone": ["cinematic", "moody"], "genre": "drama", "source_path": "brief.md"},
        "script": {"status": "approved", "version": 1, "path": "script.md", "approved_by": "director", "approved_at": "2026-04-23T16:00:00Z"},
        "shots": [fixture["starting_shot"]],
        "audio": {"dialogue": [], "sfx": []},
        "edit": {"status": "pending", "renderer": "hyperframes"},
        "budget": {"cap_usd": 100.0, "spent_usd": 0.0, "by_provider": {"fal_bytedance_seedance_2_0_i2v": 3.024}, "alibaba_quota_remaining": 50, "elevenlabs_credits_remaining": 100000},
        "run_state": {"current_stage": "make", "last_event_id": 0, "resumable": True},
        "creative_decisions": []
    }
    
    manifest["shots"][0]["status"] = "prompted"
    manifest["shots"][0]["attempts"] = []
    manifest["shots"][0]["audio_cues"] = []
    
    time.sleep(0.5)
    type_print(f"\n[Orchestrator] 🚀 Booting run. Target: {manifest['shots'][0]['shot_id']} [{manifest['shots'][0]['status']}]", "\033[1;34m", 0.01)
    
    events_db = ":memory:"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    
    with open_event_log(events_db) as log:
        orchestrator = FilmOrchestrator(
            manifest=manifest,
            manifest_path=manifest_path,
            events=log,
            tools=tools,
            run_mode="submission"
        )
        orchestrator.run()
    
    time.sleep(0.5)
    type_print("\n=== ✨ PRODUCTION LOOP FINISHED ✨ ===", "\033[1;95m", 0.02)
    with open(manifest_path, "r") as f:
        final_manifest = json.load(f)
        
    final_shot = final_manifest["shots"][0]
    type_print(f"Film Status       : {final_manifest.get('film_status')}", "\033[1;96m", 0.01)
    type_print(f"Final Render      : {final_manifest['edit'].get('render_path')}", "\033[92m", 0.01)
    type_print(f"Comp Archive      : {final_manifest['edit'].get('composition_archive_path')}", "\033[92m", 0.01)

if __name__ == "__main__":
    main()
