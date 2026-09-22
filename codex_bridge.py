#!/usr/bin/env python3
"""DSH web edition -> Codex bridge.

The web UI's session.prompt / rename / archive / create actions are routed here
by bridge_hook.js. Every action is executed through the Codex app-server
JSON-RPC channel (codex_link.py), which performs the work immediately and keeps
Codex Desktop and the web UI on one shared state instead of a parallel queue
table.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import codex_link  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def extract_thread_id(session_id):
    if session_id.startswith("session-"):
        return session_id[8:]
    return session_id


def refresh_sessions():
    """Best-effort re-projection so the web sidebar reflects the new state."""
    try:
        import subprocess

        subprocess.run(
            ["python3", os.path.join(HERE, "setup_isolated.py")],
            capture_output=True,
            timeout=30,
        )
    except Exception:
        pass


def send_prompt(session_id, payload):
    thread_id = extract_thread_id(session_id)
    result = codex_link.send_prompt(thread_id, payload)
    if result.get("ok"):
        refresh_sessions()
    return result


def archive_session(session_id):
    thread_id = extract_thread_id(session_id)
    result = codex_link.set_archived(thread_id, True)
    if result.get("ok"):
        refresh_sessions()
    return result


def rename_session(session_id, new_title):
    thread_id = extract_thread_id(session_id)
    result = codex_link.rename_thread(thread_id, new_title.strip())
    if result.get("ok"):
        refresh_sessions()
    return result


def create_session(workspace_path=None):
    result = codex_link.create_thread({"cwd": workspace_path or "/home/nahida/agents/sever"})
    if result.get("ok"):
        refresh_sessions()
    return result


def fork_session(session_id):
    thread_id = extract_thread_id(session_id)
    result = codex_link.fork_thread(thread_id)
    if result.get("ok"):
        refresh_sessions()
    return result


def cancel_session(session_id):
    thread_id = extract_thread_id(session_id)
    return codex_link.interrupt_turn(thread_id)


def main(argv):
    if len(argv) < 2:
        print(json.dumps({"ok": False, "error": "No action specified"}))
        return 1

    action = argv[1]
    try:
        if action == "prompt":
            payload = json.loads(argv[3]) if len(argv) > 3 else {}
            result = send_prompt(argv[2], payload)
        elif action == "archive":
            result = archive_session(argv[2])
        elif action == "rename":
            result = rename_session(argv[2], argv[3])
        elif action == "create":
            workspace = argv[2] if len(argv) > 2 and argv[2] != "None" else None
            result = create_session(workspace)
        elif action == "fork":
            result = fork_session(argv[2])
        elif action == "cancel":
            result = cancel_session(argv[2])
        elif action == "ping":
            probe = codex_link.WSClient()
            probe.close()
            result = {"ok": True}
        else:
            result = {"ok": False, "error": f"Unknown action {action}"}
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports failures as JSON
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
