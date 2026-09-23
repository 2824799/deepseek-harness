"""Keep web-created blank Codex threads visible until Codex records a turn."""

import fcntl
import json
import os
import sys
import time

BASE_DIR = "/home/nahida/agents/sever/dsh/.dsh-codex"
FILE = os.path.join(BASE_DIR, "pending-threads.json")
LOCK = os.path.join(BASE_DIR, "projection.lock")


def load():
    try:
        with open(FILE, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save(entries):
    temp = FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, ensure_ascii=False)
    os.replace(temp, FILE)


def session_file(thread_id, cwd):
    slug = "--" + cwd.strip("/").replace("/", "-") + "--"
    return os.path.join(BASE_DIR, "sessions", slug, "session-" + thread_id, "session.jsonl")


def register(thread_id, workspace_id, cwd):
    os.makedirs(BASE_DIR, exist_ok=True)
    with open(LOCK, "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        entries = load()
        created = time.time()
        entries[thread_id] = {
            "workspaceId": workspace_id, "cwd": cwd, "createdAt": created,
        }
        target = session_file(thread_id, cwd)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if not os.path.exists(target):
            with open(target, "x", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "session", "version": 0, "id": "session-" + thread_id,
                    "createdAt": int(created * 1000), "cwd": cwd,
                    "delegationDepth": 0, "agentPreset": "standard",
                }, ensure_ascii=False) + "\n")
        save(entries)


if __name__ == "__main__":
    register(sys.argv[1], sys.argv[2], sys.argv[3])
