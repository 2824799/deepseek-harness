import sqlite3
import json
import uuid
import time
import os
import datetime

def run():
    HOME = os.path.expanduser("~")
    CODEX_STATE = os.path.join(HOME, ".codex/state_5.sqlite")
    CODEX_HISTORY = os.path.join(HOME, ".codex/thread_history_1.sqlite")

    BASE_DIR = "/home/nahida/agents/sever/dsh/.dsh-codex"
    SESSIONS_ROOT = os.path.join(BASE_DIR, "sessions")
    STORAGES_ROOT = os.path.join(BASE_DIR, "storages")
    PROFILES_ROOT = os.path.join(BASE_DIR, "profiles/web")

    os.makedirs(SESSIONS_ROOT, exist_ok=True)
    os.makedirs(STORAGES_ROOT, exist_ok=True)
    os.makedirs(PROFILES_ROOT, exist_ok=True)

    state_conn = sqlite3.connect(CODEX_STATE)
    state_cur = state_conn.cursor()

    state_cur.execute("SELECT id, name FROM projects")
    db_projects = dict(state_cur.fetchall())

    # 读取 name 和 title
    state_cur.execute("""
        SELECT id, title, name, created_at, updated_at, cwd, project_id 
        FROM threads 
        WHERE archived = 0 
        ORDER BY updated_at DESC
    """)
    threads = state_cur.fetchall()

    hist_conn = sqlite3.connect(CODEX_HISTORY)
    hist_cur = hist_conn.cursor()

    groups = {}
    for th in threads:
        th_id, title, name, created_at, updated_at, cwd, project_id = th
        if not cwd:
            cwd = "/home/nahida/agents/sever"
        
        proj_name = db_projects.get(project_id)
        if not proj_name:
            proj_name = os.path.basename(cwd.rstrip("/")) or "home"
        
        if proj_name not in groups:
            groups[proj_name] = {
                "title": proj_name,
                "path": cwd,
                "threads": []
            }
        groups[proj_name]["threads"].append(th)

    workspace_table = {}
    workspace_ids = []
    proj_cache_sessions = {}
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    total_sessions = 0
    total_tool_calls = 0
    total_reasonings = 0

    for proj_name, ginfo in groups.items():
        cwd = ginfo["path"]
        ws_slug = "--" + cwd.strip("/").replace("/", "-") + "--"
        ws_dir = os.path.join(SESSIONS_ROOT, ws_slug)
        os.makedirs(ws_dir, exist_ok=True)

        ws_id = str(uuid.uuid5(uuid.NAMESPACE_URL, proj_name + cwd))
        workspace_ids.append(ws_id)
        
        sess_ids = []
        for th in ginfo["threads"]:
            th_id, title, name, created_at, updated_at, _, _ = th
            sess_dir_name = f"session-{th_id}"
            sess_ids.append(sess_dir_name)

            sess_path = os.path.join(ws_dir, sess_dir_name)
            os.makedirs(sess_path, exist_ok=True)
            jsonl_file = os.path.join(sess_path, "session.jsonl")

            hist_cur.execute("""
                SELECT item_id, item_type, rollout_ordinal, created_at_ms, item_json 
                FROM thread_items 
                WHERE thread_id = ? 
                ORDER BY rollout_ordinal ASC
            """, (th_id,))
            items = hist_cur.fetchall()
            if not items:
                continue

            # 优先使用 name（用户或系统赋予的精准会话名）
            clean_title = (name or title or "新对话").strip()
            if clean_title.startswith("# Files"):
                lines = [l.strip() for l in clean_title.splitlines() if l.strip() and not l.startswith("#")]
                clean_title = lines[-1] if lines else "任务详情"
            clean_title = clean_title.split("\n")[0].strip()
            if len(clean_title) > 40:
                clean_title = clean_title[:40] + "..."

            now_ms = int(time.time() * 1000)
            c_ms = int(created_at * 1000) if created_at else now_ms

            proj_cache_sessions[sess_dir_name] = {
                "identity": {
                    "createdAt": c_ms,
                    "cwd": cwd
                },
                "rows": {
                    "title": {
                        "ver": 1,
                        "seq": 3,
                        "val": clean_title
                    }
                }
            }

            header_event = {
                "type": "session",
                "version": 0,
                "id": sess_dir_name,
                "createdAt": c_ms,
                "cwd": cwd,
                "delegationDepth": 0,
                "agentPreset": "standard"
            }

            body_events = [
                {"type": "permission/preset", "time": now_ms, "data": {"preset": "danger-full-access"}},
                {"type": "sandbox/mode", "time": now_ms, "data": {"mode": "danger-full-access"}},
                {"type": "approval/policy", "time": now_ms, "data": {"policy": "never"}},
                {
                    "type": "session/title",
                    "time": now_ms,
                    "data": {
                        "title": clean_title,
                        "source": {"kind": "user"},
                        "messageSeqs": []
                    }
                }
            ]

            current_turn = 0
            current_step = 0
            step_open = False
            turn_open = False
            pending_reasoning = []

            def close_step(close_time):
                nonlocal current_turn, current_step, step_open, pending_reasoning
                if step_open:
                    if pending_reasoning:
                        r_text = "\n".join(pending_reasoning).strip()
                        body_events.append({
                            "type": "assistant/message",
                            "time": close_time,
                            "data": {
                                "turn": current_turn,
                                "step": current_step,
                                "stream": [],
                                "message": {
                                    "id": str(uuid.uuid4()),
                                    "role": "assistant",
                                    "content": [{"type": "reasoning", "text": r_text}],
                                    "source": {"kind": "model", "provider": "opencodex", "model": "A6-C/deepseek-v4.1-flash"}
                                }
                            },
                            "surfaceOp": "append"
                        })
                        pending_reasoning = []
                    body_events.append({
                        "type": "step/end",
                        "time": close_time,
                        "data": {"turn": current_turn, "step": current_step}
                    })
                    step_open = False

            def close_turn(close_time):
                nonlocal current_turn, turn_open
                if turn_open:
                    close_step(close_time)
                    body_events.append({
                        "type": "turn/end",
                        "time": close_time,
                        "data": {"turn": current_turn, "reason": {"kind": "completed"}}
                    })
                    turn_open = False

            for it in items:
                item_id, item_type, ord_val, created_ms, raw_json_str = it
                try:
                    data = json.loads(raw_json_str)
                except:
                    continue

                if item_type == "userMessage":
                    close_turn(created_ms)

                    current_turn += 1
                    current_step = 1
                    turn_open = True
                    step_open = True

                    text = ""
                    if isinstance(data.get("content"), list):
                        text = "\n".join([c.get("text", "") for c in data["content"] if isinstance(c, dict)])
                    elif isinstance(data.get("text"), str):
                        text = data["text"]

                    body_events.append({"type": "turn/start", "time": created_ms, "data": {"turn": current_turn}})
                    body_events.append({"type": "step/start", "time": created_ms, "data": {"turn": current_turn, "step": current_step}})
                    body_events.append({
                        "type": "user/message",
                        "time": created_ms,
                        "data": {
                            "content": [{"type": "text", "text": text}],
                            "source": {"kind": "user", "clientTimeZone": "Asia/Shanghai"},
                            "role": "user",
                            "id": item_id
                        },
                        "surfaceOp": "append"
                    })

                elif item_type == "reasoning":
                    total_reasonings += 1
                    r_text = ""
                    summary = data.get("summary")
                    if isinstance(summary, list):
                        r_text = "\n".join(str(s) for s in summary if s)
                    elif isinstance(summary, str):
                        r_text = summary
                    if not r_text:
                        content = data.get("content")
                        if isinstance(content, list):
                            r_text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                        elif isinstance(content, str):
                            r_text = content
                    if r_text.strip():
                        pending_reasoning.append(r_text.strip())

                elif item_type in ("commandExecution", "webSearch"):
                    total_tool_calls += 1
                    if not step_open:
                        current_step += 1
                        body_events.append({"type": "step/start", "time": created_ms, "data": {"turn": current_turn, "step": current_step}})
                        step_open = True

                    call_id = f"call-{uuid.uuid4()}"
                    if item_type == "commandExecution":
                        tool_name = "bash"
                        cmd = data.get("command", "")
                        out = data.get("aggregatedOutput") or ""
                        code = data.get("exitCode", 0)
                        args_raw = json.dumps({"command": cmd}, ensure_ascii=False)
                        is_err = (code != 0)
                        res_out = out
                    else:
                        tool_name = "web_search"
                        query = data.get("query", "")
                        args_raw = json.dumps({"queries": [query]}, ensure_ascii=False)
                        is_err = False
                        res_out = f"搜索内容: {query}"

                    content_blocks = []
                    if pending_reasoning:
                        content_blocks.append({"type": "reasoning", "text": "\n".join(pending_reasoning).strip()})
                        pending_reasoning = []
                    content_blocks.append({
                        "type": "tool-call",
                        "id": call_id,
                        "name": tool_name,
                        "arguments": args_raw
                    })

                    body_events.append({
                        "type": "assistant/message",
                        "time": created_ms,
                        "data": {
                            "turn": current_turn,
                            "step": current_step,
                            "stream": [],
                            "message": {
                                "id": str(uuid.uuid4()),
                                "role": "assistant",
                                "content": content_blocks,
                                "source": {"kind": "model", "provider": "opencodex", "model": "A6-C/deepseek-v4.1-flash"}
                            }
                        },
                        "surfaceOp": "append"
                    })

                    body_events.append({
                        "type": "tool/call",
                        "time": created_ms,
                        "data": {
                            "turn": current_turn,
                            "step": current_step,
                            "callId": call_id,
                            "name": tool_name,
                            "arguments": args_raw
                        }
                    })

                    body_events.append({
                        "type": "tool/result",
                        "time": created_ms,
                        "data": {
                            "turn": current_turn,
                            "step": current_step,
                            "message": {
                                "source": {"kind": "tool", "callId": call_id},
                                "content": [
                                    {
                                        "type": "tool-result",
                                        "toolCallId": call_id,
                                        "content": [{"type": "text", "text": res_out}],
                                        "isError": is_err
                                    }
                                ],
                                "role": "user",
                                "id": str(uuid.uuid4())
                            }
                        },
                        "surfaceOp": "append"
                    })

                    close_step(created_ms)

                elif item_type == "agentMessage":
                    text = data.get("text", "")
                    if text.strip() or pending_reasoning:
                        if not step_open:
                            current_step += 1
                            body_events.append({"type": "step/start", "time": created_ms, "data": {"turn": current_turn, "step": current_step}})
                            step_open = True

                        content_blocks = []
                        if pending_reasoning:
                            content_blocks.append({"type": "reasoning", "text": "\n".join(pending_reasoning).strip()})
                            pending_reasoning = []
                        if text.strip():
                            content_blocks.append({"type": "text", "text": text})

                        body_events.append({
                            "type": "assistant/message",
                            "time": created_ms,
                            "data": {
                                "turn": current_turn,
                                "step": current_step,
                                "stream": [],
                                "message": {
                                    "id": str(uuid.uuid4()),
                                    "role": "assistant",
                                    "content": content_blocks,
                                    "source": {"kind": "model", "provider": "opencodex", "model": "A6-C/deepseek-v4.1-flash"}
                                }
                            },
                            "surfaceOp": "append"
                        })
                        close_step(created_ms)

            close_turn(now_ms)

            for i, ev in enumerate(body_events):
                ev["seq"] = i

            with open(jsonl_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(header_event, ensure_ascii=False) + "\n")
                for ev in body_events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")

        workspace_table[ws_id] = {
            "path": cwd,
            "title": proj_name,
            "sessionIds": sess_ids,
            "createdAt": now_iso,
            "updatedAt": now_iso
        }

    ws_data = {
        "unit": {"name": "workspace", "version": 2},
        "global": {"initialized": True, "workspaceIds": workspace_ids, "archivedSessionIds": []},
        "tables": {"workspaces": workspace_table}
    }
    with open(os.path.join(STORAGES_ROOT, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump(ws_data, f, ensure_ascii=False, indent=2)

    proj_cache_data = {
        "unit": {"name": "session_projcache", "version": 3},
        "global": None,
        "tables": {"sessions": proj_cache_sessions}
    }
    with open(os.path.join(STORAGES_ROOT, "session_projcache.json"), "w", encoding="utf-8") as f:
        json.dump(proj_cache_data, f, ensure_ascii=False, indent=2)

    print(f"TITLE_NAME_SYNC_SUCCESS: current session title in cache: {proj_cache_sessions.get('session-01a0c26a-3e3f-7482-b6fc-933ad1c1b3bf', {}).get('rows', {}).get('title', {}).get('val')}")

if __name__ == '__main__':
    run()

