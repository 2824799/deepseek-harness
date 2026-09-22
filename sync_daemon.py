import subprocess
import time

SETUP = "/home/nahida/agents/sever/dsh/setup_isolated.py"

# The projection is incremental and skips unchanged threads, so an idle pass
# costs about 30 ms. Polling at 700 ms keeps the browser within roughly a
# second of Codex without measurable load.
INTERVAL_S = 0.7

while True:
    started = time.time()
    try:
        subprocess.run(["python3", SETUP], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    time.sleep(max(0.0, INTERVAL_S - (time.time() - started)))
