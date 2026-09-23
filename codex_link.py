#!/usr/bin/env python3
"""Codex app-server client for the DSH web edition.

Talks to a long-lived `codex app-server --listen ws://127.0.0.1:PORT` over a
minimal WebSocket client, so the web UI can drive Codex directly instead of
writing rows into the CLI queue table.

CLI:
  codex_link.py ping
  codex_link.py list
  codex_link.py read <threadId>
  codex_link.py prompt <threadId> <jsonPayload>
  codex_link.py create <jsonPayload>
  codex_link.py rename <threadId> <title>
  codex_link.py archive <threadId>
  codex_link.py unarchive <threadId>
  codex_link.py fork <threadId>
"""

import base64
import json
import os
import sqlite3
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import codex_stream  # noqa: E402

HOME = os.path.expanduser("~")
PORT = int(os.environ.get("CODEX_DSH_APPSERVER_PORT", "45880"))
HOST = "127.0.0.1"
SELECTION_FILE = "/home/nahida/agents/sever/dsh/.dsh-codex/model-selection.json"
SETTINGS_FILE = "/home/nahida/agents/sever/dsh/.dsh-codex/settings.yaml"
PERMISSION_FILE = "/home/nahida/agents/sever/dsh/.dsh-codex/permission-preset.json"

# The web UI's permission presets bundle a sandbox mode with an approval
# policy. Codex takes the same pair per turn, so the mapping is direct; the
# ids match on both sides, which keeps the picker's label honest.
PERMISSION_POLICIES = {
    "read-only": ({"type": "readOnly"}, "on-request"),
    "workspace-write": ({"type": "workspaceWrite"}, "on-request"),
    "danger-full-access": ({"type": "dangerFullAccess"}, "never"),
}


def load_permissions():
    """Per-thread permission preset chosen in the web UI."""
    try:
        with open(PERMISSION_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_permission(thread_id, preset):
    """Remember a thread's permission preset so every later turn keeps it.

    turn/start carries sandboxPolicy and approvalPolicy and Codex applies them
    to that turn and the ones after it, but turn/steer accepts neither, so the
    choice has to be re-sent with each turn/start for the picker to stay
    authoritative.
    """
    if preset not in PERMISSION_POLICIES:
        return {"ok": False, "error": f"unknown permission preset {preset!r}"}
    permissions = load_permissions()
    permissions[thread_id] = preset
    try:
        os.makedirs(os.path.dirname(PERMISSION_FILE), exist_ok=True)
        tmp = PERMISSION_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(permissions, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, PERMISSION_FILE)
    except OSError as exc:
        return {"ok": False, "error": f"could not record the preset: {exc}"}
    return {"ok": True, "preset": preset}


def permission_override(thread_id):
    """The turn/start fields for this thread's preset, or nothing when unset."""
    preset = load_permissions().get(thread_id)
    policy = PERMISSION_POLICIES.get(preset)
    if policy is None:
        return {}
    sandbox, approval = policy
    return {"sandboxPolicy": sandbox, "approvalPolicy": approval}


def default_model():
    """The model the web UI shows for a new conversation.

    DSH reads this from its own settings, so a conversation created from the web
    must start on the same model or the label in the composer would describe a
    model Codex is not actually using.
    """
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    in_block = False
    for line in lines:
        if line.startswith("agent-default-model:"):
            in_block = True
            continue
        if in_block:
            if line and not line[0].isspace():
                break
            stripped = line.strip()
            if stripped.startswith("model:"):
                return stripped.split(":", 1)[1].strip().strip("'\"") or None
    return None


def load_selections():
    """Per-thread model choices made in the web UI."""
    try:
        with open(SELECTION_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_selection(thread_id, model, effort=None):
    """Remember a thread's model choice so later turns keep using it.

    Codex applies a model override to the turn it is sent with and to
    subsequent turns, but it has no standalone "set this thread's model"
    call, and turn/steer accepts no model at all. Persisting the choice and
    sending it on each turn/start is what makes the web picker authoritative.
    """
    selections = load_selections()
    entry = {"model": model, "selectedAt": int(time.time() * 1000)}
    if effort:
        entry["effort"] = effort
    selections[thread_id] = entry
    try:
        os.makedirs(os.path.dirname(SELECTION_FILE), exist_ok=True)
        tmp = SELECTION_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(selections, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, SELECTION_FILE)
    except OSError:
        pass
    return entry


def current_selection(thread_id):
    """The model this thread will use, without asking DSH to resume it.

    DSH answers session.models by resuming the session's agent, and in this
    edition the session store IS the projected log, so that read would append a
    second writer's events to it. The web UI only needs the label, which the
    recorded choice already carries.
    """
    selection = load_selections().get(thread_id) or {}
    # Desktop model changes bypass the web picker. The last turn_context is
    # Codex's durable record of the model and effort actually in use.
    actual = {}
    actual_at = 0
    try:
        db = sqlite3.connect(f"file:{HOME}/.codex/state_5.sqlite?mode=ro", uri=True)
        row = db.execute("SELECT rollout_path FROM threads WHERE id = ?", (thread_id,)).fetchone()
        db.close()
        if row and row[0]:
            with open(row[0], "rb") as handle:
                handle.seek(0, os.SEEK_END)
                offset = handle.tell()
                prefix = b""
                while offset and not actual:
                    count = min(offset, 256 * 1024)
                    offset -= count
                    handle.seek(offset)
                    lines = (handle.read(count) + prefix).split(b"\n")
                    prefix = lines.pop(0) if offset else b""
                    for line in reversed(lines):
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if event.get("type") == "turn_context":
                            actual = event.get("payload") or {}
                            stamp = event.get("timestamp") or ""
                            try:
                                import datetime
                                actual_at = int(datetime.datetime.fromisoformat(
                                    stamp.replace("Z", "+00:00")).timestamp() * 1000)
                            except (TypeError, ValueError):
                                pass
                            break
    except (OSError, sqlite3.Error):
        pass
    preferred = selection if selection.get("selectedAt", 0) > actual_at else actual
    fallback = actual if preferred is selection else selection
    return {
        "provider": "opencodex",
        "model": preferred.get("model") or fallback.get("model") or default_model(),
        **({"reasoningEffort": preferred.get("effort")}
           if preferred.get("effort") else {}),
    }


class WSError(Exception):
    pass


class WSClient:
    """Minimal RFC6455 text-frame client (masked client frames only)."""

    def __init__(self, host=HOST, port=PORT, timeout=10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("handshake closed")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n")[0]:
            raise WSError(f"handshake rejected: {resp.split(b'\r\n')[0]!r}")
        self.buf = b""
        self._next_id = 1
        self._pending = {}

    def send_text(self, text):
        data = text.encode()
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv_text(self, timeout=10.0):
        self.sock.settimeout(timeout)
        while True:
            while len(self.buf) >= 2:
                b0, b1 = self.buf[0], self.buf[1]
                opcode = b0 & 0x0F
                ln = b1 & 0x7F
                off = 2
                if ln == 126:
                    if len(self.buf) < 4:
                        break
                    ln = struct.unpack(">H", self.buf[2:4])[0]
                    off = 4
                elif ln == 127:
                    if len(self.buf) < 10:
                        break
                    ln = struct.unpack(">Q", self.buf[2:10])[0]
                    off = 10
                if len(self.buf) < off + ln:
                    break
                payload = self.buf[off:off + ln]
                self.buf = self.buf[off + ln:]
                if opcode == 0x1:
                    return payload.decode(errors="replace")
                if opcode == 0x8:
                    raise WSError("server closed")
                if opcode == 0x9:
                    continue
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("connection closed")
            self.buf += chunk

    def call(self, method, params=None, timeout=30.0):
        rid = self._next_id
        self._next_id += 1
        msg = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_text(json.dumps(msg))
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self.recv_text(timeout=max(0.5, min(5.0, end - time.time())))
            except (socket.timeout, TimeoutError):
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") != rid:
                continue
            if "error" in m:
                return {"ok": False, "error": m["error"]}
            return {"ok": True, "value": m.get("result")}
        return {"ok": False, "error": {"code": -32000, "message": f"{method} timed out"}}

    def notify(self, method, params=None):
        msg = {"method": method}
        if params is not None:
            msg["params"] = params
        self.send_text(json.dumps(msg))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def connect(timeout=10.0, experimental=False):
    ws = WSClient(timeout=timeout)
    params = {"clientInfo": {"name": "dsh-web-edition", "title": "DSH Web Edition", "version": "0.1.0"}}
    if experimental:
        # The app-server gates project/* behind the experimentalApi capability;
        # without it project/list answers -32600 "requires experimentalApi".
        params["capabilities"] = {"experimentalApi": True, "requestAttestation": False}
    res = ws.call("initialize", params, timeout=timeout)
    if not res["ok"]:
        raise WSError(f"initialize failed: {res['error']}")
    ws.notify("initialized")
    return ws


def thread_is_running(ws, thread_id):
    """Return the active turn id when the thread is mid-turn, else None."""
    # thread/read only fills its turns list for the resume/fork/read-with-turns
    # shapes, so the old includeTurns=False call always saw an empty list and
    # this check could never report a running turn. Hydrating turns from
    # thread/read is also deprecated and costs megabytes on a long thread, so
    # only the newest page of turn summaries is fetched.
    res = ws.call("thread/turns/list",
                  {"threadId": thread_id, "limit": 3, "itemsView": "notLoaded"},
                  timeout=15)
    if not res["ok"]:
        # An older app-server may not serve the paginated call; fall back to a
        # metadata read, which still reports the thread's own status.
        res = ws.call("thread/read", {"threadId": thread_id, "includeTurns": False},
                      timeout=15)
        if not res["ok"]:
            return None, res
        thread = (res["value"] or {}).get("thread") or {}
        return (thread.get("id") if thread.get("status") == "running" else None), res
    # The page is newest-first, and only the newest turn can still be running.
    turns = (res["value"] or {}).get("data") or []
    for turn in turns:
        if turn.get("status") == "inProgress":
            return turn.get("id"), res
    return None, res


def build_input(content):
    """Map DSH content blocks onto Codex UserInput blocks."""
    items = []
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            text = block.get("text") or ""
            if text:
                items.append({"type": "text", "text": text})
        elif kind == "image":
            data = block.get("data")
            if not data:
                continue
            media = block.get("mediaType") or "image/png"
            items.append({"type": "image", "url": f"data:{media};base64,{data}"})
        elif kind == "localImage":
            path = block.get("path")
            if path and os.path.exists(path):
                with open(path, "rb") as handle:
                    encoded = base64.b64encode(handle.read()).decode()
                items.append({"type": "image", "url": f"data:image/png;base64,{encoded}"})
    return items


def payload_to_content(payload):
    """Accept either DSH content blocks or the bridge's {text, images} form."""
    content = payload.get("content")
    if content:
        return build_input(content)
    items = []
    text = (payload.get("text") or "").strip()
    if text:
        items.append({"type": "text", "text": text})
    for path in payload.get("images") or []:
        if path and os.path.exists(path):
            with open(path, "rb") as handle:
                encoded = base64.b64encode(handle.read()).decode()
            items.append({"type": "image", "url": f"data:image/png;base64,{encoded}"})
    return items


def send_prompt(thread_id, payload):
    mode = payload.get("mode") or "queue"
    items = payload_to_content(payload)
    if not items:
        return {"ok": False, "error": "empty prompt"}
    selection = load_selections().get(thread_id) or {}
    model_override = {}
    if payload.get("model"):
        model_override["model"] = payload["model"]
    elif selection.get("model"):
        model_override["model"] = selection["model"]
    effort = payload.get("effort") or selection.get("effort")
    if effort:
        model_override["effort"] = effort
    # Codex only returns the model's reasoning text when a summary is asked
    # for; with the default ("none") the reasoning item arrives empty and the
    # web conversation shows no thinking. "detailed" is what surfaces it.
    summary = payload.get("summary")
    if summary is None:
        summary = selection.get("summary") or "detailed"
    if summary:
        model_override["summary"] = summary
    # The permission picker writes to its own file, so it is merged in beside
    # the model choice rather than replacing it: a turn carries both.
    model_override.update(permission_override(thread_id))
    ws = connect(experimental=True)
    try:
        # Attach to the thread first: turn/start needs a loaded thread, and a
        # resume failure tells us whether Desktop already owns this writer.
        resume = ws.call("thread/resume", {"threadId": thread_id, "excludeTurns": True}, timeout=20)
        if not resume["ok"]:
            reason = json.dumps(resume.get("error"))
            if "active writer" in reason:
                # Codex Desktop holds the writer lock for this thread; the only
                # supported channel is the durable queue, which Desktop drains.
                if mode == "steer":
                    return {"ok": False, "error": "Codex Desktop owns this turn; steering is unavailable from the web app-server"}
                return queue_prompt(thread_id, items, reason, payload.get("images") or [])
            if "no rollout found" not in reason and "thread not found" not in reason:
                return {"ok": False, "error": reason}
            # A thread created moments ago has no rollout file yet, so resume
            # has nothing to attach to. turn/start loads it itself, which is
            # exactly what the first message of a new conversation needs.
            # A blank web row whose thread was reaped before its first message
            # arrives here too: recreate it so the message still lands.
            if "thread not found" in reason:
                created = create_thread({"cwd": thread_cwd(thread_id)})
                if not created.get("ok"):
                    return {"ok": False, "error": reason}
                thread_id = created["threadId"]

        active_turn, _ = thread_is_running(ws, thread_id)
        if active_turn:
            if mode == "queue":
                return queue_prompt(thread_id, items, image_paths=payload.get("images") or [])
            # turn/steer takes no model, so a mid-turn message cannot switch it;
            # the choice is already recorded and applies from the next turn.
            res = ws.call(
                "turn/steer",
                {"threadId": thread_id, "expectedTurnId": active_turn, "input": items},
                timeout=20,
            )
            if res["ok"]:
                # A steer continues the turn that is already open, so the
                # streamer may fill into it immediately.
                stream = hand_off_stream(ws, thread_id, steer=True, turn_id=active_turn)
                return {"ok": True, "mode": "steer", "turnId": active_turn,
                        "stream": stream}
            if "active writer" in json.dumps(res.get("error")):
                return {"ok": False, "error": "Codex Desktop owns this turn; steering is unavailable from the web app-server"}
            return {"ok": False, "error": json.dumps(res["error"])}
        params = {"threadId": thread_id, "input": items}
        # Blank threads are absent from the durable Workspace registry until
        # their first turn. Carry the picked cwd into that first turn so Codex
        # does not persist the app-server's fallback working directory.
        picked_cwd = pending_thread_cwd(thread_id)
        if picked_cwd:
            params["cwd"] = picked_cwd
            params["runtimeWorkspaceRoots"] = [picked_cwd]
            params["environments"] = [{"environmentId": "local", "cwd": picked_cwd,
                                        "runtimeWorkspaceRoots": [picked_cwd]}]
        params.update(model_override)
        res = ws.call("turn/start", params, timeout=25)
        if res["ok"]:
            turn = (res["value"] or {}).get("turn") or {}
            stream = hand_off_stream(ws, thread_id, steer=False,
                                     turn_id=turn.get("id"))
            return {"ok": True, "mode": "start", "turnId": turn.get("id"),
                    "stream": stream}
        if "active writer" in json.dumps(res.get("error")):
            if mode == "steer":
                return {"ok": False, "error": "Codex Desktop owns this turn; steering is unavailable from the web app-server"}
            return queue_prompt(thread_id, items, json.dumps(res.get("error")), payload.get("images") or [])
        return {"ok": False, "error": json.dumps(res["error"])}
    finally:
        ws.close()


def hand_off_stream(ws, thread_id, steer, turn_id=None):
    """Give this connection's socket to a detached child that follows the turn.

    Codex only emits token deltas on the connection that submitted the turn, so
    the streaming child inherits this socket instead of opening its own. The
    parent still closes its copy, which is safe: the child holds a duplicate
    descriptor and the kernel keeps the connection alive until it is closed.
    """
    session_id = "session-" + thread_id
    tail = codex_stream.read_tail(session_id)
    current = (tail or {}).get("turn") or 0
    # A fresh turn has not reached the log yet, so the child waits for the turn
    # counter to advance. A steer lands inside the turn already open.
    base_turn = current - 1 if steer else current
    pid = codex_stream.spawn_streamer(ws, thread_id, session_id, base_turn, turn_id)
    if pid is None:
        return {"ok": False, "reason": "fork failed"}
    return {"ok": True, "pid": pid, "baseTurn": base_turn}


def queue_prompt(thread_id, items, reason=None, image_paths=None):
    """Fall back to the durable CLI queue that Codex Desktop drains."""
    text = "\n".join(block.get("text", "") for block in items if block.get("type") == "text").strip()
    image_paths = [path for path in image_paths or [] if os.path.isfile(path)]
    if not text and not image_paths:
        return {"ok": False, "error": f"queue requires text or an image: {reason}"}
    codex_bin = os.path.expanduser("~/.local/bin/codex")
    if not os.path.exists(codex_bin):
        codex_bin = "/usr/lib/chatgpt/resources/codex"
    import subprocess

    env = os.environ.copy()
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
    try:
        command = [codex_bin, "queue", "--thread", thread_id, "--message", text]
        selected_model = (load_selections().get(thread_id) or {}).get("model")
        if selected_model:
            command.extend(["-m", selected_model])
        for image_path in image_paths:
            command.extend(["-i", image_path])
        res = subprocess.run(
            command,
            capture_output=True, text=True, env=env, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - reported to the web UI as a failure
        return {"ok": False, "error": f"queue failed: {exc}"}
    if res.returncode != 0:
        return {"ok": False, "error": (res.stderr or res.stdout or "queue failed").strip()}
    return {"ok": True, "mode": "queue", "queued": True}


def create_thread(payload):
    cwd = payload.get("cwd") or "/home/nahida/agents/sever"
    params = {
        "cwd": cwd,
        "runtimeWorkspaceRoots": [cwd],
        "environments": [{"environmentId": "local", "cwd": cwd,
                          "runtimeWorkspaceRoots": [cwd]}],
    }
    model = payload.get("model") or default_model()
    if model:
        params["model"] = model
    ws = connect(experimental=True)
    try:
        res = ws.call("thread/start", params, timeout=25)
        if not res["ok"]:
            return {"ok": False, "error": json.dumps(res["error"])}
        thread = (res["value"] or {}).get("thread") or {}
        thread_id = thread.get("id")
        if thread_id and model:
            # New conversations have no prior turn to carry the override, so the
            # choice is recorded for the first turn/start to pick up.
            save_selection(thread_id, model)
        return {"ok": True, "threadId": thread_id, "sessionId": thread.get("sessionId")}
    finally:
        ws.close()


def thread_cwd(thread_id):
    """The working directory a projected session belongs to, from the registry."""
    picked = pending_thread_cwd(thread_id)
    if picked:
        return picked
    session_id = thread_id if thread_id.startswith("session-") else "session-" + thread_id
    try:
        with open(os.path.join(os.path.dirname(SELECTION_FILE), "storages", "workspace.json"),
                  encoding="utf-8") as handle:
            tables = (json.load(handle).get("tables", {}).get("workspaces") or {})
    except Exception:
        return None
    for entry in tables.values():
        if session_id in (entry.get("sessionIds") or []):
            return entry.get("path")
    return None


def pending_thread_cwd(thread_id):
    """The selected Workspace of a blank web thread awaiting its first turn."""
    try:
        with open(os.path.join(os.path.dirname(SELECTION_FILE), "pending-threads.json"),
                  encoding="utf-8") as handle:
            entry = json.load(handle).get(thread_id) or {}
        return entry.get("cwd")
    except (OSError, ValueError):
        return None


def rename_thread(thread_id, title):
    ws = connect()
    try:
        res = ws.call("thread/name/set", {"threadId": thread_id, "name": title}, timeout=15)
        return {"ok": res["ok"], "error": None if res["ok"] else json.dumps(res["error"])}
    finally:
        ws.close()


def set_archived(thread_id, archived):
    ws = connect()
    try:
        method = "thread/archive" if archived else "thread/unarchive"
        res = ws.call(method, {"threadId": thread_id}, timeout=15)
        if res["ok"]:
            return {"ok": True}
        reason = json.dumps(res.get("error"))
    finally:
        ws.close()
    if "active writer" not in reason:
        return {"ok": False, "error": reason}
    # Codex Desktop keeps a writer lock on every thread it has open, and both
    # the app-server and the CLI refuse to archive those. Record the intent in
    # the same table the CLI uses so it still takes effect and survives.
    import sqlite3
    import time

    state_db = os.path.join(HOME, ".codex", "state_5.sqlite")
    try:
        conn = sqlite3.connect(state_db, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "UPDATE threads SET archived = ?, archived_at = ? WHERE id = ?",
            (1 if archived else 0, int(time.time()) if archived else None, thread_id),
        )
        if cur.rowcount != 1:
            conn.close()
            return {"ok": False, "error": f"thread {thread_id} no longer exists"}
        conn.commit()
        conn.close()
        return {"ok": True, "fallback": "state_5.sqlite", "reason": reason}
    except Exception as exc:  # noqa: BLE001 - reported to the web UI as a failure
        return {"ok": False, "error": f"{reason}; fallback failed: {exc}"}


def fork_thread(thread_id):
    ws = connect()
    try:
        res = ws.call("thread/fork", {"threadId": thread_id}, timeout=25)
        if not res["ok"]:
            return {"ok": False, "error": json.dumps(res["error"])}
        thread = (res["value"] or {}).get("thread") or {}
        return {"ok": True, "threadId": thread.get("id")}
    finally:
        ws.close()


def interrupt_turn(thread_id):
    """Stop the thread's in-flight turn (the web UI's stop button)."""
    ws = connect()
    try:
        active, lookup = thread_is_running(ws, thread_id)
        if not lookup["ok"]:
            return {"ok": False, "error": json.dumps(lookup["error"])}
        if active is None:
            # Desktop-owned turns are absent from this separate app-server's
            # loaded-turn list. Do not acknowledge a stop we cannot perform.
            import codex_live
            if thread_id in codex_live.scan_running():
                return {"ok": False, "error": "This Codex Desktop turn cannot be interrupted through the web app-server"}
            return {"ok": True, "interrupted": False, "reason": "no active turn"}
        res = ws.call("turn/interrupt", {"threadId": thread_id, "turnId": active}, timeout=15)
        return {"ok": res["ok"], "interrupted": res["ok"],
                "error": None if res["ok"] else json.dumps(res["error"])}
    finally:
        ws.close()
def main(argv):
    if len(argv) < 2:
        print(json.dumps({"ok": False, "error": "no action"}))
        return 1
    action = argv[1]
    try:
        if action == "ping":
            ws = connect()
            ws.close()
            print(json.dumps({"ok": True, "port": PORT}))
        elif action == "list":
            ws = connect()
            res = ws.call("thread/list", {}, timeout=20)
            ws.close()
            print(json.dumps(res))
        elif action == "read":
            ws = connect()
            res = ws.call("thread/read", {"threadId": argv[2], "includeTurns": False}, timeout=20)
            ws.close()
            print(json.dumps(res))
        elif action == "prompt":
            payload = json.loads(argv[3]) if len(argv) > 3 else {}
            print(json.dumps(send_prompt(argv[2], payload)))
        elif action == "create":
            payload = json.loads(argv[2]) if len(argv) > 2 else {}
            print(json.dumps(create_thread(payload)))
        elif action == "rename":
            print(json.dumps(rename_thread(argv[2], argv[3])))
        elif action == "archive":
            print(json.dumps(set_archived(argv[2], True)))
        elif action == "unarchive":
            print(json.dumps(set_archived(argv[2], False)))
        elif action == "fork":
            print(json.dumps(fork_thread(argv[2])))
        elif action == "model":
            effort = argv[4] if len(argv) > 4 and argv[4] else None
            print(json.dumps({"ok": True,
                              **save_selection(argv[2], argv[3], effort)}))
        elif action == "permission":
            print(json.dumps(save_permission(argv[2], argv[3])))
        else:
            print(json.dumps({"ok": False, "error": f"unknown action {action}"}))
            return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports every failure as JSON
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
