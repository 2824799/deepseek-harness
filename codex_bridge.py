#!/usr/bin/env python3
import sys
import os
import json
import sqlite3
import subprocess
import time
import uuid

HOME = os.path.expanduser("~")
STATE_DB = os.path.join(HOME, ".codex/state_5.sqlite")
CODEX_BIN = os.path.expanduser("~/.local/bin/codex")
if not os.path.exists(CODEX_BIN):
    CODEX_BIN = "/usr/lib/chatgpt/resources/codex"

def extract_thread_id(session_id: str) -> str:
    if session_id.startswith("session-"):
        return session_id[8:]
    return session_id

def send_prompt(session_id: str, text: str, image_paths: list = None, mode: str = "followup"):
    thread_id = extract_thread_id(session_id)
    cmd = [CODEX_BIN, "queue", "--thread", thread_id, "--message", text]
    if image_paths:
        for p in image_paths:
            if os.path.exists(p):
                cmd.extend(["-i", p])
    
    # Run queue command
    env = os.environ.copy()
    env["PATH"] = f"{HOME}/.local/bin:{env.get('PATH', '')}"
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        return {
            "ok": False,
            "error": res.stderr.strip() or res.stdout.strip() or "codex queue failed"
        }
    
    try:
        subprocess.run(["python3", "/home/nahida/agents/sever/dsh/setup_isolated.py"], capture_output=True)
    except:
        pass

    return {
        "ok": True,
        "output": res.stdout.strip()
    }

def archive_session(session_id: str):
    thread_id = extract_thread_id(session_id)
    cmd = [CODEX_BIN, "archive", thread_id]
    env = os.environ.copy()
    env["PATH"] = f"{HOME}/.local/bin:{env.get('PATH', '')}"
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    try:
        subprocess.run(["python3", "/home/nahida/agents/sever/dsh/setup_isolated.py"], capture_output=True)
    except:
        pass
    return {"ok": res.returncode == 0, "output": res.stdout.strip()}

def rename_session(session_id: str, new_title: str):
    thread_id = extract_thread_id(session_id)
    new_title = new_title.strip()
    try:
        conn = sqlite3.connect(STATE_DB)
        cur = conn.cursor()
        cur.execute("UPDATE threads SET name = ?, title = ?, updated_at = ? WHERE id = ?", 
                    (new_title, new_title, int(time.time()), thread_id))
        conn.commit()
        conn.close()
        subprocess.run(["python3", "/home/nahida/agents/sever/dsh/setup_isolated.py"], capture_output=True)
        return {"ok": True, "title": new_title}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def create_session(workspace_path: str = None, requested_preset: str = None):
    new_id = str(uuid.uuid4())
    now_sec = int(time.time())
    now_ms = int(time.time() * 1000)
    cwd = workspace_path or "/home/nahida/agents/sever"
    rollout_date = time.strftime("%Y/%m/%d")
    rollout_time = time.strftime("%Y-%m-%dT%H-%M-%S")
    rollout_dir = os.path.join(HOME, f".codex/sessions/{rollout_date}")
    os.makedirs(rollout_dir, exist_ok=True)
    rollout_path = os.path.join(rollout_dir, f"rollout-{rollout_time}-{new_id}.jsonl")
    
    with open(rollout_path, "w", encoding="utf-8") as f:
        f.write("")

    try:
        conn = sqlite3.connect(STATE_DB)
        cur = conn.cursor()
        cur.execute("SELECT id FROM projects WHERE path = ? LIMIT 1", (cwd,))
        row = cur.fetchone()
        project_id = row[0] if row else None

        cur.execute("""
            INSERT INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider, 
                cwd, title, sandbox_policy, approval_mode, model, name, project_id,
                created_at_ms, updated_at_ms, recency_at, recency_at_ms, first_user_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            new_id, rollout_path, now_sec, now_sec, "vscode", "opencodex_retry",
            cwd, "新会话", '{"type":"disabled"}', "never", "A6-C/deepseek-v4.1-flash", "新会话", project_id,
            now_ms, now_ms, now_sec, now_ms, ""
        ))
        conn.commit()
        conn.close()

        subprocess.run(["python3", "/home/nahida/agents/sever/dsh/setup_isolated.py"], capture_output=True)
        return {"ok": True, "sessionId": f"session-{new_id}", "threadId": new_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No action specified"}))
        sys.exit(1)
    
    action = sys.argv[1]
    if action == "prompt":
        sess = sys.argv[2]
        payload = json.loads(sys.argv[3])
        txt = payload.get("text", "")
        imgs = payload.get("images", [])
        mode = payload.get("mode", "followup")
        res = send_prompt(sess, txt, imgs, mode)
        print(json.dumps(res))
    elif action == "archive":
        sess = sys.argv[2]
        res = archive_session(sess)
        print(json.dumps(res))
    elif action == "rename":
        sess = sys.argv[2]
        title = sys.argv[3]
        res = rename_session(sess, title)
        print(json.dumps(res))
    elif action == "create":
        ws = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "None" else None
        res = create_session(ws)
        print(json.dumps(res))
    else:
        print(json.dumps({"error": f"Unknown action {action}"}))

