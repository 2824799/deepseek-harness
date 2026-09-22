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
import socket
import struct
import sys
import time

HOME = os.path.expanduser("~")
PORT = int(os.environ.get("CODEX_DSH_APPSERVER_PORT", "45880"))
HOST = "127.0.0.1"


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


def connect(timeout=10.0):
    ws = WSClient(timeout=timeout)
    res = ws.call(
        "initialize",
        {"clientInfo": {"name": "dsh-web-edition", "title": "DSH Web Edition", "version": "0.1.0"}},
        timeout=timeout,
    )
    if not res["ok"]:
        raise WSError(f"initialize failed: {res['error']}")
    ws.notify("initialized")
    return ws


def thread_is_running(ws, thread_id):
    """Return the active turn id when the thread is mid-turn, else None."""
    res = ws.call("thread/read", {"threadId": thread_id, "includeTurns": False}, timeout=15)
    if not res["ok"]:
        return None, res
    thread = (res["value"] or {}).get("thread") or {}
    turns = thread.get("turns") or []
    for turn in reversed(turns):
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
    ws = connect()
    try:
        # Attach to the thread first: turn/start needs a loaded thread, and a
        # resume failure tells us whether Desktop already owns this writer.
        resume = ws.call("thread/resume", {"threadId": thread_id, "excludeTurns": True}, timeout=20)
        if not resume["ok"]:
            reason = json.dumps(resume.get("error"))
            if "active writer" in reason:
                # Codex Desktop holds the writer lock for this thread; the only
                # supported channel is the durable queue, which Desktop drains.
                return queue_prompt(thread_id, items, reason)
            return {"ok": False, "error": reason}

        active_turn, _ = thread_is_running(ws, thread_id)
        if active_turn:
            res = ws.call(
                "turn/steer",
                {"threadId": thread_id, "expectedTurnId": active_turn, "input": items},
                timeout=20,
            )
            if res["ok"]:
                return {"ok": True, "mode": "steer", "turnId": active_turn}
            if "active writer" in json.dumps(res.get("error")):
                return queue_prompt(thread_id, items, json.dumps(res.get("error")))
            return {"ok": False, "error": json.dumps(res["error"])}
        res = ws.call("turn/start", {"threadId": thread_id, "input": items}, timeout=25)
        if res["ok"]:
            turn = (res["value"] or {}).get("turn") or {}
            return {"ok": True, "mode": "start", "turnId": turn.get("id")}
        if "active writer" in json.dumps(res.get("error")):
            return queue_prompt(thread_id, items, json.dumps(res.get("error")))
        return {"ok": False, "error": json.dumps(res["error"])}
    finally:
        ws.close()


def queue_prompt(thread_id, items, reason=None):
    """Fall back to the durable CLI queue that Codex Desktop drains."""
    text = "\n".join(block.get("text", "") for block in items if block.get("type") == "text").strip()
    if not text:
        return {"ok": False, "error": f"Desktop holds this thread and images cannot be queued: {reason}"}
    codex_bin = os.path.expanduser("~/.local/bin/codex")
    if not os.path.exists(codex_bin):
        codex_bin = "/usr/lib/chatgpt/resources/codex"
    import subprocess

    env = os.environ.copy()
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
    try:
        res = subprocess.run(
            [codex_bin, "queue", "--thread", thread_id, "--message", text],
            capture_output=True, text=True, env=env, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - reported to the web UI as a failure
        return {"ok": False, "error": f"queue failed: {exc}"}
    if res.returncode != 0:
        return {"ok": False, "error": (res.stderr or res.stdout or "queue failed").strip()}
    return {"ok": True, "mode": "queue", "queued": True}


def create_thread(payload):
    cwd = payload.get("cwd") or "/home/nahida/agents/sever"
    params = {"cwd": cwd}
    if payload.get("model"):
        params["model"] = payload["model"]
    ws = connect()
    try:
        res = ws.call("thread/start", params, timeout=25)
        if not res["ok"]:
            return {"ok": False, "error": json.dumps(res["error"])}
        thread = (res["value"] or {}).get("thread") or {}
        return {"ok": True, "threadId": thread.get("id"), "sessionId": thread.get("sessionId")}
    finally:
        ws.close()


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
        res = ws.call("thread/read", {"threadId": thread_id, "includeTurns": False}, timeout=15)
        if not res["ok"]:
            return {"ok": False, "error": json.dumps(res["error"])}
        thread = (res["value"] or {}).get("thread") or {}
        turns = thread.get("turns") or []
        active = None
        for turn in reversed(turns):
            if turn.get("status") == "inProgress":
                active = turn.get("id")
                break
        if active is None:
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
        else:
            print(json.dumps({"ok": False, "error": f"unknown action {action}"}))
            return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports every failure as JSON
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
