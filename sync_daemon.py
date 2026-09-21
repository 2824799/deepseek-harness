import subprocess
import time

while True:
    try:
        subprocess.run(["python3", "/home/nahida/agents/sever/dsh/setup_isolated.py"], check=True)
    except Exception as e:
        pass
    time.sleep(3)
