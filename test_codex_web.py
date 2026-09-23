"""Focused regression checks for the Codex-backed browser adapter."""

import json
import sqlite3
from unittest import mock

import codex_link
import codex_live


class FakeSocket:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        return self.replies[method]

    def close(self):
        pass


def test_stop_uses_active_turn_id():
    ws = FakeSocket({
        "thread/turns/list": {"ok": True, "value": {"data": [
            {"id": "turn-1", "status": "inProgress"}]}},
        "turn/interrupt": {"ok": True, "value": {}},
    })
    with mock.patch.object(codex_link, "connect", return_value=ws):
        result = codex_link.interrupt_turn("thread-1")
    assert result["interrupted"] is True
    assert ws.calls[-1] == ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"})


def test_queue_choice_does_not_steer_active_turn():
    ws = FakeSocket({
        "thread/resume": {"ok": True, "value": {}},
        "thread/turns/list": {"ok": True, "value": {"data": [
            {"id": "turn-1", "status": "inProgress"}]}},
    })
    with (mock.patch.object(codex_link, "connect", return_value=ws),
          mock.patch.object(codex_link, "queue_prompt", return_value={"ok": True, "mode": "queue"}) as queued):
        result = codex_link.send_prompt("thread-1", {"mode": "queue", "text": "hello"})
    assert result["mode"] == "queue"
    assert queued.call_count == 1
    assert all(method != "turn/steer" for method, _ in ws.calls)


def test_empty_project_list_replaces_previous_snapshot(tmp_path):
    live_file = tmp_path / "live-state.json"
    live_file.write_text(json.dumps({"projects": {"/old": "old"}, "projectsAt": 1}), encoding="utf-8")
    ws = FakeSocket({"project/list": {"ok": True, "value": {"data": [], "nextCursor": None}}})
    with (mock.patch.object(codex_live, "LIVE_FILE", str(live_file)),
          mock.patch.object(codex_link, "connect", return_value=ws)):
        projects = codex_live.scan_projects()
        result = codex_live.publish({}, projects, {})
    assert projects == {}
    assert result["projects"] == {}


def test_project_list_reads_all_pages(tmp_path):
    replies = iter([
        {"ok": True, "value": {"data": [{"name": "one", "roots": [{"path": "/one"}]}], "nextCursor": "next"}},
        {"ok": True, "value": {"data": [{"name": "two", "roots": [{"path": "/two"}]}], "nextCursor": None}},
    ])
    class PagedSocket:
        def call(self, method, params, timeout=None):
            return next(replies)

        def close(self):
            pass

    with (mock.patch.object(codex_live, "LIVE_FILE", str(tmp_path / "missing.json")),
          mock.patch.object(codex_link, "connect", side_effect=[PagedSocket(), PagedSocket()])):
        assert codex_live.scan_projects() == {"/one": "one", "/two": "two"}


def test_archive_fallback_rejects_missing_thread(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    db = sqlite3.connect(codex_dir / "state_5.sqlite")
    db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER, archived_at INTEGER)")
    db.commit()
    db.close()
    ws = FakeSocket({"thread/archive": {"ok": False, "error": {"message": "active writer"}}})
    with (mock.patch.object(codex_link, "connect", return_value=ws),
          mock.patch.object(codex_link, "HOME", str(tmp_path))):
        result = codex_link.set_archived("gone", True)
    assert result["ok"] is False
    assert "no longer exists" in result["error"]


def test_queue_keeps_selected_model_and_images(tmp_path):
    image = tmp_path / "picture.png"
    image.write_bytes(b"image")
    completed = mock.Mock(returncode=0, stdout="", stderr="")
    with (mock.patch.object(codex_link, "load_selections", return_value={"thread-1": {"model": "A6-C/deepseek-v4.1-flash"}}),
          mock.patch("subprocess.run", return_value=completed) as run):
        result = codex_link.queue_prompt("thread-1", [{"type": "text", "text": "hello"}], image_paths=[str(image)])
    command = run.call_args.args[0]
    assert result["mode"] == "queue"
    assert command[command.index("-m") + 1] == "A6-C/deepseek-v4.1-flash"
    assert command[command.index("-i") + 1] == str(image)


def test_running_scan_ignores_events_after_task_complete(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    def event(kind):
        return json.dumps({"type": "event_msg", "payload": {"type": kind}})
    rollout.write_text("\n".join([event("task_started"), event("task_complete"), event("item_completed")]), encoding="utf-8")
    assert codex_live._tail_terminal(rollout) is True
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write("\n" + event("task_started") + "\n")
    assert codex_live._tail_terminal(rollout) is False
