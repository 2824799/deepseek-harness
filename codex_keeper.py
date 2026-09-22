"""Create a Codex thread and hold it until its first message lands.

The app-server only writes a thread to disk once it has a turn; a blank
thread lives in memory, owned by the connection that created it, and is
reaped about a minute after that connection closes. The web UI creates a
thread when the user opens a new conversation and sends the first message
seconds or minutes later, so the creating connection has to outlive the
request. This process prints the new thread id, then keeps its connection
open until Codex has persisted the thread or half an hour passes.
"""
import json
import os
import sqlite3
import sys
import time

import codex_link as link

CODEX_STATE = os.path.join(os.environ.get("HOME", ""), ".codex", "state_5.sqlite")
TTL_S = 1800.0


def persisted(thread_id):
    try:
        conn = sqlite3.connect(f"file:{CODEX_STATE}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT rollout_path FROM threads WHERE id = ?", (thread_id,))
        row = cur.fetchone()
        conn.close()
        return bool(row and row[0] and os.path.exists(row[0]))
    except sqlite3.Error:
        return False


def main(argv):
    cwd = argv[1] if len(argv) > 1 and argv[1] not in ("", "None") else None
    # create_thread closes its connection on return, and the reaper kills a
    # blank thread once its creating connection is gone, so the start call
    # and the hold must share one socket.
    ws = link.connect(timeout=15)
    params = {"cwd": cwd or "/home/nahida/agents/sever"}
    model = link.default_model()
    if model:
        params["model"] = model
    res = ws.call("thread/start", params, timeout=25)
    if not res["ok"]:
        print(json.dumps(res["error"]), file=sys.stderr)
        return 1
    thread = (res["value"] or {}).get("thread") or {}
    thread_id = thread.get("id")
    if thread_id and model:
        link.save_selection(thread_id, model)
    print(thread_id, flush=True)
    deadline = time.time() + TTL_S
    while time.time() < deadline:
        if persisted(thread_id):
            ws.close()
            return 0
        time.sleep(3.0)
    ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
