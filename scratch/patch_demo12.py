def run():
    with open('scratch/run_lighthouse_demo.py', 'r') as f:
        src = f.read()

    import re
    src = re.sub(r'"render_md5": "ccbb586460429482b42ffbccbb58646042"', '"render_md5": "ccbb586460429482b42ffbccbb586460"', src)

    with open('scratch/run_lighthouse_demo.py', 'w') as f:
        f.write(src)
run()
