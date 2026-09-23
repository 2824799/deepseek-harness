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

def extract_thread_id(session_id):
    if session_id.startswith("session-"):
        return session_id[8:]
    return session_id


def send_prompt(session_id, payload):
    thread_id = extract_thread_id(session_id)
    # The permission picker is a slash command in this UI, so it arrives on the
    # prompt channel. It is a Codex setting rather than something to say to the
    # model, and answering it here is what stops the command text from being
    # sent to Codex as a message.
    text = (payload.get("text") or "").strip()
    if text.startswith("/permission"):
        parts = text.split()
        if len(parts) < 2:
            return {"ok": False, "error": "usage: /permission <preset>"}
        return set_permission(session_id, parts[1])
    return codex_link.send_prompt(thread_id, payload)


def archive_session(session_id):
    thread_id = extract_thread_id(session_id)
    return codex_link.set_archived(thread_id, True)


def rename_session(session_id, new_title):
    thread_id = extract_thread_id(session_id)
    return codex_link.rename_thread(thread_id, new_title.strip())


def create_session(workspace_path=None):
    return codex_link.create_thread({"cwd": workspace_path or "/home/nahida/agents/sever"})


def fork_session(session_id):
    thread_id = extract_thread_id(session_id)
    return codex_link.fork_thread(thread_id)


def cancel_session(session_id):
    thread_id = extract_thread_id(session_id)
    return codex_link.interrupt_turn(thread_id)


def set_model(session_id, model, effort=None):
    """Record the web UI's model choice for this thread.

    Codex has no standalone "set this thread's model" call, so the choice is
    stored and attached to every later turn/start.
    """
    thread_id = extract_thread_id(session_id)
    return {"ok": True, **codex_link.save_selection(thread_id, model, effort or None)}


def set_permission(session_id, preset):
    """Record the web UI's permission preset for this thread.

    Codex takes the sandbox and approval pair per turn, so the preset is stored
    and merged into every later turn/start alongside the model choice.
    """
    thread_id = extract_thread_id(session_id)
    return codex_link.save_permission(thread_id, preset)


def model_state(session_id):
    """The model label for this thread without resuming it through DSH."""
    thread_id = extract_thread_id(session_id)
    return {"ok": True, "current": codex_link.current_selection(thread_id)}


def model_catalog():
    """Codex's supported effort levels and defaults for the web model picker."""
    ws = codex_link.connect(timeout=5)
    models = {}
    cursor = None
    try:
        while True:
            response = ws.call("model/list", {"cursor": cursor} if cursor else {}, timeout=10)
            if not response.get("ok"):
                return response
            value = response.get("value") or {}
            for entry in value.get("data") or []:
                levels = entry.get("supportedReasoningEfforts") or []
                models[entry.get("model") or entry.get("id")] = {
                    "name": entry.get("displayName") or entry.get("model") or entry.get("id"),
                    "description": entry.get("description"),
                    "efforts": [{"id": level["reasoningEffort"],
                                 "name": level["reasoningEffort"]}
                                for level in levels if level.get("reasoningEffort")],
                    "defaultEffort": entry.get("defaultReasoningEffort"),
                }
            cursor = value.get("nextCursor")
            if not cursor:
                return {"ok": True, "models": models}
    finally:
        ws.close()


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
        elif action == "model":
            result = set_model(argv[2], argv[3], argv[4] if len(argv) > 4 else None)
        elif action == "permission":
            result = set_permission(argv[2], argv[3])
        elif action == "model-state":
            result = model_state(argv[2])
        elif action == "model-catalog":
            result = model_catalog()
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
