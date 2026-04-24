def run():
    with open('scratch/run_lighthouse_demo.py', 'r') as f:
        src = f.read()

    import re
    src = re.sub(r's\["final"\] = \{"render_path": fixture\["tool_results"\]\["render"\]\["render_path"\]\}', 
                 's["final"] = {"render_path": fixture["tool_results"]["render"]["render_path"], "attempt_id": attempt_id}', 
                 src)
    
    with open('scratch/run_lighthouse_demo.py', 'w') as f:
        f.write(src)
run()
