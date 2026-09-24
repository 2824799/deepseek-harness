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
import shutil
import shlex
import time
import uuid
from collections import OrderedDict

import codex_stream
import codex_live
import codex_pending

HOME = os.path.expanduser("~")
CODEX_STATE = os.path.join(HOME, ".codex/state_5.sqlite")
CODEX_HISTORY = os.path.join(HOME, ".codex/thread_history_1.sqlite")

BASE_DIR = os.environ.get("DSH_HOME") or "/home/nahida/agents/sever/dsh/.dsh-codex"
SESSIONS_ROOT = os.path.join(BASE_DIR, "sessions")
STORAGES_ROOT = os.path.join(BASE_DIR, "storages")
STATE_FILE = os.path.join(BASE_DIR, "projection-state.json")
PERMISSION_FILE = os.path.join(BASE_DIR, "permission-preset.json")

# Bumped whenever this projector changes how it renders a Codex item. A stored
# session from an older version is rebuilt from its rollout instead of being
# skipped, because its log is missing whatever the newer rendering adds (for
# example attached images, or reasoning text that used to be dropped).
PROJECTION_VERSION = 9

# Codex item types that each represent one model-visible action, and so each
# become their own tool row. Projecting only the shell and search calls spliced
# the actions on either side of a file edit, an MCP call, or a subagent
# delegation together, which misreported the turn's order and hid the edit.
TOOL_ITEM_TYPES = (
    "commandExecution", "webSearch", "fileChange", "mcpToolCall",
    "dynamicToolCall", "functionCallOutput", "imageView", "sleep",
    "collabAgentToolCall", "subAgentActivity", "contextCompaction",
)

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


def write_json_if_changed(path, value, *, indent=None):
    """Avoid waking file watchers when a projection snapshot is unchanged."""
    content = json.dumps(value, ensure_ascii=False, indent=indent)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            if handle.read() == content:
                return False
    except OSError:
        pass
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(temporary, path)
    return True


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
                    marker = stem[-36:]
                    if len(marker) != 36:
                        continue
                    index.setdefault(marker, []).append(os.path.join(dirpath, name))
        except OSError:
            continue
    return index


def _completed_item(record):
    """Convert a durable rollout item to the older history item's shape."""
    payload = record.get("payload") or {}
    if record.get("type") != "event_msg" or payload.get("type") != "item_completed":
        return None
    item = payload.get("item") or {}
    kind = item.get("type")
    mapping = {
        "UserMessage": "userMessage", "AgentMessage": "agentMessage",
        "Reasoning": "reasoning", "CommandExecution": "commandExecution",
        "FileChange": "fileChange", "McpToolCall": "mcpToolCall",
        "ContextCompaction": "contextCompaction",
        "DynamicToolCall": "dynamicToolCall", "WebSearch": "webSearch",
        "CollabAgentToolCall": "collabAgentToolCall", "ImageView": "imageView",
    }
    if kind not in mapping:
        return None
    data = dict(item)
    data["type"] = mapping[kind]
    if kind == "UserMessage":
        data["content"] = [
            {"type": "image" if part.get("type") in ("image", "localImage") else "text",
             **({"url": part.get("url", "")} if part.get("type") in ("image", "localImage")
                else {"text": part.get("text", "")})}
            for part in item.get("content") or [] if isinstance(part, dict)
        ]
    elif kind == "AgentMessage":
        data["text"] = NEWLINE.join(part.get("text", "") for part in item.get("content") or []
                                    if isinstance(part, dict) and isinstance(part.get("text"), str))
    elif kind == "Reasoning":
        data["content"] = item.get("raw_content")
        data["summary"] = item.get("summary_text")
    elif kind == "CommandExecution":
        command = item.get("command") or ""
        data["command"] = shlex.join(command) if isinstance(command, list) else command
        data["aggregatedOutput"] = item.get("aggregated_output") or item.get("formatted_output") or ""
        data["exitCode"] = item.get("exit_code")
    elif kind == "FileChange" and isinstance(item.get("changes"), dict):
        data["changes"] = [
            {"path": path, "kind": {"type": change.get("type", "update")},
             "diff": change.get("diff", ""), "content": change.get("content", "")}
            for path, change in item["changes"].items() if isinstance(change, dict)
        ]
    timestamp = payload.get("completed_at_ms") or payload.get("started_at_ms")
    if not isinstance(timestamp, int):
        try:
            timestamp = int(datetime.datetime.fromisoformat(
                record["timestamp"].replace("Z", "+00:00")).timestamp() * 1000)
        except (KeyError, TypeError, ValueError):
            timestamp = int(time.time() * 1000)
    return (item.get("id") or str(uuid.uuid4()), data["type"],
            record.get("ordinal"), timestamp, json.dumps(data, ensure_ascii=False),
            payload.get("turn_id"))


_ROLLOUT_META_CACHE = OrderedDict()
_ROLLOUT_META_CACHE_LIMIT = 16

_HISTORY_SUMMARY_CACHE = {}


def history_summary(connection):
    """Reuse the history index while both SQLite and its WAL are unchanged."""
    stamp = []
    for path in (CODEX_HISTORY, CODEX_HISTORY + "-wal"):
        try:
            info = os.stat(path)
            stamp.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
        except FileNotFoundError:
            stamp.append(None)
    key = (CODEX_HISTORY, tuple(stamp))
    if _HISTORY_SUMMARY_CACHE.get("key") != key:
        rows = connection.execute(
            "SELECT thread_id, MAX(rollout_ordinal), COUNT(*), MIN(created_at_ms) "
            "FROM thread_items GROUP BY thread_id").fetchall()
        _HISTORY_SUMMARY_CACHE.update(key=key, rows=rows)
    return _HISTORY_SUMMARY_CACHE["rows"]


def _rollout_meta_file(candidate, after_ordinal, incremental):
    """Fold only complete new JSONL records when this process has a cursor."""
    stat = os.stat(candidate)
    with open(candidate, "rb") as handle:
        prefix = handle.read(256)
        cached = _ROLLOUT_META_CACHE.get(candidate) if incremental else None
        valid = (cached is not None
                 and cached["identity"] == (stat.st_dev, stat.st_ino, prefix)
                 and cached["offset"] <= stat.st_size
                 and cached["floor"] <= after_ordinal
                 and (cached["size"] != stat.st_size
                      or cached["mtime_ns"] == stat.st_mtime_ns))
        if valid:
            state = cached
            if after_ordinal > state["floor"]:
                state["recovered"] = [item for item in state["recovered"]
                                      if item[2] > after_ordinal]
                state["floor"] = after_ordinal
        else:
            state = {"identity": (stat.st_dev, stat.st_ino, prefix),
                     "offset": 0, "size": 0, "mtime_ns": 0,
                     "floor": after_ordinal, "timings": {}, "usage": [],
                     "recovered": [], "tool_ms": {}, "item_turns": {}}
        handle.seek(state["offset"])
        while True:
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                # Do not advance past a half-written record. The next sweep
                # will parse it once its newline has arrived.
                break
            state["offset"] = handle.tell()
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            turn_id = payload.get("turn_id")
            ordinal = record.get("ordinal")
            if kind == "item_completed" and isinstance(ordinal, int) and turn_id:
                state["item_turns"][ordinal] = turn_id
            if (record.get("type") == "event_msg" and kind == "item_completed"
                    and isinstance(ordinal, int) and ordinal > state["floor"]):
                converted = _completed_item(record)
                if converted is not None:
                    state["recovered"].append(converted)
            if kind == "task_complete" and turn_id:
                started = payload.get("started_at")
                state["timings"][turn_id] = {
                    "startMs": int(started) * 1000 if started else None,
                    "durationMs": payload.get("duration_ms"),
                    "ttftMs": payload.get("time_to_first_token_ms"),
                }
            elif record.get("type") == "token_usage_record":
                ordinal = record.get("ordinal")
                usage = payload.get("usage")
                if isinstance(ordinal, int) and isinstance(usage, dict):
                    state["usage"].append((ordinal, usage))
            elif kind == "item_completed":
                item = payload.get("item")
                if isinstance(item, dict) and item.get("type") in (
                        "CommandExecution", "McpToolCall", "FileChange",
                        "DynamicToolCall", "WebSearch", "CollabAgentToolCall") and turn_id:
                    started_ms = payload.get("started_at_ms")
                    completed_ms = payload.get("completed_at_ms")
                    if started_ms and completed_ms and completed_ms > started_ms:
                        tool_ms = state["tool_ms"]
                        tool_ms[turn_id] = tool_ms.get(turn_id, 0) + (completed_ms - started_ms)
    state["size"] = stat.st_size
    state["mtime_ns"] = stat.st_mtime_ns
    if incremental:
        _ROLLOUT_META_CACHE[candidate] = state
        _ROLLOUT_META_CACHE.move_to_end(candidate)
        while len(_ROLLOUT_META_CACHE) > _ROLLOUT_META_CACHE_LIMIT:
            _ROLLOUT_META_CACHE.popitem(last=False)
    return state


def read_rollout_meta(rollout_path, rollout_index=None, after_ordinal=-1, *,
                      incremental=False, include_item_turns=False):
    """Per-turn timing and per-ordinal usage recorded by Codex.

    Codex writes first-token latency and turn duration to the rollout rather
    than to thread_items, so the projection joins the two on turn_id and
    rollout_ordinal.
    """
    timings = {}
    usage_records = []
    recovered = []
    tool_ms = {}
    item_turns = {}
    # Codex rotates a thread's rollout on compaction, so a thread's earlier
    # turns live in sibling files beside the current one. Read them all, or
    # the earlier turns report no usage.
    candidates = []
    if rollout_path:
        candidates.append(rollout_path)
        base = os.path.basename(rollout_path)
        # "rollout-<ts>-<threadId>.jsonl" / "...-<threadId>_<suffix>.jsonl"
        thread_marker = base.split("_", 1)[0].removesuffix(".jsonl")[-36:]
        for candidate in (rollout_index or {}).get(thread_marker, []):
            if candidate not in candidates:
                candidates.append(candidate)
    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        try:
            state = _rollout_meta_file(candidate, after_ordinal, incremental)
            timings.update(state["timings"])
            usage_records.extend(state["usage"])
            recovered.extend(item for item in state["recovered"] if item[2] > after_ordinal)
            if include_item_turns:
                item_turns.update(state["item_turns"])
            for turn, ms in state["tool_ms"].items():
                tool_ms[turn] = tool_ms.get(turn, 0) + ms
        except Exception:
            continue
    for turn, ms in tool_ms.items():
        timings.setdefault(turn, {})["toolMs"] = ms
    if include_item_turns:
        return timings, usage_records, recovered, item_turns
    return timings, usage_records, recovered


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
        # Codex's input_tokens includes the cached portion. DSH's inputTokens
        # is the disjoint uncached portion; counting both doubles the divisor.
        total_input = int(usage.get("input_tokens") or 0)
        cached = int(usage.get("cached_input_tokens") or 0)
        written = int(usage.get("cache_write_input_tokens") or 0)
        bucket["inputTokens"] += max(0, total_input - cached - written)
        bucket["outputTokens"] += int(usage.get("output_tokens") or 0)
        bucket["cacheReadTokens"] += cached
        bucket["cacheWriteTokens"] += written
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


def emit(cx, out, event, keep_time=False):
    # Codex stamps each item with its own creation time while the projector adds
    # synthetic ones (a turn's first-token and completion instants), and the two
    # sources occasionally interleave out of order. The browser folds the log by
    # time, so the log itself is kept non-decreasing.
    when = event.get("time")
    if isinstance(when, int):
        last = cx.get("last_time")
        if keep_time:
            # A metric-only step boundary can precede completed tool rows in
            # the same turn. Preserve its measured timestamp without moving
            # the visible conversation's high-water clock backwards.
            pass
        elif isinstance(last, int) and when < last:
            event["time"] = last
        else:
            cx["last_time"] = when
    cx["seq"] += 1
    event["seq"] = cx["seq"]
    out.append(event)


def _text_parts(value):
    """Flatten one reasoning text field to its plain strings.

    Codex writes reasoning text either as bare strings or as
    `{"type": "reasoning_text", "text": ...}` parts, and uses both shapes; a
    reader that understood only one of them dropped every block using the other.
    """
    if isinstance(value, list):
        pieces = []
        for part in value:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
            elif isinstance(part, str):
                pieces.append(part)
        return pieces
    return [value] if isinstance(value, str) else []


def reasoning_text(data):
    """The readable thinking text of one Codex reasoning item.

    A model whose chain of thought is encrypted (the GPT family) publishes it
    only as an encrypted blob, leaves `content` null, and writes the
    human-readable condensation to `summary` — so the summary is the only text
    there is. A model that exposes its chain (deepseek, luna, mimo) does the
    opposite: full text in `content`, empty `summary`. Reading only one field
    therefore blanks the panel for one family or the other, so both are read and
    the full chain wins whenever it is present.
    """
    content = NEWLINE.join(p for p in _text_parts(data.get("content")) if p).strip()
    if content:
        return content
    return NEWLINE.join(p for p in _text_parts(data.get("summary")) if p).strip()


def _diff_text_pair(diff_text):
    """Reconstruct a change's before/after text from its unified diff.

    The diff card re-derives its own patch from old and new text, so a file
    change is projected as that pair: the browser then draws real added/removed
    rows instead of a pre-rendered blob it cannot style.
    """
    old_lines = []
    new_lines = []
    for line in diff_text.split(NEWLINE):
        if line.startswith("@@") or line.startswith("\\"):
            continue
        if line.startswith("-"):
            old_lines.append(line[1:])
        elif line.startswith("+"):
            new_lines.append(line[1:])
        elif line.startswith(" "):
            old_lines.append(line[1:])
            new_lines.append(line[1:])
        elif line == "":
            # A hunk's blank context line arrives with its leading space eaten.
            old_lines.append("")
            new_lines.append("")
    return NEWLINE.join(old_lines), NEWLINE.join(new_lines)


def _file_change_row(data):
    """One file edit as a diff-card payload plus the row's own path and text."""
    diffs = []
    paths = []
    added_kinds = 0
    for change in data.get("changes") or []:
        if not isinstance(change, dict):
            continue
        path = change.get("path")
        if not isinstance(path, str) or not path:
            continue
        kind = change.get("kind") if isinstance(change.get("kind"), dict) else {}
        change_kind = kind.get("type") or "update"
        diff = change.get("diff")
        old_text, new_text = _diff_text_pair(diff) if isinstance(diff, str) and diff else (
            None, change.get("content") or "")
        if change_kind == "add":
            old_text = None
            added_kinds += 1
        elif change_kind == "delete":
            new_text = ""
        move = kind.get("move_path")
        # A moved file is opened where it now lives; the source path would
        # point at a file that no longer exists.
        target = move if isinstance(move, str) and move else path
        label = path + (" -> " + move if isinstance(move, str) and move else "")
        paths.append(label)
        diffs.append({"path": target, "oldText": old_text, "newText": new_text})
    if not diffs:
        return None
    # A change that only adds files reads as a write; anything else reads as an
    # edit. The tool name is what selects the row's icon, title, and openable
    # path, while the card itself is drawn from the hunks above.
    all_adds = added_kinds == len(diffs)
    first = diffs[0]
    if all_adds:
        name = "write"
        args = {"file_path": first["path"], "content": first["newText"]}
    else:
        name = "edit"
        args = {"file_path": first["path"],
                "old_string": first["oldText"] or "",
                "new_string": first["newText"]}
    summary = (paths[0] if len(paths) == 1
               else "%d files: %s" % (len(paths), ", ".join(paths)))
    return {"name": name, "args": args, "output": NEWLINE.join(paths),
            "meta": {"diffs": diffs}, "summary": summary,
            "isError": data.get("status") == "failed"}


def _json_text(value):
    """A readable rendering of an item's structured arguments or result."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def _result_text(result):
    """Flatten an MCP result envelope to the text its blocks carry."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        blocks = result.get("content")
        if isinstance(blocks, list):
            pieces = []
            for block in blocks:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        pieces.append(text)
                    elif block.get("type") not in (None, "text"):
                        pieces.append(_json_text(block))
            if pieces:
                return NEWLINE.join(pieces)
    return _json_text(result)


def tool_row(item_type, data):
    """Map one Codex action item onto the tool row the web UI draws.

    Every item type Codex records for a model-visible action gets a row. Types
    without a dedicated DSH view fall back to the generic row, which shows the
    call's arguments and its result text — a faithful rendering beats the
    alternative of dropping the action and splicing its neighbours together.
    """
    if item_type == "commandExecution":
        command = data.get("command") or ""
        return {"name": "bash", "args": {"command": command},
                "output": data.get("aggregatedOutput") or "", "meta": None,
                "summary": command, "isError": (data.get("exitCode") or 0) != 0}
    if item_type == "webSearch":
        query = data.get("query") or ""
        return {"name": "web_search", "args": {"queries": [query]},
                "output": "搜索内容: " + str(query), "meta": None,
                "summary": query, "isError": False}
    if item_type == "fileChange":
        return _file_change_row(data)
    if item_type == "mcpToolCall":
        server = data.get("server") or ""
        tool = data.get("tool") or ""
        arguments = data.get("arguments")
        # The plugin-owned tools have no DSH view, so the row names them the way
        # MCP names them everywhere else and shows the arguments verbatim.
        output = data.get("error") or _result_text(data.get("result"))
        return {"name": "mcp__%s__%s" % (server, tool),
                "args": arguments if isinstance(arguments, dict) else {"arguments": arguments},
                "output": output, "meta": None, "summary": tool or server,
                "isError": data.get("status") not in (None, "completed")}
    if item_type == "dynamicToolCall":
        tool = data.get("tool") or "tool"
        pieces = []
        for block in data.get("contentItems") or []:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return {"name": "%s__%s" % (data.get("namespace") or "tool", tool),
                "args": data.get("arguments") if isinstance(data.get("arguments"), dict) else {},
                "output": NEWLINE.join(pieces), "meta": None, "summary": tool,
                "isError": data.get("success") is False}
    if item_type == "functionCallOutput":
        tool = data.get("name") or "tool"
        return {"name": "%s__%s" % (data.get("namespace") or "tool", tool),
                "args": {}, "output": data.get("output") or "", "meta": None,
                "summary": tool, "isError": False}
    if item_type == "imageView":
        path = data.get("path") or ""
        # The durable reference carries no attachment-store identity, so the row
        # is an ordinary file read whose path link opens the picture, rather
        # than a read_image row claiming an image card nothing could resolve.
        return {"name": "read", "args": {"path": path},
                "output": path, "meta": None, "summary": path, "isError": False}
    if item_type == "sleep":
        duration = data.get("durationMs")
        return {"name": "sleep", "args": {"durationMs": duration},
                "output": "等待 %s 毫秒" % duration, "meta": None,
                "summary": str(duration), "isError": False}
    if item_type == "collabAgentToolCall":
        prompt = data.get("prompt") or ""
        receivers = data.get("receiverThreadIds") or []
        # `subagent` is the name the chat's process fold recognizes as a
        # delegation, so the turn's process disclosure counts it as one.
        return {"name": "subagent",
                "args": {"prompt": prompt, "model": data.get("model"),
                         "tool": data.get("tool")},
                "output": "子代理: " + ", ".join(str(r) for r in receivers),
                "meta": None, "summary": prompt,
                "isError": data.get("status") not in (None, "completed")}
    if item_type == "subAgentActivity":
        path = data.get("agentPath") or ""
        return {"name": "subagent",
                "args": {"agentThreadId": data.get("agentThreadId")},
                "output": "%s %s" % (data.get("kind") or "", path),
                "meta": None, "summary": "%s %s" % (data.get("kind") or "", path),
                "isError": False}
    if item_type == "contextCompaction":
        # Compaction is a marker in Codex's log, not a tool call. Emitting DSH's
        # own compaction lifecycle would demand a matching summary event and
        # surface replacement, and a malformed one makes the browser reject the
        # whole log; a plain marker row keeps the position visible and safe.
        return {"name": "context_compaction", "args": {},
                "output": "上下文已压缩", "meta": None,
                "summary": "上下文已压缩", "isError": False}
    return None


def _scan_log(path):
    """Read a log's tail through the streamer's scanner.

    Both writers must agree on what the log says, so the fold lives in one place
    and this is only the file-handling half of it.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return codex_stream._scan(fh)


def read_log_tail(path):
    """The log's structural tail, or None when it cannot be read."""
    try:
        return _scan_log(path)
    except OSError:
        return None


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


def append_live_chunks(request):
    """Place Codex deltas in their item's step, after its durable predecessor.

    Called only by the projection writer. An item cannot borrow a previous
    item's step, and a late delta cannot reopen an already completed item.
    """
    session_id = request.get("sessionId")
    item_id = request.get("itemId")
    source_turn = request.get("turnId")
    chunks = request.get("chunks")
    if (not isinstance(session_id, str) or not session_id.startswith("session-")
            or not isinstance(item_id, str) or not item_id
            or not isinstance(source_turn, str) or not source_turn
            or not isinstance(chunks, list) or not chunks or len(chunks) > 5000
            or any(not isinstance(chunk, dict) for chunk in chunks)):
        return None
    path = codex_stream.find_session_file(session_id)
    if path is None:
        return None
    with open(os.path.join(BASE_DIR, "projection.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(STATE_FILE, encoding="utf-8") as handle:
                states = json.load(handle)
        except (OSError, ValueError):
            return None
        cx = states.get(session_id)
        if not cx or cx.get("version") != PROJECTION_VERSION:
            return None
        completed = cx.get("completed_item_ids") or []
        if item_id in completed:
            return {"dropped": True}
        if cx.get("source_turn_id") != source_turn:
            return None
        active = cx.get("live_item_id")
        if active and active != item_id:
            return None
        predecessor = request.get("afterItemId")
        if (not active and predecessor and predecessor != cx.get("last_item_id")
                and predecessor not in completed):
            return None
        out = []
        now = int(time.time() * 1000)
        starting = active != item_id
        if starting:
            if not cx["step_open"]:
                cx["step"] += 1
                cx["step_open"] = True
                cx["step_start"] = now
                emit(cx, out, {"type": "step/start", "time": now,
                               "data": {"turn": cx["turn"], "step": cx["step"]}})
            cx["live_item_id"] = item_id
        for chunk in chunks:
            emit(cx, out, {"type": "assistant/chunk", "time": now,
                           "data": {"turn": cx["turn"], "step": cx["step"],
                                    "chunk": chunk}})
        append_locked(path, out)
        if starting:
            # Subsequent deltas need no checkpoint rewrite. The next sweep
            # reads their time/seq high-water mark from the log's small tail.
            cx["seq"] = out[-1]["seq"]
            write_json_if_changed(STATE_FILE, states)
        return {"seq": out[-1]["seq"], "turn": cx["turn"], "step": cx["step"]}


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

    state_conn = sqlite3.connect(f"file:{CODEX_STATE}?mode=ro", uri=True)
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
    # Every thread id that still exists in Codex, archived or not. Session
    # directories outside this set belong to threads Codex has deleted and
    # must not keep listing on the web.
    state_cur.execute("SELECT id, cwd FROM threads")
    known_threads = dict(state_cur.fetchall())
    known_thread_ids = set(known_threads)
    pending_threads = codex_pending.load()
    active_pending = codex_pending.prune(pending_threads, known_thread_ids)
    if active_pending != pending_threads:
        codex_pending.save(active_pending)
    pending_threads = active_pending
    known_session_ids = {"session-" + thread_id for thread_id in known_thread_ids}
    known_session_ids.update("session-" + thread_id for thread_id in pending_threads)
    for thread_id, entry in pending_threads.items():
        codex_pending.materialize(thread_id, entry)
    state_conn.close()

    hist_conn = sqlite3.connect(f"file:{CODEX_HISTORY}?mode=ro", uri=True)
    hist_cur = hist_conn.cursor()
    rollout_index = build_rollout_index()
    # Cheap change detection: a thread whose newest item and item count are
    # unchanged since the last pass cannot produce new events, so it is skipped
    # entirely. Without this every pass re-read the whole history.
    summary_rows = history_summary(hist_conn)
    item_marks = {row[0]: (row[1], row[2]) for row in summary_rows}
    earliest_items = {row[0]: row[3] for row in summary_rows}

    # The app-server's project/list owns both the project list and its order.
    # Project membership comes from threads.project_id when assigned, with cwd
    # fallback for older Codex threads that have no explicit project ID.
    live_projects = codex_live.scan_projects()
    live_doc = codex_live.read()
    if live_projects is None:
        live_projects = live_doc.get("projects") or {}
    # The previous registry document, read once: stable createdAt/updatedAt
    # keep workspace.json byte-identical across idle sweeps, which is what
    # lets the host's file watcher stay quiet.
    previous_ws_entries = {}
    try:
        with open(os.path.join(STORAGES_ROOT, "workspace.json"), "r", encoding="utf-8") as handle:
            previous_workspaces = json.load(handle)
        previous_ws_entries = previous_workspaces.get("tables", {}).get("workspaces") or {}
    except Exception:
        pass
    group_roots = dict(live_projects)
    previous_ids_by_path = {entry.get("path"): workspace_id
                            for workspace_id, entry in previous_ws_entries.items()
                            if entry.get("path") in group_roots}
    project_root_by_id = {project_id: root for root, project_id in codex_live.project_ids().items()
                          if project_id and root in group_roots}
    groups = {root: {"title": name, "path": root, "threads": []}
              for root, name in group_roots.items()}
    loose = []
    for th in threads:
        th_id, title, name, created_at, updated_at, cwd, project_id, rollout_path = th
        if not cwd:
            cwd = "/home/nahida/agents/sever"
        group_path = project_root_by_id.get(project_id, cwd)
        proj_name = group_roots.get(group_path)
        if proj_name is None:
            loose.append(th)
            continue
        groups[group_path]["threads"].append(th)

    workspace_table = {}
    workspace_ids = []
    proj_cache_sessions = {}
    titles = {}
    derived_slugs = set()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    for _, ginfo in list(groups.items()) + [("", {"title": "", "path": None, "threads": loose})]:
        proj_name = ginfo["title"]
        cwd = ginfo["path"]
        # The storage layer derives every session directory from the header
        # cwd, so the loose bucket shares the fallback root's directory; the
        # web grouping comes from workspace.json membership, not the folder.
        ws_slug = "--" + (cwd or "/home/nahida/agents/sever").strip("/").replace("/", "-") + "--"
        derived_slugs.add(ws_slug)
        ws_dir = os.path.join(SESSIONS_ROOT, ws_slug)
        os.makedirs(ws_dir, exist_ok=True)
        ws_id = previous_ids_by_path.get(cwd) or str(uuid.uuid5(
            uuid.NAMESPACE_URL, cwd or "/home/nahida/agents/sever"))
        # Session headers and projection identities carry a cwd the storage
        # layer requires to be a string; the loose bucket has no directory of
        # its own, so its sessions borrow the fallback root.
        cwd = cwd or "/home/nahida/agents/sever"
        if proj_name:
            workspace_ids.append(ws_id)

        sess_ids = []
        for th in ginfo["threads"]:
            th_id, title, name, created_at, updated_at, _, _, rollout_path = th
            sess_dir_name = "session-" + th_id
            sess_path = os.path.join(ws_dir, sess_dir_name)
            os.makedirs(sess_path, exist_ok=True)
            jsonl_file = os.path.join(sess_path, "session.jsonl")

            label = clean_title(name, title)
            titles[sess_dir_name] = label
            c_ms = int(created_at * 1000) if created_at else int(time.time() * 1000)
            # After compaction Codex may reset threads.created_at to the new
            # rollout's creation, while thread_history still contains older
            # turns. Seeding the session clock from that newer date clamps all
            # earlier events forward and turns minutes into days of fake LLM
            # time. The first durable item is the session's real lower bound.
            earliest = earliest_items.get(th_id)
            if isinstance(earliest, int) and earliest > 0:
                c_ms = min(c_ms, earliest)
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
            if (st is not None and os.path.exists(jsonl_file)
                    and st.get("version") == PROJECTION_VERSION
                    and st.get("mark") == (list(mark) if mark is not None else None)
                    and st.get("rolloutMtime") == rollout_mtime
                    and st.get("label") == label
                    # Older projector passes retained a completed reasoning
                    # item without publishing it until the next tool or answer.
                    and not st.get("pending")
                    # A permission switch changes nothing Codex records in the
                    # rollout, so this is the only signal that the picker moved.
                    and st.get("perm") == permission_presets.get(th_id, "danger-full-access")):
                sess_ids.append(sess_dir_name)
                proj_cache_sessions[sess_dir_name] = {
                    "identity": {"createdAt": c_ms, "cwd": cwd},
                    "rows": {"title": {"ver": 1, "seq": 3, "val": label}},
                }
                continue

            continuing = (st is not None and os.path.exists(jsonl_file)
                          and st.get("version") == PROJECTION_VERSION)
            after_ordinal = st.get("last_ordinal", -1) if continuing else -1
            hist_cur.execute("""
                SELECT item_id, item_type, rollout_ordinal, created_at_ms, item_json, turn_id
                FROM thread_items
                WHERE thread_id = ? AND rollout_ordinal > ?
                ORDER BY rollout_ordinal ASC
            """, (th_id, after_ordinal))
            items = hist_cur.fetchall()
            history_max = mark[0] if mark and isinstance(mark[0], int) else -1
            recovery_floor = history_max
            if (st is not None and os.path.exists(jsonl_file)
                    and st.get("version") == PROJECTION_VERSION):
                recovery_floor = max(history_max, st.get("last_ordinal", -1))
            timings, usage_records, recovered, rollout_item_turns = read_rollout_meta(
                rollout_path, rollout_index, recovery_floor,
                incremental=True, include_item_turns=True)
            if recovered:
                # Desktop can keep writing its rollout after its history index
                # stops updating. The rollout's completed items are the source
                # of truth for that gap; history remains the source below its
                # high-water ordinal so the same item is never projected twice.
                items = sorted([*items, *recovered], key=lambda it: it[2])
            # The history index can lag or be empty for a thread whose rollout
            # already supplied items. Once those ordinals are checkpointed,
            # the next sweep recovers no new items; keep its existing log.
            existing_rollout_log = (st is not None and os.path.exists(jsonl_file)
                                    and st.get("version") == PROJECTION_VERSION
                                    and st.get("last_ordinal", -1) >= 0)
            if not items:
                if not existing_rollout_log:
                    # A genuinely blank thread has no conversation to list.
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

            usage_by_turn = attribute_usage(
                {**rollout_item_turns,
                 **{it[2]: it[5] for it in items if it[5] and isinstance(it[2], int)}},
                usage_records)

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
                codex_stream._TAIL_CACHE.pop(os.path.abspath(jsonl_file), None)
                cx = {
                    "seq": -1, "turn": 0, "step": 0, "step_open": False,
                    "turn_open": False, "pending": [], "step_start": None,
                    "last_ordinal": -1, "label": None, "last_time": None,
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
                # Continue the log's own high-water time mark rather than this
                # process's, so a resumed pass cannot emit a stamp below the
                # tail the streamer already wrote.
                tail = read_log_tail(jsonl_file)
                if tail is not None and tail.get("maxTime") is not None:
                    cx["last_time"] = tail["maxTime"]
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

            def show_reasoning(text, when):
                if not cx["step_open"]:
                    open_step(when)
                emit(cx, out, {
                    "type": "assistant/message", "time": when,
                    "data": {"turn": cx["turn"], "step": cx["step"], "stream": [],
                             "message": {"id": str(uuid.uuid4()), "role": "assistant",
                                         "content": [{"type": "reasoning", "text": text}],
                                         "source": dict(MODEL_SOURCE)}},
                    "surfaceOp": "append",
                })
                # DSH has one settled assistant row per step. Keep this item
                # in its own step so the next tool/answer cannot replace it.
                close_step(when)

            if cx["pending"]:
                # Publish reasoning held by an earlier pass before any later
                # item; its ordinal was already committed to the checkpoint.
                show_reasoning(NEWLINE.join(cx["pending"]).strip(),
                               cx.get("last_time") or c_ms)
                cx["pending"] = []

            for it in items:
                item_id, item_type, ord_val, created_ms, raw_json_str, item_turn_id = it
                if ord_val <= cx["last_ordinal"]:
                    continue
                try:
                    data = json.loads(raw_json_str)
                except Exception:
                    continue
                cx["last_ordinal"] = ord_val
                cx["source_turn_id"] = item_turn_id
                cx["last_item_id"] = item_id
                cx["completed_item_ids"] = [*(cx.get("completed_item_ids") or []), item_id][-32:]
                if cx.get("live_item_id") == item_id:
                    cx.pop("live_item_id", None)

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
                    r_text = reasoning_text(data)
                    if r_text:
                        show_reasoning(r_text, created_ms)

                elif item_type in TOOL_ITEM_TYPES:
                    row = tool_row(item_type, data)
                    if row is not None:
                        if not cx["step_open"]:
                            open_step(created_ms)

                        call_id = "call-" + str(uuid.uuid4())
                        tool_name = row["name"]
                        args_raw = json.dumps(row["args"], ensure_ascii=False)
                        out_text = row["output"]
                        is_err = row["isError"]

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
                                     **({} if row["meta"] is None else {"meta": row["meta"]}),
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
                        # The turn's model-time sample is carried by exactly
                        # one step so cumulative stats count it once.
                        elif is_carrier and timing.get("durationMs") and cx.get("turn_start"):
                            if cx["step_open"]:
                                close_step(created_ms)
                            cx["step"] += 1
                            model_ms = max(0, int(timing["durationMs"]) - int(timing.get("toolMs") or 0))
                            cx["step_start"] = max(cx["turn_start"], created_ms - model_ms)
                            cx["step_open"] = True
                            emit(cx, out, {"type": "step/start", "time": cx["step_start"],
                                           "data": {"turn": cx["turn"], "step": cx["step"]}},
                                 keep_time=True)
                        if not cx["step_open"]:
                            open_step(created_ms)

                        blocks = []
                        if cx["pending"]:
                            blocks.append({"type": "reasoning", "text": NEWLINE.join(cx["pending"]).strip()})
                            cx["pending"] = []
                        if text.strip():
                            blocks.append({"type": "text", "text": text})

                        msg_time = created_ms
                        timing_stream = []
                        if (is_carrier
                                and timing.get("durationMs") and timing.get("ttftMs") is not None
                                and cx["step_start"]
                                and live_step is None):
                            ttft = int(timing["ttftMs"])
                            model_ms = int(timing["durationMs"]) - int(timing.get("toolMs") or 0)
                            decode_ms = model_ms - ttft
                            if decode_ms >= 100:
                                # The DSH stats fold reads first-token time
                                # from the assembled stream, not an unrelated
                                # assistant/chunk event. The completion time is
                                # the real item completion; the metric-only
                                # boundary subtracts measured model time.
                                timing_stream = [{"type": "chunk",
                                                  "time": cx["step_start"] + ttft,
                                                  "chunk": {"type": "text-delta",
                                                            "index": 0, "text": " "}}]
                                # The installed DSH release counts live
                                # assistant/chunk events, while newer releases
                                # read the assembled stream above. Emit both
                                # forms so the same measured boundary works
                                # with either reader.
                                emit(cx, out, {"type": "assistant/chunk",
                                               "time": cx["step_start"] + ttft,
                                               "data": {"turn": cx["turn"], "step": cx["step"],
                                                        "chunk": {"type": "text-delta",
                                                                  "index": 0, "text": " "}}},
                                     keep_time=True)

                        emit(cx, out, {
                            "type": "assistant/message", "time": msg_time,
                            "data": {"turn": cx["turn"], "step": cx["step"], "stream": timing_stream,
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

        if proj_name:
            workspace_table[ws_id] = {
                "path": cwd, "title": proj_name, "sessionIds": sess_ids,
                "createdAt": (previous_ws_entries.get(ws_id) or {}).get("createdAt") or now_iso,
                "updatedAt": now_iso if (
                    (previous_ws_entries.get(ws_id) or {}).get("sessionIds") != sess_ids
                    or (previous_ws_entries.get(ws_id) or {}).get("title") != proj_name
                ) else (previous_ws_entries.get(ws_id) or {}).get("updatedAt") or now_iso,
            }

    # A thread can change project roots while both the old and new roots still
    # exist. The ungrouped bucket uses the fallback root, even when Codex's cwd
    # is elsewhere. Retain displaced copies outside the scanned sessions tree.
    displaced_root = os.path.join(BASE_DIR, "displaced-projections")
    # Index actual directories once. Testing every thread under every workspace
    # made an unchanged sweep issue thousands of unnecessary stat calls.
    locations = {}
    for workspace_entry in os.scandir(SESSIONS_ROOT):
        if not workspace_entry.is_dir():
            continue
        for session_entry in os.scandir(workspace_entry.path):
            if session_entry.name.startswith("session-") and session_entry.is_dir():
                locations.setdefault(session_entry.name, []).append(workspace_entry.name)
    for thread_id, thread_cwd in known_threads.items():
        owner_cwd = thread_cwd if thread_cwd in group_roots else "/home/nahida/agents/sever"
        expected_slug = "--" + owner_cwd.strip("/").replace("/", "-") + "--"
        session_name = "session-" + thread_id
        canonical = os.path.join(SESSIONS_ROOT, expected_slug, session_name, "session.jsonl")
        candidates = locations.get(session_name, [])
        if len(candidates) < 2:
            continue
        candidates = [slug for slug in candidates if os.path.isfile(
            os.path.join(SESSIONS_ROOT, slug, session_name, "session.jsonl"))]
        if len(candidates) < 2:
            continue
        keeper = expected_slug if os.path.isfile(canonical) else max(
            candidates, key=lambda slug: os.path.getsize(os.path.join(
                SESSIONS_ROOT, slug, session_name, "session.jsonl")))
        for slug in candidates:
            if slug == keeper:
                continue
            displaced = os.path.join(SESSIONS_ROOT, slug, session_name)
            target = os.path.join(displaced_root, slug, session_name + "-" + str(time.time_ns()))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(displaced, target)

    # Session directories of threads Codex no longer knows about would keep
    # listing forever otherwise: persistence.list() walks every directory under
    # the sessions root, not just the ones a workspace references.
    for slug in os.listdir(SESSIONS_ROOT):
        slug_dir = os.path.join(SESSIONS_ROOT, slug)
        if not os.path.isdir(slug_dir):
            continue
        for entry in os.listdir(slug_dir):
            if not entry.startswith("session-") or entry in known_session_ids:
                continue
            stale = os.path.join(slug_dir, entry)
            if os.path.isdir(stale):
                shutil.rmtree(stale, ignore_errors=True)

    hist_conn.close()

    workspace_file = os.path.join(STORAGES_ROOT, "workspace.json")
    # The previous registry supplies stable IDs and timestamps only. A project
    # absent from Codex's latest list cannot survive by virtue of an old DSH
    # row. Prune its folder so the DSH persistence scanner cannot show ghosts.
    keep_slugs = set(derived_slugs)
    for slug in os.listdir(SESSIONS_ROOT):
        if slug in keep_slugs:
            continue
        slug_dir = os.path.join(SESSIONS_ROOT, slug)
        if os.path.isdir(slug_dir):
            shutil.rmtree(slug_dir, ignore_errors=True)

    write_json_if_changed(workspace_file, {
            "unit": {"name": "workspace", "version": 2},
            "global": {"initialized": True,
                       "workspaceIds": workspace_ids,
                       "archivedSessionIds": archived_session_ids},
            "tables": {"workspaces": workspace_table},
        }, indent=2)

    write_json_if_changed(os.path.join(STORAGES_ROOT, "session_projcache.json"), {
            "unit": {"name": "session_projcache", "version": 3},
            "global": None,
            "tables": {"sessions": proj_cache_sessions},
        }, indent=2)

    write_json_if_changed(STATE_FILE, states)

    # A blank thread exists only in the app-server until its first turn.
    # Keep its header and workspace membership through every projection pass;
    # once real history has been projected, Codex's own row takes over.
    projected = {session_id.removeprefix("session-") for session_id in proj_cache_sessions}
    remaining = {thread_id: entry for thread_id, entry in pending_threads.items()
                 if thread_id not in projected or not item_marks.get(thread_id)}
    if remaining != pending_threads:
        codex_pending.save(remaining)

    # One small document the host process reads to answer "which conversations
    # are running" and "what is this session called" without touching Codex.
    codex_live.publish(codex_live.scan_running(), live_projects, titles,
                       proj_cache_sessions.keys())

    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    lock_fh.close()


if __name__ == "__main__":
    run()
