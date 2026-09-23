"""Live-state bridge between Codex and the DSH web edition.

Two facts the web sidebar needs cannot come from the projected session logs:
which conversations are running right now, and which workspaces Codex still
knows about. Both are recomputed here on every sync pass and published as one
small JSON document the host process reads without touching Codex itself.

Running detection rides the rollout file: a live turn keeps appending events,
and every finished turn ends with task_complete or turn_aborted. A rollout
whose tail lacks a terminal event and was touched within the freshness window
is a running conversation. Desktop-owned threads never load into our
app-server, so thread/turns/list cannot see them; the disk can.
"""
import json
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DSH_HOME = os.environ.get("DSH_HOME") or os.path.join(HERE, ".dsh-codex")
LIVE_FILE = os.path.join(DSH_HOME, "live-state.json")
CODEX_STATE = os.path.join(os.environ.get("HOME", ""), ".codex", "state_5.sqlite")

# A turn appends rollout events continuously; once writes stop for this long
# without a terminal event the process is gone, not thinking.
FRESH_S = 45.0
TAIL_BYTES = 96 * 1024
TERMINAL = {("event_msg", "task_complete"), ("event_msg", "turn_aborted")}
START = ("event_msg", "task_started")


def _tail_terminal(path):
    """True when the most recent turn lifecycle event ended a turn."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return True
    if size == 0:
        return True
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, size - TAIL_BYTES))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return True
    last = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        payload = obj.get("payload") or {}
        kind = (obj.get("type"), payload.get("type"))
        if kind == START or kind in TERMINAL:
            last = kind
    return last in TERMINAL


def scan_running(now=None):
    """Map thread id -> True for conversations writing their rollout now."""
    now = time.time() if now is None else now
    running = {}
    try:
        conn = sqlite3.connect(f"file:{CODEX_STATE}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT id, rollout_path FROM threads WHERE archived = 0")
        rows = cur.fetchall()
        conn.close()
    except sqlite3.Error:
        return running
    for thread_id, rollout_path in rows:
        if not rollout_path:
            continue
        try:
            mtime = os.path.getmtime(rollout_path)
        except OSError:
            continue
        if now - mtime > FRESH_S:
            continue
        if _tail_terminal(rollout_path):
            continue
        running[thread_id] = True
    return running


def scan_projects():
    """Authoritative path -> project name map from the app-server.

    Returns None when the app-server is unreachable so the caller can keep
    the previous snapshot instead of dropping every workspace.
    """
    # The sync daemon sweeps every 700 ms; opening an app-server connection on
    # every pass would cost a WebSocket handshake per second for a list that
    # changes only when the user adds or removes a project.
    previous = read()
    fetched_at = previous.get("projectsAt") or 0
    if "projects" in previous and previous["projects"] is not None and time.time() * 1000 - fetched_at < 30000:
        return previous["projects"]
    try:
        import codex_link as link
        ws = link.connect(timeout=5, experimental=True)
        try:
            res = ws.call("project/list", {}, timeout=10)
        finally:
            ws.close()
    except Exception:
        return None
    if not res.get("ok"):
        return None
    projects = {}
    while True:
        value = res.get("value") or {}
        for entry in value.get("data") or []:
            name = entry.get("name")
            for root in entry.get("roots") or []:
                path = root.get("path") if isinstance(root, dict) else root
                if path and name:
                    projects[path] = name
        cursor = value.get("nextCursor")
        if not cursor:
            break
        try:
            ws = link.connect(timeout=5, experimental=True)
            try:
                res = ws.call("project/list", {"cursor": cursor}, timeout=10)
            finally:
                ws.close()
        except Exception:
            return None
        if not res.get("ok"):
            return None
    _PROJECTS_AT[0] = int(time.time() * 1000)
    return projects


_PROJECTS_AT = [0]


def publish(running, projects, titles=None):
    """Merge into live-state.json, keeping the last known project map on failure."""
    previous = read()
    if projects is None:
        projects = previous.get("projects")
    values = {
        "running": sorted(running),
        "projects": projects,
        "titles": titles if titles is not None else previous.get("titles") or {},
        "projectsAt": _PROJECTS_AT[0] or previous.get("projectsAt") or 0,
    }
    if all(previous.get(key) == value for key, value in values.items()):
        return previous
    doc = {**values, "updatedAt": int(time.time() * 1000)}
    tmp = LIVE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False)
    os.replace(tmp, LIVE_FILE)
    return doc


def read():
    try:
        with open(LIVE_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def scan():
    return publish(scan_running(), scan_projects())


if __name__ == "__main__":
    print(json.dumps(scan(), ensure_ascii=False)[:2000])
