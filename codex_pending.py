"""Keep web-created blank Codex threads visible until Codex records a turn."""

import fcntl
import json
import os
import sys
import time

BASE_DIR = os.environ.get("DSH_HOME") or "/home/nahida/agents/sever/dsh/.dsh-codex"
FILE = os.path.join(BASE_DIR, "pending-threads.json")
LOCK = os.path.join(BASE_DIR, "projection.lock")
MAX_BLANK_AGE_S = 1860


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


def prune(entries, persisted_ids, now=None):
    """Discard a blank placeholder after its Codex connection has expired."""
    now = time.time() if now is None else now
    return {thread_id: entry for thread_id, entry in entries.items()
            if thread_id in persisted_ids
            or now - entry.get("createdAt", 0) < MAX_BLANK_AGE_S}


def session_file(thread_id, cwd):
    slug = "--" + cwd.strip("/").replace("/", "-") + "--"
    return os.path.join(BASE_DIR, "sessions", slug, "session-" + thread_id, "session.jsonl")


def register(thread_id, workspace_id, cwd):
    """Record the Codex thread identity; the sync daemon owns its log."""
    os.makedirs(BASE_DIR, exist_ok=True)
    with open(LOCK, "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        entries = load()
        created = time.time()
        entries[thread_id] = {
            "workspaceId": workspace_id, "cwd": cwd, "createdAt": created,
        }
        save(entries)


def materialize(thread_id, entry):
    """Create a blank projection only from the sync daemon's locked sweep."""
    cwd = entry["cwd"]
    target = session_file(thread_id, cwd)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if not os.path.exists(target):
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "session", "version": 0, "id": "session-" + thread_id,
                "createdAt": int(entry["createdAt"] * 1000), "cwd": cwd,
                "delegationDepth": 0, "agentPreset": "standard",
            }, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    register(sys.argv[1], sys.argv[2], sys.argv[3])
