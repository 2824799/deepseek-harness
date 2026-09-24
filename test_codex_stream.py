"""Codex notifications retain item identity across streaming and settlements."""

import json
import os
from collections import deque
from unittest import mock

import codex_link
import codex_stream
import projection_writer


def test_turn_start_retains_early_notifications():
    ws = codex_link.WSClient.__new__(codex_link.WSClient)
    ws._next_id = 1
    ws._notifications = deque()
    early = {"method": "item/agentMessage/delta", "params": {"delta": "first"}}
    frames = iter([json.dumps(early), json.dumps({"id": 1, "result": {"turn": {"id": "t"}}})])
    with (mock.patch.object(ws, "send_text"),
          mock.patch.object(ws, "recv_text", side_effect=lambda **_: next(frames))):
        assert ws.call("turn/start", {})["ok"]
        assert ws.recv_notification() == early


def test_stream_keeps_pending_prefix_and_uses_distinct_items():
    def event(method, **params):
        return {"method": method, "params": {"threadId": "thread", "turnId": "turn", **params}}

    events = iter([
        event("item/completed", item={"id": "user"}),
        event("item/started", item={"id": "reason", "type": "reasoning"}),
        event("item/reasoning/summaryTextDelta", itemId="reason", delta="short summary"),
        event("item/reasoning/textDelta", itemId="reason", delta="exposed reasoning"),
        event("item/reasoning/summaryTextDelta", itemId="reason", delta="unused summary"),
        event("item/completed", item={"id": "reason"}),
        event("item/completed", item={"id": "tool"}),
        event("item/started", item={"id": "answer", "type": "agentMessage"}),
        event("item/agentMessage/delta", itemId="answer", delta="first "),
        event("item/agentMessage/delta", itemId="answer", delta="second"),
        event("turn/completed"),
    ])
    ws = mock.Mock()
    ws.recv_notification.side_effect = lambda **_: next(events)
    seen = []
    attempts = 0

    def send(session_id, chunks, **placement):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return None  # The user message is still waiting to be projected.
        seen.append((placement, list(map(dict, chunks))))
        return {"turn": 1, "step": 1 if placement["item_id"] == "reason" else 3}

    clock = iter(i * 0.1 for i in range(1000))
    with (mock.patch.object(projection_writer, "send_chunks", side_effect=send),
          mock.patch.object(codex_stream.time, "monotonic", side_effect=lambda: next(clock))):
        codex_stream.stream_turn(ws, "thread", "session-test", 0, "turn")
    assert all(placement["turn_id"] == "turn" for placement, _ in seen)
    reasoning = [chunk for place, chunks in seen if place["item_id"] == "reason" for chunk in chunks]
    # The summary is replaced when raw text is provided, and a later summary
    # cannot be appended to the raw text.
    assert [chunk["text"] for chunk in reasoning if "text" in chunk] == [
        "short summary", "exposed reasoning"]
    answer = [(place, chunks) for place, chunks in seen if place["item_id"] == "answer"]
    assert all(place["after_item_id"] == "tool" for place, _ in answer)
    assert "".join(chunk.get("text", "") for _, chunks in answer for chunk in chunks) == "first second"
    assert all(chunk["index"] == 0 for _, chunks in seen for chunk in chunks)


def test_completed_notifications_from_another_turn_are_rejected():
    assert not codex_stream._owns({"turn": {"id": "old"}}, "current", "turn/completed")


def test_tail_cache_reads_only_new_bytes_and_preserves_unicode_fragments(tmp_path):
    path = tmp_path / "session.jsonl"
    row = lambda seq, kind, data: json.dumps({"seq": seq, "type": kind, "time": seq, "data": data}, ensure_ascii=False).encode() + b"\n"
    path.write_bytes(row(0, "turn/start", {"turn": 1}) + row(1, "step/start", {"turn": 1, "step": 1}))
    with path.open("a+", encoding="utf-8") as handle:
        state = codex_stream._scan(handle)
        assert state["stepOpen"]
        start = path.stat().st_size
        chunk = row(2, "assistant/chunk", {"turn": 1, "step": 1,
                    "chunk": {"type": "text-delta", "index": 0, "text": "中文回复"}})
        cut = chunk.index("中文".encode()) + 1  # Half of a UTF-8 character.
        with path.open("ab") as writer:
            writer.write(chunk[:cut])
        with mock.patch.object(codex_stream.os, "pread", wraps=os.pread) as read:
            assert codex_stream._scan(handle)["tornAt"] == start
            assert read.call_args.args[2] == start
        with path.open("ab") as writer:
            writer.write(chunk[cut:])
        state = codex_stream._scan(handle)
        assert state["maxSeq"] == 2
        assert state["stepHasChunks"] and state["tornAt"] is None
        with mock.patch.object(codex_stream.os, "pread", wraps=os.pread) as read:
            assert codex_stream._scan(handle) == state
            read.assert_not_called()
