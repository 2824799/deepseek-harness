"""Exercise the Codex rollout to DSH log path for an unfinished turn."""

import json
import sqlite3
from unittest import mock

import codex_live
import codex_pending
import setup_isolated


def test_completed_reasoning_is_visible_before_the_next_answer(tmp_path):
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

    append(
        completed(1, {"type": "UserMessage", "content": [
            {"type": "text", "text": "question"}]}),
        completed(2, {"type": "Reasoning", "raw_content": ["thinking"],
                      "summary_text": []}),
    )

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
          mock.patch.object(codex_live, "scan_projects", return_value={workspace: "workspace"}),
          mock.patch.object(codex_live, "project_ids", return_value={workspace: "project-one"}),
          mock.patch.object(codex_live, "read", return_value={}),
          mock.patch.object(codex_live, "scan_running", return_value={}),
          mock.patch.object(codex_live, "publish"),
          mock.patch.object(codex_pending, "load", return_value={}),
          mock.patch.object(codex_pending, "prune", side_effect=lambda entries, _: entries)):
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
