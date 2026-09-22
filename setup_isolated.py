#!/usr/bin/env python3
"""Project Codex threads into the DSH web edition's session logs.

Runs incrementally: each pass reads only the thread_items appended since the
last pass and appends the matching DSH events, so the browser follows Codex
within one poll interval instead of waiting for a whole-file rebuild.

Codex keeps the numbers the conversation footer needs (cache-hit share,
first-token latency, output throughput) in the rollout file rather than in
thread_items, so each pass also reads the rollout and joins the two on
rollout_ordinal / turn_id.
"""

import datetime
import fcntl
import json
import os
import sqlite3
import time
import uuid

import codex_stream

HOME = os.path.expanduser("~")
CODEX_STATE = os.path.join(HOME, ".codex/state_5.sqlite")
CODEX_HISTORY = os.path.join(HOME, ".codex/thread_history_1.sqlite")

BASE_DIR = "/home/nahida/agents/sever/dsh/.dsh-codex"
SESSIONS_ROOT = os.path.join(BASE_DIR, "sessions")
STORAGES_ROOT = os.path.join(BASE_DIR, "storages")
STATE_FILE = os.path.join(BASE_DIR, "projection-state.json")
PERMISSION_FILE = os.path.join(BASE_DIR, "permission-preset.json")

# Bumped whenever this projector changes how it renders a Codex item. A stored
# session from an older version is rebuilt from its rollout instead of being
# skipped, because its log is missing whatever the newer rendering adds (for
# example attached images, or reasoning text that used to be dropped).
PROJECTION_VERSION = 2

# The picker's presets bundle a sandbox mode with an approval policy, and the
# projector mirrors the chosen bundle back into the log so the picker shows
# what Codex is actually running under.
PERMISSION_BUNDLES = {
    "read-only": ("read-only", "ask"),
    "workspace-write": ("workspace-write", "ask"),
    "danger-full-access": ("danger-full-access", "never"),
}


def load_permission_presets():
    """Thread id -> permission preset chosen in the web UI."""
    try:
        with open(PERMISSION_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

MODEL_SOURCE = {
    "kind": "model",
    "provider": "opencodex",
    "model": "A6-C/deepseek-v4.1-flash",
}

NEWLINE = chr(10)


def build_rollout_index():
    """Map every thread id to its rollout files across all date directories.

    A long-lived thread keeps its older rollouts in earlier date directories,
    so the index is built once per pass rather than per thread.
    """
    index = {}
    roots = [os.path.join(HOME, ".codex", "sessions"),
             os.path.join(HOME, ".codex", "archived_sessions")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if not name.startswith("rollout-") or not name.endswith(".jsonl"):
                        continue
                    stem = name[:-len(".jsonl")]
                    if "_" in stem:
                        stem = stem.split("_", 1)[0]
                    marker = stem.rsplit("-", 1)[-1]
                    index.setdefault(marker, []).append(os.path.join(dirpath, name))
        except OSError:
            continue
    return index


def read_rollout_meta(rollout_path, rollout_index=None):
    """Per-turn timing and per-ordinal usage recorded by Codex.

    Codex writes first-token latency and turn duration to the rollout rather
    than to thread_items, so the projection joins the two on turn_id and
    rollout_ordinal.
    """
    timings = {}
    usage_records = []
    tool_ms = {}
    # Codex rotates a thread's rollout on compaction, so a thread's earlier
    # turns live in sibling files beside the current one. Read them all, or
    # the earlier turns report no usage.
    candidates = []
    if rollout_path:
        candidates.append(rollout_path)
        base = os.path.basename(rollout_path)
        # "rollout-<ts>-<threadId>.jsonl" / "...-<threadId>_<suffix>.jsonl"
        thread_marker = base.rsplit("-", 1)[-1].split(".")[0]
        if "_" in base:
            thread_marker = base.split("_", 1)[0].rsplit("-", 1)[-1]
        for candidate in (rollout_index or {}).get(thread_marker, []):
            if candidate not in candidates:
                candidates.append(candidate)
    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    kind = payload.get("type")
                    turn_id = payload.get("turn_id")
                    if kind == "task_complete" and turn_id:
                        started = payload.get("started_at")
                        timings[turn_id] = {
                            "startMs": int(started) * 1000 if started else None,
                            "durationMs": payload.get("duration_ms"),
                            "ttftMs": payload.get("time_to_first_token_ms"),
                        }
                    elif record.get("type") == "token_usage_record":
                        # One record per model request, each landing on its own
                        # rollout line with no matching thread_item. They are
                        # collected with their ordinal and attributed to the
                        # item that preceded them, so every request is counted
                        # exactly once.
                        ordinal = record.get("ordinal")
                        usage = payload.get("usage")
                        if isinstance(ordinal, int) and isinstance(usage, dict):
                            usage_records.append((ordinal, usage))
                    elif kind == "item_completed":
                        # Codex reports whole-turn wall time, which includes
                        # tool execution. The footer shows tool time on its own
                        # line, so the model time this feeds is the turn's
                        # duration minus its tool calls.
                        item = payload.get("item")
                        if (isinstance(item, dict) and item.get("type") == "CommandExecution"
                                and turn_id):
                            started_ms = payload.get("started_at_ms")
                            completed_ms = payload.get("completed_at_ms")
                            if started_ms and completed_ms and completed_ms > started_ms:
                                tool_ms[turn_id] = tool_ms.get(turn_id, 0) + (completed_ms - started_ms)
        except Exception:
            continue
    for turn, ms in tool_ms.items():
        timings.setdefault(turn, {})["toolMs"] = ms
    return timings, usage_records


def attribute_usage(ordinal_to_turn, usage_records):
    """Total each turn's billed usage.

    thread_items carry a subset of the rollout's ordinals and each
    token_usage_record sits just after the item it billed, so a record belongs
    to the greatest item ordinal not above its own. Records are summed per
    turn: one turn issues many requests, and counting only the last would both
    under-report the turn and, if attached to every step, multiply the total.
    """
    totals = {}
    if not ordinal_to_turn:
        return totals
    import bisect
    ordered = sorted(ordinal_to_turn)
    for ordinal, usage in usage_records:
        index = bisect.bisect_right(ordered, ordinal) - 1
        if index < 0:
            continue
        turn = ordinal_to_turn[ordered[index]]
        bucket = totals.setdefault(turn, {
            "inputTokens": 0, "outputTokens": 0,
            "cacheReadTokens": 0, "cacheWriteTokens": 0,
        })
        bucket["inputTokens"] += int(usage.get("input_tokens") or 0)
        bucket["outputTokens"] += int(usage.get("output_tokens") or 0)
        bucket["cacheReadTokens"] += int(usage.get("cached_input_tokens") or 0)
        bucket["cacheWriteTokens"] += int(usage.get("cache_write_input_tokens") or 0)
    return totals


def clean_title(name, title):
    text = (name or title or "新对话").strip()
    if text.startswith("# Files"):
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        text = lines[-1] if lines else "任务详情"
    text = text.split(NEWLINE)[0].strip()
    if len(text) > 40:
        text = text[:40] + "..."
    return text


def emit(cx, out, event):
    cx["seq"] += 1
    event["seq"] = cx["seq"]
    out.append(event)


def _scan_log(path):
    """Read a log's tail through the streamer's scanner.

    Both writers must agree on what the log says, so the fold lives in one place
    and this is only the file-handling half of it.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return codex_stream._scan(fh)


def append_locked(path, events):
    """Append events under the log's exclusive lock, renumbering from its tail.

    The streaming child writes to this same file, so the projector cannot trust
    the sequence numbers it computed in memory: it takes the lock, re-reads the
    tail, and continues from whatever the streamer last wrote. Without this the
    two writers would interleave duplicate or regressing seqs and the browser
    would reject the whole log.
    """
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            state = _scan_log(path)
            if state["tornAt"] is not None:
                fh.truncate(state["tornAt"])
            seq = state["maxSeq"]
            for event in events:
                seq += 1
                event["seq"] = seq
            fh.seek(0, os.SEEK_END)
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + NEWLINE)
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def streamed_step(path, turn):
    """The open step when the streamer has already filled it with this turn's deltas.

    The projector finalizes the assistant message into that step rather than
    opening a fresh one: a new step would render the same answer a second time
    and leave the streamed step showing as still running.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            state = _scan_log(path)
    except OSError:
        return None
    if not state or not state.get("stepOpen") or not state.get("stepHasChunks"):
        return None
    if state.get("turn") != turn:
        return None
    return state.get("step")


def run():
    os.makedirs(SESSIONS_ROOT, exist_ok=True)
    os.makedirs(STORAGES_ROOT, exist_ok=True)

    # The polling daemon and the bridge's post-action refresh both project, and
    # each reads its state at the start and writes it at the end. Overlapping
    # passes therefore start from the same cursor and append the same events
    # twice, so one whole pass is serialized behind the other.
    lock_path = os.path.join(BASE_DIR, "projection.lock")
    lock_fh = open(lock_path, "w", encoding="utf-8")
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            states = json.load(handle)
    except Exception:
        states = {}

    permission_presets = load_permission_presets()

    state_conn = sqlite3.connect(CODEX_STATE)
    state_cur = state_conn.cursor()
    state_cur.execute("SELECT id, name FROM projects")
    db_projects = dict(state_cur.fetchall())
    state_cur.execute("""
        SELECT id, title, name, created_at, updated_at, cwd, project_id, rollout_path
        FROM threads
        WHERE archived = 0
        ORDER BY updated_at DESC
    """)
    threads = state_cur.fetchall()
    # Archived threads are excluded from the listing above, so the only way the
    # web UI can tell they were archived is this set. Leaving it empty made an
    # archived conversation keep showing up in the sidebar.
    state_cur.execute("SELECT id FROM threads WHERE archived = 1")
    archived_session_ids = ["session-" + row[0] for row in state_cur.fetchall()]
    state_conn.close()

    hist_conn = sqlite3.connect(CODEX_HISTORY)
    hist_cur = hist_conn.cursor()
    rollout_index = build_rollout_index()
    # Cheap change detection: a thread whose newest item and item count are
    # unchanged since the last pass cannot produce new events, so it is skipped
    # entirely. Without this every pass re-read the whole history.
    hist_cur.execute("SELECT thread_id, MAX(rollout_ordinal), COUNT(*) FROM thread_items GROUP BY thread_id")
    item_marks = {row[0]: (row[1], row[2]) for row in hist_cur.fetchall()}

    groups = {}
    for th in threads:
        th_id, title, name, created_at, updated_at, cwd, project_id, rollout_path = th
        if not cwd:
            cwd = "/home/nahida/agents/sever"
        proj_name = db_projects.get(project_id) or os.path.basename(cwd.rstrip("/")) or "home"
        groups.setdefault(proj_name, {"title": proj_name, "path": cwd, "threads": []})
        groups[proj_name]["threads"].append(th)

    workspace_table = {}
    workspace_ids = []
    proj_cache_sessions = {}
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    for proj_name, ginfo in groups.items():
        cwd = ginfo["path"]
        ws_slug = "--" + cwd.strip("/").replace("/", "-") + "--"
        ws_dir = os.path.join(SESSIONS_ROOT, ws_slug)
        os.makedirs(ws_dir, exist_ok=True)
        ws_id = str(uuid.uuid5(uuid.NAMESPACE_URL, proj_name + cwd))
        workspace_ids.append(ws_id)

        sess_ids = []
        for th in ginfo["threads"]:
            th_id, title, name, created_at, updated_at, _, _, rollout_path = th
            sess_dir_name = "session-" + th_id
            sess_path = os.path.join(ws_dir, sess_dir_name)
            os.makedirs(sess_path, exist_ok=True)
            jsonl_file = os.path.join(sess_path, "session.jsonl")

            label = clean_title(name, title)
            c_ms = int(created_at * 1000) if created_at else int(time.time() * 1000)
            st = states.get(sess_dir_name)
            mark = item_marks.get(th_id)
            rollout_mtime = None
            if rollout_path and os.path.exists(rollout_path):
                try:
                    rollout_mtime = os.path.getmtime(rollout_path)
                except OSError:
                    rollout_mtime = None
            # Skip a thread that cannot have produced new events. Its session
            # file and cache row already exist, so nothing else in this pass
            # would change either.
            if (st is not None and os.path.exists(jsonl_file) and mark is not None
                    and st.get("version") == PROJECTION_VERSION
                    and st.get("mark") == list(mark)
                    and st.get("rolloutMtime") == rollout_mtime
                    and st.get("label") == label
                    # A permission switch changes nothing Codex records in the
                    # rollout, so this is the only signal that the picker moved.
                    and st.get("perm") == permission_presets.get(th_id, "danger-full-access")):
                sess_ids.append(sess_dir_name)
                proj_cache_sessions[sess_dir_name] = {
                    "identity": {"createdAt": c_ms, "cwd": cwd},
                    "rows": {"title": {"ver": 1, "seq": 3, "val": label}},
                }
                continue

            hist_cur.execute("""
                SELECT item_id, item_type, rollout_ordinal, created_at_ms, item_json, turn_id
                FROM thread_items
                WHERE thread_id = ?
                ORDER BY rollout_ordinal ASC
            """, (th_id,))
            items = hist_cur.fetchall()
            # A thread with no recorded items cannot be projected: listing it
            # would make the browser request a log that does not exist and show
            # "history unavailable".
            if not items:
                try:
                    os.rmdir(sess_path)
                except OSError:
                    pass
                states.pop(sess_dir_name, None)
                continue
            sess_ids.append(sess_dir_name)

            proj_cache_sessions[sess_dir_name] = {
                "identity": {"createdAt": c_ms, "cwd": cwd},
                "rows": {"title": {"ver": 1, "seq": 3, "val": label}},
            }

            timings, usage_records = read_rollout_meta(rollout_path, rollout_index)
            usage_by_turn = attribute_usage(
                {it[2]: it[5] for it in items if it[5]}, usage_records)

            # The turn's last assistant message carries its first-token and
            # decode timing, because that is the step the footer measures.
            carrier = {}
            for it in items:
                if it[1] == "agentMessage":
                    carrier[it[5]] = it[2]

            # A stored log from an older projector is rebuilt rather than
            # appended to: its earlier events are missing whatever the newer
            # rendering adds, and only a rewrite can fill them in.
            fresh = (st is None or not os.path.exists(jsonl_file)
                     or st.get("version") != PROJECTION_VERSION)
            if fresh:
                cx = {
                    "seq": -1, "turn": 0, "step": 0, "step_open": False,
                    "turn_open": False, "pending": [], "step_start": None,
                    "last_ordinal": -1, "label": None,
                    "perm": permission_presets.get(th_id, "danger-full-access"),
                    "version": PROJECTION_VERSION,
                }
                out = []
                with open(jsonl_file, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "type": "session", "version": 0, "id": sess_dir_name,
                        "createdAt": c_ms, "cwd": cwd, "delegationDepth": 0,
                        "agentPreset": "standard",
                    }, ensure_ascii=False) + NEWLINE)
                initial_preset = permission_presets.get(th_id, "danger-full-access")
                initial_sandbox, initial_approval = PERMISSION_BUNDLES.get(
                    initial_preset, ("danger-full-access", "never"))
                for ev in [
                    {"type": "permission/preset", "time": c_ms, "data": {"preset": initial_preset}},
                    {"type": "sandbox/mode", "time": c_ms, "data": {"mode": initial_sandbox}},
                    {"type": "approval/policy", "time": c_ms, "data": {"policy": initial_approval}},
                    {"type": "session/title", "time": c_ms, "data": {
                        "title": label, "source": {"kind": "user"}, "messageSeqs": []}},
                ]:
                    emit(cx, out, ev)
                cx["label"] = label
            else:
                cx = dict(st)
                cx["version"] = PROJECTION_VERSION
                cx["pending"] = list(cx.get("pending") or [])
                out = []
                if cx.get("label") != label:
                    emit(cx, out, {"type": "session/title", "time": int(time.time() * 1000),
                                   "data": {"title": label, "source": {"kind": "user"},
                                            "messageSeqs": []}})
                    cx["label"] = label
                # The permission picker writes to Codex rather than to this log,
                # so the projector is what carries the choice back: without this
                # the picker would keep showing the preset the session started
                # on while Codex actually ran under the new one.
                chosen = permission_presets.get(th_id)
                if chosen is not None and cx.get("perm") != chosen:
                    sandbox, approval = PERMISSION_BUNDLES[chosen]
                    emit(cx, out, {"type": "permission/preset", "time": int(time.time() * 1000),
                                   "data": {"preset": chosen}})
                    emit(cx, out, {"type": "sandbox/mode", "time": int(time.time() * 1000),
                                   "data": {"mode": sandbox}})
                    emit(cx, out, {"type": "approval/policy", "time": int(time.time() * 1000),
                                   "data": {"policy": approval}})
                    cx["perm"] = chosen

            def close_step(close_time):
                if not cx["step_open"]:
                    return
                if cx["pending"]:
                    text = NEWLINE.join(cx["pending"]).strip()
                    emit(cx, out, {
                        "type": "assistant/message", "time": close_time,
                        "data": {"turn": cx["turn"], "step": cx["step"], "stream": [],
                                 "message": {"id": str(uuid.uuid4()), "role": "assistant",
                                             "content": [{"type": "reasoning", "text": text}],
                                             "source": dict(MODEL_SOURCE)}},
                        "surfaceOp": "append",
                    })
                    cx["pending"] = []
                emit(cx, out, {"type": "step/end", "time": close_time,
                               "data": {"turn": cx["turn"], "step": cx["step"]}})
                cx["step_open"] = False

            def close_turn(close_time):
                if not cx["turn_open"]:
                    return
                close_step(close_time)
                emit(cx, out, {"type": "turn/end", "time": close_time,
                               "data": {"turn": cx["turn"], "reason": {"kind": "completed"}}})
                cx["turn_open"] = False

            def open_step(start_time):
                cx["step"] += 1
                cx["step_start"] = start_time
                cx["step_open"] = True
                emit(cx, out, {"type": "step/start", "time": start_time,
                               "data": {"turn": cx["turn"], "step": cx["step"]}})

            for it in items:
                item_id, item_type, ord_val, created_ms, raw_json_str, item_turn_id = it
                if ord_val <= cx["last_ordinal"]:
                    continue
                try:
                    data = json.loads(raw_json_str)
                except Exception:
                    continue
                cx["last_ordinal"] = ord_val

                # Only the turn's final assistant message carries the total, so
                # the per-step fold sums each turn exactly once.
                step_usage = (usage_by_turn.get(item_turn_id)
                              if carrier.get(item_turn_id) == ord_val else None)

                if item_type == "userMessage":
                    close_turn(created_ms)
                    cx["turn"] += 1
                    cx["turn_open"] = True
                    cx["step"] = 0
                    cx["step_open"] = False
                    cx["turn_start"] = created_ms
                    open_step(created_ms)

                    # Codex records the prompt as typed input, so an attached
                    # image is a separate part. Projecting text alone would show
                    # the message without the picture the model actually saw.
                    blocks = []
                    if isinstance(data.get("content"), list):
                        for part in data["content"]:
                            if not isinstance(part, dict):
                                continue
                            if part.get("type") in ("image", "localImage"):
                                url = part.get("url") or ""
                                media = "image/png"
                                if url.startswith("data:") and ";" in url:
                                    media = url[5:url.index(";")]
                                data_b64 = url.split(",", 1)[1] if "," in url else ""
                                if part.get("type") == "localImage":
                                    data_b64 = ""
                                if data_b64:
                                    blocks.append({"type": "image", "mediaType": media,
                                                   "data": data_b64})
                            else:
                                blocks.append({"type": "text",
                                               "text": part.get("text", "")})
                    elif isinstance(data.get("text"), str):
                        blocks.append({"type": "text", "text": data["text"]})
                    if not blocks:
                        blocks.append({"type": "text", "text": ""})

                    emit(cx, out, {"type": "turn/start", "time": created_ms,
                                   "data": {"turn": cx["turn"]}})
                    emit(cx, out, {"type": "user/message", "time": created_ms,
                                   "data": {"content": blocks,
                                            "source": {"kind": "user",
                                                       "clientTimeZone": "Asia/Shanghai"},
                                            "role": "user", "id": item_id},
                                   "surfaceOp": "append"})

                elif item_type == "reasoning":
                    r_text = ""
                    summary = data.get("summary")
                    if isinstance(summary, list):
                        r_text = NEWLINE.join(str(s) for s in summary if s)
                    elif isinstance(summary, str):
                        r_text = summary
                    if not r_text:
                        content = data.get("content")
                        if isinstance(content, list):
                            # Reasoning text arrives either as plain strings or
                            # as {"type": "reasoning_text", "text": ...} parts,
                            # and Codex uses both shapes; reading only the dict
                            # form silently dropped every thinking block.
                            pieces = []
                            for part in content:
                                if isinstance(part, dict):
                                    pieces.append(part.get("text", ""))
                                elif isinstance(part, str):
                                    pieces.append(part)
                            r_text = NEWLINE.join(p for p in pieces if p)
                        elif isinstance(content, str):
                            r_text = content
                    if r_text.strip():
                        cx["pending"].append(r_text.strip())

                elif item_type in ("commandExecution", "webSearch"):
                    if not cx["step_open"]:
                        open_step(created_ms)

                    call_id = "call-" + str(uuid.uuid4())
                    if item_type == "commandExecution":
                        tool_name = "bash"
                        cmd = data.get("command", "")
                        out_text = data.get("aggregatedOutput") or ""
                        code = data.get("exitCode", 0)
                        args_raw = json.dumps({"command": cmd}, ensure_ascii=False)
                        is_err = code != 0
                    else:
                        tool_name = "web_search"
                        query = data.get("query", "")
                        args_raw = json.dumps({"queries": [query]}, ensure_ascii=False)
                        is_err = False
                        out_text = "搜索内容: " + str(query)

                    blocks = []
                    if cx["pending"]:
                        blocks.append({"type": "reasoning", "text": NEWLINE.join(cx["pending"]).strip()})
                        cx["pending"] = []
                    blocks.append({"type": "tool-call", "id": call_id,
                                   "name": tool_name, "arguments": args_raw})

                    emit(cx, out, {
                        "type": "assistant/message", "time": created_ms,
                        "data": {"turn": cx["turn"], "step": cx["step"], "stream": [],
                                 **({} if step_usage is None else {"usage": step_usage}),
                                 "message": {"id": str(uuid.uuid4()), "role": "assistant",
                                             "content": blocks, "source": dict(MODEL_SOURCE)}},
                        "surfaceOp": "append",
                    })
                    emit(cx, out, {"type": "tool/call", "time": created_ms,
                                   "data": {"turn": cx["turn"], "step": cx["step"],
                                            "callId": call_id, "name": tool_name,
                                            "arguments": args_raw}})
                    emit(cx, out, {
                        "type": "tool/result", "time": created_ms,
                        "data": {"turn": cx["turn"], "step": cx["step"],
                                 "message": {"source": {"kind": "tool", "callId": call_id},
                                             "content": [{"type": "tool-result",
                                                          "toolCallId": call_id,
                                                          "content": [{"type": "text", "text": out_text}],
                                                          "isError": is_err}],
                                             "role": "user", "id": str(uuid.uuid4())}},
                        "surfaceOp": "append",
                    })
                    close_step(created_ms)

                elif item_type == "agentMessage":
                    text = data.get("text", "")
                    if text.strip() or cx["pending"]:
                        is_carrier = carrier.get(item_turn_id) == ord_val
                        timing = timings.get(item_turn_id) or {}
                        # When the streamer has already painted this turn's
                        # answer into the open step, finalize there instead of
                        # opening another step and rendering it twice.
                        live_step = streamed_step(jsonl_file, cx["turn"])
                        if live_step is not None and cx["step"] != live_step:
                            if cx["step_open"]:
                                close_step(created_ms)
                            cx["step"] = live_step
                            cx["step_open"] = True
                            cx["step_start"] = cx.get("turn_start") or created_ms
                        # The turn's whole-turn timing is carried by exactly one
                        # step. Its step/start is the turn's start, so the fold
                        # reads the turn's real wall time instead of adding a
                        # whole-turn duration onto every step.
                        elif is_carrier and timing.get("durationMs") and cx.get("turn_start"):
                            if cx["step_open"]:
                                close_step(created_ms)
                            cx["step"] += 1
                            cx["step_start"] = cx["turn_start"]
                            cx["step_open"] = True
                            emit(cx, out, {"type": "step/start", "time": cx["turn_start"],
                                           "data": {"turn": cx["turn"], "step": cx["step"]}})
                        if not cx["step_open"]:
                            open_step(created_ms)

                        blocks = []
                        if cx["pending"]:
                            blocks.append({"type": "reasoning", "text": NEWLINE.join(cx["pending"]).strip()})
                            cx["pending"] = []
                        if text.strip():
                            blocks.append({"type": "text", "text": text})

                        msg_time = created_ms
                        if (is_carrier
                                and timing.get("durationMs") and timing.get("ttftMs") is not None
                                and cx["step_start"]
                                and live_step is None):
                            ttft = int(timing["ttftMs"])
                            model_ms = int(timing["durationMs"]) - int(timing.get("toolMs") or 0)
                            # A turn whose first token lands at the very end of
                            # its window leaves no decode span; Codex still
                            # reports the request's output tokens, which would
                            # divide into an absurd rate. Such a turn keeps its
                            # real timestamps and contributes no decode sample.
                            decode_ms = model_ms - ttft
                            if decode_ms >= 100:
                                emit(cx, out, {
                                    "type": "assistant/chunk",
                                    "time": cx["step_start"] + ttft,
                                    "data": {"turn": cx["turn"], "step": cx["step"],
                                             "chunk": {"type": "text-delta", "index": 0, "text": " "}},
                                })
                                msg_time = cx["step_start"] + max(ttft, model_ms)

                        emit(cx, out, {
                            "type": "assistant/message", "time": msg_time,
                            "data": {"turn": cx["turn"], "step": cx["step"], "stream": [],
                                     **({} if step_usage is None else {"usage": step_usage}),
                                     "message": {"id": str(uuid.uuid4()), "role": "assistant",
                                                 "content": blocks, "source": dict(MODEL_SOURCE)}},
                            "surfaceOp": "append",
                        })
                        close_step(msg_time)

            if out:
                append_locked(jsonl_file, out)
                cx["seq"] = out[-1]["seq"]

            cx["mark"] = list(mark) if mark is not None else None
            cx["rolloutMtime"] = rollout_mtime
            states[sess_dir_name] = cx

        workspace_table[ws_id] = {
            "path": cwd, "title": proj_name, "sessionIds": sess_ids,
            "createdAt": now_iso, "updatedAt": now_iso,
        }

    hist_conn.close()

    # A workspace the user added from the web has no Codex thread behind it, so
    # this pass derives nothing for it. The existing file is read back and those
    # entries are carried over; writing only the derived set would drop them on
    # the next sweep and the restart after that would lose them for good.
    workspace_file = os.path.join(STORAGES_ROOT, "workspace.json")
    carried_ids = []
    carried = {}
    try:
        with open(workspace_file, "r", encoding="utf-8") as handle:
            previous = json.load(handle)
        derived_paths = {entry["path"] for entry in workspace_table.values()}
        for ws_id, entry in (previous.get("tables", {}).get("workspaces") or {}).items():
            if entry.get("path") in derived_paths:
                continue
            carried[ws_id] = entry
            carried_ids.append(ws_id)
    except Exception:
        pass

    with open(workspace_file, "w", encoding="utf-8") as handle:
        json.dump({
            "unit": {"name": "workspace", "version": 2},
            "global": {"initialized": True,
                       "workspaceIds": workspace_ids + carried_ids,
                       "archivedSessionIds": archived_session_ids},
            "tables": {"workspaces": {**workspace_table, **carried}},
        }, handle, ensure_ascii=False, indent=2)

    with open(os.path.join(STORAGES_ROOT, "session_projcache.json"), "w", encoding="utf-8") as handle:
        json.dump({
            "unit": {"name": "session_projcache", "version": 3},
            "global": None,
            "tables": {"sessions": proj_cache_sessions},
        }, handle, ensure_ascii=False, indent=2)

    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(states, handle, ensure_ascii=False)

    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    lock_fh.close()


if __name__ == "__main__":
    run()
