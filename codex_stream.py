#!/usr/bin/env python3
"""Forward token-level Codex turn deltas to the single projection writer.

Codex delivers item/agentMessage/delta and item/reasoning/*Delta notifications
only on the app-server connection that submitted the turn, so the process that
calls turn/start hands its socket to a detached child. That child forwards
deltas to the sync daemon over a local socket. The daemon alone writes the
projected log, including its structural events.
"""

import fcntl
import json
import os
import socket
import time
from collections import OrderedDict

NEWLINE = chr(10)
SESSIONS_ROOT = os.path.join(os.environ.get("DSH_HOME") or
                             "/home/nahida/agents/sever/dsh/.dsh-codex", "sessions")
TAIL_WINDOW = 1 << 20
STRUCTURAL = ("turn/start", "turn/end", "step/start", "step/end")
IDLE_TIMEOUT_S = 300
HARD_TIMEOUT_S = 7200
FLUSH_INTERVAL_S = 0.08
DEBUG_LOG = os.environ.get("DSH_CODEX_STREAM_DEBUG")
_TAIL_CACHE = OrderedDict()


def _debug(message):
    """Append a diagnostic line when DSH_CODEX_STREAM_DEBUG names a file."""
    if not DEBUG_LOG:
        return
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{time.time():.3f} pid={os.getpid()} {message}{NEWLINE}")
    except OSError:
        pass


def find_session_file(session_id):
    """Locate one projected session log by its DSH session id."""
    try:
        workspaces = os.listdir(SESSIONS_ROOT)
    except OSError:
        return None
    for workspace in workspaces:
        candidate = os.path.join(SESSIONS_ROOT, workspace, session_id, "session.jsonl")
        if os.path.exists(candidate):
            return candidate
    return None


def _scan(fh):
    """Read a log's tail: highest seq, open turn/step, and any torn last line."""
    stat = os.fstat(fh.fileno())
    key = os.path.abspath(fh.name)
    identity = (stat.st_dev, stat.st_ino)
    cached = _TAIL_CACHE.get(key)
    valid = (cached and cached["identity"] == identity and cached["size"] <= stat.st_size
             and (cached["size"] < stat.st_size or cached["mtime"] == stat.st_mtime_ns))
    if valid and cached["size"] == stat.st_size:
        return dict(cached["state"])
    initial = cached["state"] if valid else None
    start = cached["offset"] if valid else max(0, stat.st_size - TAIL_WINDOW)
    raw = os.pread(fh.fileno(), stat.st_size - start, start)
    offset = start + raw.rfind(b"\n") + 1
    state = _fold(raw.decode("utf-8", errors="replace"), start, stat.st_size, initial)
    # Text positions are not byte offsets for Chinese or image metadata.
    state["tornAt"] = offset if raw and not raw.endswith(b"\n") else None
    _TAIL_CACHE[key] = {"identity": identity, "size": stat.st_size,
                        "mtime": stat.st_mtime_ns, "offset": offset, "state": state}
    _TAIL_CACHE.move_to_end(key)
    while len(_TAIL_CACHE) > 64:
        _TAIL_CACHE.popitem(last=False)
    return dict(state)


def _fold(data, start, size, initial=None):
    """Fold a log slice into the structural state it ends in."""
    torn_at = None
    if data and not data.endswith(NEWLINE):
        cut = data.rfind(NEWLINE)
        torn_at = start + cut + 1
        data = data[:cut + 1] if cut >= 0 else ""
    lines = data.split(NEWLINE)
    if start > 0 and lines and initial is None:
        lines = lines[1:]
    state = dict(initial) if initial is not None else {"maxSeq": -1, "maxTime": None, "turn": 0, "step": 0,
             "stepOpen": False, "turnOpen": False, "tornAt": torn_at,
             "stepHasChunks": False}
    state["tornAt"] = torn_at
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        seq = event.get("seq")
        if isinstance(seq, int) and seq > state["maxSeq"]:
            state["maxSeq"] = seq
        # The browser folds the log by time, so both writers clamp their own
        # stamps to the tail's high-water mark rather than trusting that their
        # source clock agrees with the other's.
        when = event.get("time")
        if isinstance(when, int) and (state["maxTime"] is None or when > state["maxTime"]):
            state["maxTime"] = when
        kind = event.get("type")
        if kind == "assistant/chunk":
            payload = event.get("data") or {}
            if (state["stepOpen"]
                    and payload.get("turn") == state["turn"]
                    and payload.get("step") == state["step"]):
                state["stepHasChunks"] = True
            continue
        if kind not in STRUCTURAL:
            continue
        payload = event.get("data") or {}
        if kind == "turn/start":
            state["turn"] = payload.get("turn", state["turn"])
            state["turnOpen"] = True
            # The projector emits step/start before turn/start for a user
            # message, so turn/start must not clear the step it just opened.
            # Step state is owned by step/start and step/end alone.
        elif kind == "turn/end":
            state["turnOpen"] = False
            state["stepOpen"] = False
        elif kind == "step/start":
            state["step"] = payload.get("step", state["step"])
            state["stepOpen"] = True
            state["stepHasChunks"] = False
        elif kind == "step/end":
            state["stepOpen"] = False
    return state


def read_tail(session_id):
    """The log's current structural state, read without taking the write lock."""
    path = find_session_file(session_id)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return _scan(fh)
    except OSError:
        return None


def _locked_append(path, events, fill_placement=False, min_turn=None, only_turn=None):
    """Append events under an exclusive lock, renumbering them from the log tail.

    Returns the placement that was used, None when the log cannot accept the
    events yet (no step is open, or the turn has not advanced), and a dict with
    dropped=True when the target turn has already been superseded.
    """
    try:
        fh = open(path, "a+", encoding="utf-8")
    except OSError as exc:
        _debug(f"open failed: {exc}")
        return None
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        state = _scan(fh)
        _debug(f"scan state={state}")
        if state["tornAt"] is not None:
            fh.truncate(state["tornAt"])
        if fill_placement and not state["stepOpen"]:
            _debug("reject: no open step")
            return None
        if min_turn is not None and state["turn"] <= min_turn:
            _debug(f"reject: turn {state['turn']} <= min_turn {min_turn}")
            return None
        if only_turn is not None:
            if state["turn"] < only_turn:
                # The projector has not written this turn yet. Hold the deltas:
                # dropping them here would silently lose the opening words of
                # every reply, because the projector lags the model by a poll.
                _debug(f"hold: turn {state['turn']} < target {only_turn}")
                return None
            if state["turn"] > only_turn:
                # A newer turn already opened, so these deltas belong to a turn
                # that is over. They can never be placed correctly.
                return {"dropped": True}
        seq = state["maxSeq"]
        floor = state["maxTime"]
        for event in events:
            if fill_placement:
                event["data"]["turn"] = state["turn"]
                event["data"]["step"] = state["step"]
            # Both writers append to one log the browser folds by time, so a
            # delta may never carry a stamp below what is already there.
            when = event.get("time")
            if isinstance(when, int) and isinstance(floor, int) and when < floor:
                event["time"] = floor
            elif isinstance(when, int):
                floor = when
            seq += 1
            event["seq"] = seq
        fh.seek(0, os.SEEK_END)
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + NEWLINE)
        fh.flush()
        return {"seq": seq, "turn": state["turn"], "step": state["step"]}
    except OSError as exc:
        _debug(f"append failed: {exc}")
        return None
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def append_events(path, events):
    """Append projector-owned events, renumbered past anything a streamer wrote."""
    return _locked_append(path, events)


def _chunk_events(chunks):
    now = int(time.time() * 1000)
    return [{"type": "assistant/chunk", "time": now,
             "data": {"turn": 0, "step": 0, "chunk": chunk}} for chunk in chunks]


def _owns(params, turn_id, method):
    """Whether a notification belongs to the turn this streamer is following.

    A streamer is pinned to one turn id, so a late notification from a previous
    turn can never be written into the step that is currently open. Connection
    lifecycle notifications carry no turn id and are always accepted.
    """
    if turn_id is None:
        return True
    if method in ("thread/status/changed", "thread/closed", "thread/tokenUsage/updated",
                  "account/rateLimits/updated", "thread/goal/cleared",
                  "mcpServer/startupStatus/updated", "error"):
        return True
    reported = params.get("turnId") or (params.get("turn") or {}).get("id")
    return reported in (None, turn_id)


def spawn_streamer(ws, thread_id, session_id, base_turn, turn_id=None):
    """Hand the live socket to a detached child and return immediately.

    The child must not hold the parent's stdio open: the bridge is invoked with
    spawnSync, which would block until every inherited pipe closed.
    """
    try:
        pid = os.fork()
    except OSError:
        return None
    if pid != 0:
        return pid
    try:
        os.setsid()
    except OSError:
        pass
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                pass
        if devnull > 2:
            os.close(devnull)
    except OSError:
        pass
    try:
        stream_turn(ws, thread_id, session_id, base_turn, turn_id)
    except BaseException:
        pass
    finally:
        os._exit(0)


def stream_turn(ws, thread_id, session_id, base_turn, turn_id=None):
    """Forward item-scoped deltas; the projector owns their step placement."""
    from projection_writer import send_chunks
    from codex_link import WSError

    pending = []
    items = {}
    predecessor = None
    last_flush = 0.0
    completed_at = None
    idle_deadline = time.monotonic() + IDLE_TIMEOUT_S
    hard_deadline = time.monotonic() + HARD_TIMEOUT_S

    def flush():
        nonlocal last_flush
        while pending:
            batch = pending[0]
            result = send_chunks(session_id, batch["chunks"], item_id=batch["id"],
                                 turn_id=batch["turn"], after_item_id=batch["after"])
            _debug(f"flush item={batch['id']} after={batch['after']} result={result}")
            if result is None:
                break
            pending.pop(0)
        last_flush = time.monotonic()

    def delta(item_id, source_turn, kind, text, channel=None, part=0):
        if not item_id or not source_turn or not text:
            return
        state = items.setdefault(item_id, {"after": predecessor, "started": False,
                                          "channel": channel, "part": part})
        # Raw text and its summary are alternative representations, not two
        # consecutive paragraphs. Prefer exposed text just like the replay path.
        if state["channel"] == "raw" and channel == "summary":
            return
        restart = channel == "raw" and state["channel"] == "summary"
        if not pending or pending[-1]["id"] != item_id:
            pending.append({"id": item_id, "turn": source_turn,
                            "after": state["after"], "chunks": []})
        chunks = pending[-1]["chunks"]
        if not state["started"] or restart:
            chunks.append({"type": "block-start", "index": 0, "blockType": kind})
            state["started"] = True
        elif part != state["part"]:
            text = "\n" + text
        state["channel"] = channel
        state["part"] = part
        chunk_type = "reasoning-delta" if kind == "reasoning" else "text-delta"
        if chunks and chunks[-1]["type"] == chunk_type:
            chunks[-1]["text"] += text
        else:
            chunks.append({"type": chunk_type, "index": 0, "text": text})

    while time.monotonic() < min(hard_deadline, idle_deadline):
        try:
            message = ws.recv_notification(timeout=FLUSH_INTERVAL_S)
        except (TimeoutError, socket.timeout):
            message = None
        except (OSError, ValueError, WSError):
            break
        if message:
            idle_deadline = time.monotonic() + IDLE_TIMEOUT_S
            method = message.get("method")
            params = message.get("params") or {}
            _debug(f"notify method={method} item={params.get('itemId') or (params.get('item') or {}).get('id')} "
                   f"characters={len(params.get('delta') or '')}")
            if params.get("threadId") not in (None, thread_id) or not _owns(params, turn_id, method):
                continue
            source_turn = params.get("turnId") or turn_id
            if method == "item/started":
                item = params.get("item") or {}
                if item.get("id"):
                    items.setdefault(item["id"], {"after": predecessor, "started": False,
                                                  "channel": None, "part": 0})
            elif method == "item/completed":
                item = params.get("item") or {}
                predecessor = item.get("id") or predecessor
            elif method == "item/agentMessage/delta":
                delta(params.get("itemId"), source_turn, "text", params.get("delta"))
            elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
                raw = method == "item/reasoning/textDelta"
                delta(params.get("itemId"), source_turn, "reasoning", params.get("delta"),
                      "raw" if raw else "summary",
                      params.get("contentIndex" if raw else "summaryIndex", 0))
            elif method == "turn/completed":
                completed_at = time.monotonic()
        if pending and time.monotonic() - last_flush >= FLUSH_INTERVAL_S:
            flush()
        if completed_at is not None:
            if not pending or time.monotonic() - completed_at > 10:
                break
    flush()


def main(argv):
    if len(argv) < 2:
        print("usage: codex_stream.py tail <sessionId>")
        return 1
    if argv[1] == "tail":
        print(json.dumps(read_tail(argv[2]), ensure_ascii=False))
        return 0
    print("unknown action")
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv))
