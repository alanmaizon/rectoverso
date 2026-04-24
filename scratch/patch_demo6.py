def run():
    with open('scratch/run_lighthouse_demo.py', 'r') as f:
        src = f.read()

    src = src.replace('"render_md5": fixture["tool_results"]["render"]["render_md5"],', '')
    src = src.replace('"duration_rendered_s": fixture["tool_results"]["render"].get("duration_rendered_s", 4.0),', '')
    src = src.replace('"provider_metadata": fixture["tool_results"]["render"].get("provider_metadata", {})', '')
    src = src.replace('s["attempts"].append({\n                        "attempt_id": attempt_id,\n                        "render_path": fixture["tool_results"]["render"]["render_path"],\n                        \n                        \n                        \n                    })', 's["attempts"].append({"attempt_id": attempt_id, "render_path": fixture["tool_results"]["render"]["render_path"]})')
    
    # Just generic fix for attempts dict
    import re
    src = re.sub(r's\["attempts"\].append\(\{(.*?)\}\)', 's["attempts"].append({"attempt_id": attempt_id, "render_path": fixture["tool_results"]["render"]["render_path"]})', src, flags=re.DOTALL)
    
    with open('scratch/run_lighthouse_demo.py', 'w') as f:
        f.write(src)
run()
