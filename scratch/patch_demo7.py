def run():
    with open('scratch/run_lighthouse_demo.py', 'r') as f:
        src = f.read()

    import re
    src = re.sub(r's\["attempts"\].append\(\{"attempt_id": attempt_id, "render_path": fixture\["tool_results"\]\["render"\]\["render_path"\]\}\)', 
                 's["attempts"].append({"attempt_id": attempt_id, "render_path": fixture["tool_results"]["render"]["render_path"], "provider": "fal_bytedance_seedance_2_0_i2v", "cost_usd": 0.0})', 
                 src)
    
    with open('scratch/run_lighthouse_demo.py', 'w') as f:
        f.write(src)
run()
