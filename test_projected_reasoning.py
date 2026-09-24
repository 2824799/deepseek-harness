"""Exercise the Codex rollout to DSH log path for an unfinished turn."""

import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import codex_live
import codex_stream
import codex_pending
import setup_isolated


@contextmanager
def projected_session(tmp_path):
    state_db = tmp_path / "state.sqlite"
    history_db = tmp_path / "history.sqlite"
    rollout = tmp_path / "rollout.jsonl"
    dsh_home = tmp_path / "dsh"
    workspace = str(tmp_path / "workspace")
    thread_id = "00000000-0000-0000-0000-000000000001"
    turn_id = "turn-one"
    base_ms = 1790160000000

    with sqlite3.connect(state_db) as db:
        db.execute("CREATE TABLE projects (id TEXT, name TEXT)")
        db.execute("INSERT INTO projects VALUES (?, ?)", ("project-one", "workspace"))
        db.execute("""CREATE TABLE threads (
            id TEXT, title TEXT, name TEXT, created_at INTEGER, updated_at INTEGER,
            cwd TEXT, project_id TEXT, rollout_path TEXT, archived INTEGER)""")
        db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (thread_id, "test", "test", base_ms // 1000,
                    base_ms // 1000, workspace, "project-one", str(rollout), 0))
    with sqlite3.connect(history_db) as db:
        db.execute("""CREATE TABLE thread_items (
            thread_id TEXT, item_id TEXT, item_type TEXT, rollout_ordinal INTEGER,
            created_at_ms INTEGER, item_json TEXT, turn_id TEXT)""")

    def completed(ordinal, item):
        return {"type": "event_msg", "ordinal": ordinal,
                "timestamp": "2026-09-23T12:00:00Z",
                "payload": {"type": "item_completed", "turn_id": turn_id,
                            "completed_at_ms": base_ms + ordinal * 1000,
                            "item": {"id": f"item-{ordinal}", **item}}}

    def append(*records):
        with rollout.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


    log = (dsh_home / "sessions" / ("--" + workspace.strip("/").replace("/", "-") + "--")
           / ("session-" + thread_id) / "session.jsonl")

    def events():
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[1:]]

    setup_isolated._ROLLOUT_META_CACHE.clear()

    with (mock.patch.multiple(setup_isolated,
                              BASE_DIR=str(dsh_home),
                              SESSIONS_ROOT=str(dsh_home / "sessions"),
                              STORAGES_ROOT=str(dsh_home / "storages"),
                              STATE_FILE=str(dsh_home / "projection-state.json"),
                              CODEX_STATE=str(state_db), CODEX_HISTORY=str(history_db)),
          mock.patch.object(setup_isolated, "build_rollout_index",
                            return_value={thread_id: [str(rollout)]}),
          mock.patch.object(codex_stream, "SESSIONS_ROOT", str(dsh_home / "sessions")),
          mock.patch.object(codex_live, "scan_projects", return_value={workspace: "workspace"}),
          mock.patch.object(codex_live, "project_ids", return_value={workspace: "project-one"}),
          mock.patch.object(codex_live, "read", return_value={}),
          mock.patch.object(codex_live, "scan_running", return_value={}),
          mock.patch.object(codex_live, "publish"),
          mock.patch.object(codex_pending, "load", return_value={}),
          mock.patch.object(codex_pending, "prune", side_effect=lambda entries, _: entries)):
        yield SimpleNamespace(append=append, completed=completed, events=events,
                              home=dsh_home, session_id="session-" + thread_id,
                              turn_id=turn_id, log=log)
    setup_isolated._ROLLOUT_META_CACHE.clear()


def test_completed_reasoning_is_visible_before_the_next_answer(tmp_path):
    with projected_session(tmp_path) as fixture:
        append, completed, events = fixture.append, fixture.completed, fixture.events
        dsh_home, thread_id = fixture.home, fixture.session_id.removeprefix("session-")
        append(
            completed(1, {"type": "UserMessage", "content": [
                {"type": "text", "text": "question"}]}),
            completed(2, {"type": "Reasoning", "raw_content": ["thinking"],
                          "summary_text": []}),
        )
        setup_isolated.run()
        messages = [event for event in events() if event["type"] in
                    ("user/message", "assistant/message")]
        assert [event["type"] for event in messages] == ["user/message", "assistant/message"]
        assert messages[-1]["data"]["message"]["content"] == [
            {"type": "reasoning", "text": "thinking"}]

        setup_isolated.run()
        assert len([e for e in events() if e["type"] == "assistant/message"]) == 1

        append(completed(3, {"type": "AgentMessage", "content": [
            {"type": "Text", "text": "answer"}]}))
        setup_isolated.run()
        contents = [e["data"]["message"]["content"] for e in events()
                    if e["type"] == "assistant/message"]
        assert contents == [[{"type": "reasoning", "text": "thinking"}],
                            [{"type": "text", "text": "answer"}]]
        # DSH settles one assistant row per (turn, step), including on reload.
        # A later answer/tool in the same step replaces the reasoning row.
        settled = {}
        for event in events():
            if event["type"] == "assistant/message":
                data = event["data"]
                settled[(data["turn"], data["step"])] = data["message"]["content"]
        assert list(settled.values()) == contents

        checkpoint_file = dsh_home / "projection-state.json"
        checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        checkpoint["session-" + thread_id]["pending"] = ["previously held thinking"]
        checkpoint_file.write_text(json.dumps(checkpoint), encoding="utf-8")
        setup_isolated.run()
        assert [e["data"]["message"]["content"] for e in events()
                if e["type"] == "assistant/message"][-1] == [
                    {"type": "reasoning", "text": "previously held thinking"}]
        after_recovery = len(events())
        setup_isolated.run()
        assert len(events()) == after_recovery
    setup_isolated._ROLLOUT_META_CACHE.clear()


def test_live_items_keep_their_steps_after_tools_and_replay(tmp_path):
    with projected_session(tmp_path) as fixture:
        def complete(ordinal, item):
            fixture.append(fixture.completed(ordinal, item))
            setup_isolated.run()

        def stream(ordinal, text, after=None, kind="text"):
            return setup_isolated.append_live_chunks({
                "sessionId": fixture.session_id, "itemId": f"item-{ordinal}",
                "turnId": fixture.turn_id,
                "afterItemId": f"item-{after}" if after is not None else None,
                "chunks": [{"type": "block-start", "index": 0, "blockType": kind},
                           {"type": kind + "-delta", "index": 0, "text": text}],
            })

        complete(1, {"type": "UserMessage", "content": [{"type": "text", "text": "question"}]})
        thinking = stream(2, "first thinking", after=1, kind="reasoning")
        assert thinking["step"] == 1
        # The tool is not in the durable source yet, so a future item's
        # deltas cannot be put into the reasoning's current step.
        assert stream(4, "second thinking", after=3, kind="reasoning") is None
        complete(2, {"type": "Reasoning", "raw_content": ["first thinking"]})
        assert stream(2, "late thinking", kind="reasoning") == {"dropped": True}
        assert stream(4, "second thinking", after=3, kind="reasoning") is None
        complete(3, {"type": "CommandExecution", "command": "pwd", "exit_code": 0})
        second = stream(4, "second thinking", after=3, kind="reasoning")
        assert second["step"] > thinking["step"]
        complete(4, {"type": "Reasoning", "raw_content": ["second thinking"]})
        answer = stream(5, "live answer", after=4)
        assert answer["step"] > second["step"]
        live_events = fixture.events()
        assert live_events[-1]["type"] == "assistant/chunk"
        assert live_events[-1]["data"]["chunk"]["text"] == "live answer"
        assert not any(e["type"] == "assistant/message" and
                       any(b.get("text") == "live answer" for b in e["data"]["message"]["content"])
                       for e in live_events)
        complete(5, {"type": "AgentMessage", "content": [{"type": "Text", "text": "live answer"}]})
        messages = [e for e in fixture.events() if e["type"] == "assistant/message"]
        assert [e["data"]["message"]["content"][0]["type"] for e in messages] == [
            "reasoning", "tool-call", "reasoning", "text"]
        assert len({(e["data"]["turn"], e["data"]["step"]) for e in messages}) == 4
        assert messages[-1]["data"]["step"] == answer["step"]
        before = fixture.log.read_bytes()
        setup_isolated.run()
        assert fixture.log.read_bytes() == before
