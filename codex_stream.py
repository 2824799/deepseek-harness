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
import time

NEWLINE = chr(10)
SESSIONS_ROOT = os.path.join(os.environ.get("DSH_HOME") or
                             "/home/nahida/agents/sever/dsh/.dsh-codex", "sessions")
TAIL_WINDOW = 1 << 20
STRUCTURAL = ("turn/start", "turn/end", "step/start", "step/end")
IDLE_TIMEOUT_S = 300
HARD_TIMEOUT_S = 7200
FLUSH_INTERVAL_S = 0.08
DEBUG_LOG = os.environ.get("DSH_CODEX_STREAM_DEBUG")


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
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    start = max(0, size - TAIL_WINDOW)
    fh.seek(start)
    data = fh.read()
    return _fold(data, start, size)


def _fold(data, start, size):
    """Fold a log slice into the structural state it ends in."""
    torn_at = None
    if data and not data.endswith(NEWLINE):
        cut = data.rfind(NEWLINE)
        torn_at = start + cut + 1
        data = data[:cut + 1] if cut >= 0 else ""
    lines = data.split(NEWLINE)
    if start > 0 and lines:
        lines = lines[1:]
    state = {"maxSeq": -1, "maxTime": None, "turn": 0, "step": 0,
             "stepOpen": False, "turnOpen": False, "tornAt": torn_at,
             "stepHasChunks": False}
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
    reported = params.get("turnId")
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
    """Follow one turn's deltas and append them until the turn ends."""
    _debug(f"stream_turn start thread={thread_id} session={session_id} "
           f"base_turn={base_turn} turn_id={turn_id}")
    path = None
    for _ in range(150):
        path = find_session_file(session_id)
        if path:
            break
        time.sleep(0.2)
    if not path:
        _debug("no session file found; aborting")
        return
    _debug(f"session file {path}")

    index_of = {}
    next_index = [0]
    kind_of = {}
    buffer = []
    placement = {"turn": None, "step": None}
    last_flush = [0.0]

    def reset_blocks():
        """Forget the block layout of a step that has closed.

        The projector closes a step as soon as Codex reports the item complete,
        which can be a few milliseconds before the last delta reaches this
        socket. Those trailing chunks must not land in the next step, so the
        layout is rebuilt from the new step's own item/started notifications.
        """
        index_of.clear()
        kind_of.clear()
        del next_index[:]
        next_index.append(0)
        buffer.clear()

    def block_for(item_id, kind):
        """The stream index for one item, opening its block on first use.

        A block is opened lazily because a provider that reports no reasoning
        summary still announces its reasoning items: opening a block on the
        announcement alone would paint empty reasoning bubbles while the answer
        streams.
        """
        index = index_of.get(item_id)
        if index is None:
            index = next_index[0]
            next_index[0] += 1
            index_of[item_id] = index
            kind_of[item_id] = kind
            buffer.append({"type": "block-start", "index": index, "blockType": kind})
        return index

    def flush():
        """Forward buffered Codex deltas to the projection writer."""
        if not buffer:
            return True
        from projection_writer import send_chunks
        result = send_chunks(
            session_id, buffer,
            min_turn=base_turn if placement["turn"] is None else None,
            only_turn=placement["turn"])
        _debug(f"flush n={len(buffer)} placement={placement} result={result}")
        if result is None:
            return False
        if result.get("dropped"):
            buffer.clear()
            return True
        if placement["turn"] is None:
            # Lock onto the turn the log actually opened for these deltas. That
            # is the turn the model is answering in, whatever ordinal the
            # projector assigned it.
            placement["turn"] = result["turn"]
            placement["step"] = result["step"]
        elif result["step"] != placement["step"]:
            placement["step"] = result["step"]
            reset_blocks()
            return True
        buffer.clear()
        last_flush[0] = time.time()
        return True

    ws.sock.settimeout(0.5)
    idle_deadline = time.time() + IDLE_TIMEOUT_S
    hard_deadline = time.time() + HARD_TIMEOUT_S

    while time.time() < hard_deadline and time.time() < idle_deadline:
        line = None
        try:
            line = ws.recv_text(timeout=0.5)
        except Exception:
            line = None
        if line:
            idle_deadline = time.time() + IDLE_TIMEOUT_S
            try:
                message = json.loads(line)
            except Exception:
                message = None
            method = (message or {}).get("method")
            if method:
                _debug(f"method {method}")
                params = message.get("params") or {}
                if params.get("threadId") in (None, thread_id) and _owns(params, turn_id, method):
                    if method == "item/started":
                        item = params.get("item") or {}
                        kind = item.get("type")
                        item_id = item.get("id")
                        if item_id is not None and kind in ("agentMessage", "reasoning"):
                            kind_of.setdefault(item_id, "text" if kind == "agentMessage" else "reasoning")
                    elif method == "item/agentMessage/delta":
                        item_id = params.get("itemId")
                        delta = params.get("delta") or ""
                        if item_id and delta:
                            index = block_for(item_id, kind_of.get(item_id, "text"))
                            buffer.append({"type": "text-delta", "index": index, "text": delta})
                    elif method in ("item/reasoning/summaryTextDelta",
                                    "item/reasoning/textDelta"):
                        item_id = params.get("itemId")
                        delta = params.get("delta") or ""
                        if item_id and delta:
                            index = block_for(item_id, "reasoning")
                            buffer.append({"type": "reasoning-delta", "index": index, "text": delta})
                    elif method == "turn/completed":
                        _debug("turn/completed; flushing and exiting")
                        flush()
                        return
        if buffer and time.time() - last_flush[0] >= FLUSH_INTERVAL_S:
            flush()
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
