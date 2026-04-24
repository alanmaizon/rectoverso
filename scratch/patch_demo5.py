def run():
    with open('scratch/run_lighthouse_demo.py', 'r') as f:
        src = f.read()

    # Need to target the correct original string this time
    # Looking for a fallback in case the script substitution failed
    import re
    src = re.sub(r'"script":\s*\{"status":\s*"approved"[^\}]*\},', '"script": {"status": "approved", "version": 1, "path": "script.md", "approved_by": "director", "approved_at": "2026-04-23T16:00:00Z"},', src)

    with open('scratch/run_lighthouse_demo.py', 'w') as f:
        f.write(src)
run()
