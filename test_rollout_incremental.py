"""Read-only regression checks for the rollout metadata byte cursor."""

import json
import os
import tempfile
import unittest
import sqlite3
from unittest import mock

import setup_isolated


def record(ordinal, kind, turn="turn-1", **fields):
    return {"type": "event_msg", "ordinal": ordinal,
            "timestamp": "2026-09-23T00:00:00Z",
            "payload": {"type": kind, "turn_id": turn, **fields}}


class RolloutIncrementalTests(unittest.TestCase):
    def setUp(self):
        setup_isolated._ROLLOUT_META_CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rollout-test.jsonl")

    def tearDown(self):
        setup_isolated._ROLLOUT_META_CACHE.clear()
        self.tmp.cleanup()

    def append(self, *records):
        with open(self.path, "a", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    def compare(self, after):
        incremental = setup_isolated.read_rollout_meta(
            self.path, after_ordinal=after, incremental=True)
        complete = setup_isolated.read_rollout_meta(self.path, after_ordinal=after)
        self.assertEqual(incremental, complete)
        return incremental

    def test_append_reuses_prefix_and_preserves_usage(self):
        self.append(record(1, "item_completed", item={"type": "UserMessage",
                     "id": "first", "content": [{"type": "text", "text": "中文"}]}),
                    {"type": "token_usage_record", "ordinal": 2,
                     "payload": {"usage": {"input_tokens": 100,
                                           "cached_input_tokens": 90}}})
        self.compare(-1)
        self.append(record(3, "item_completed", item={"type": "AgentMessage",
                     "id": "second", "content": [{"type": "Text", "text": "回复"}]}),
                    record(4, "task_complete", duration_ms=600,
                           time_to_first_token_ms=25))
        timings, usage, recovered = self.compare(2)
        self.assertEqual([item[2] for item in recovered], [3])
        self.assertEqual(usage[0][0], 2)
        self.assertEqual(timings["turn-1"]["ttftMs"], 25)
        self.assertEqual(self.compare(-1)[2][0][2], 1)

    def test_partial_line_is_held_until_newline(self):
        self.append(record(1, "item_completed", item={"type": "UserMessage",
                     "id": "one", "content": [{"type": "text", "text": "one"}]}))
        self.compare(-1)
        second = json.dumps(record(2, "item_completed", item={
            "type": "UserMessage", "id": "two",
            "content": [{"type": "text", "text": "two"}]}))
        with open(self.path, "ab") as handle:
            handle.write(second.encode("utf-8")[:-1])
        self.assertEqual(len(setup_isolated.read_rollout_meta(
            self.path, incremental=True)[2]), 1)
        with open(self.path, "ab") as handle:
            handle.write(second.encode("utf-8")[-1:] + b"\n")
        self.assertEqual([item[2] for item in self.compare(-1)[2]], [1, 2])

    def test_rewrite_invalidates_cached_offset(self):
        self.append(record(1, "item_completed", item={"type": "UserMessage",
                     "id": "old", "content": [{"type": "text", "text": "old"}]}))
        self.compare(-1)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record(1, "item_completed", item={
                "type": "UserMessage", "id": "new",
                "content": [{"type": "text", "text": "new"}]})) + "\n")
        stat = os.stat(self.path)
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        self.assertEqual(self.compare(-1)[2][0][0], "new")

    def test_recovery_floor_prunes_old_bodies_but_keeps_usage_index(self):
        self.append(record(1, "item_completed", item={"type": "UserMessage",
                     "id": "one", "content": [{"type": "text", "text": "old"}]}),
                    {"type": "token_usage_record", "ordinal": 2,
                     "payload": {"usage": {"input_tokens": 100}}},
                    record(3, "item_completed", item={"type": "AgentMessage",
                     "id": "two", "content": [{"type": "Text", "text": "new"}]}))
        first = setup_isolated.read_rollout_meta(
            self.path, after_ordinal=0, incremental=True, include_item_turns=True)
        self.assertEqual(len(first[2]), 2)
        later = setup_isolated.read_rollout_meta(
            self.path, after_ordinal=1, incremental=True, include_item_turns=True)
        self.assertEqual([item[2] for item in later[2]], [3])
        cached = setup_isolated._ROLLOUT_META_CACHE[self.path]
        self.assertEqual([item[2] for item in cached["recovered"]], [3])
        self.assertEqual(later[3], {1: "turn-1", 3: "turn-1"})
        self.assertEqual(setup_isolated.attribute_usage(later[3], later[1])[
            "turn-1"]["inputTokens"], 100)

    def test_unchanged_snapshot_keeps_its_inode_and_mtime(self):
        snapshot = os.path.join(self.tmp.name, "snapshot.json")
        self.assertTrue(setup_isolated.write_json_if_changed(snapshot, {"x": 1}))
        before = os.stat(snapshot)
        self.assertFalse(setup_isolated.write_json_if_changed(snapshot, {"x": 1}))
        after = os.stat(snapshot)
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))
        self.assertTrue(setup_isolated.write_json_if_changed(snapshot, {"x": 2}))

    def test_history_summary_skips_unchanged_sqlite_and_tracks_wal_appends(self):
        database = os.path.join(self.tmp.name, "history.sqlite")
        with sqlite3.connect(database) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE thread_items (thread_id TEXT, rollout_ordinal INTEGER, created_at_ms INTEGER)")
            conn.execute("INSERT INTO thread_items VALUES ('one', 1, 10)")
            conn.commit()
            statements = []
            conn.set_trace_callback(statements.append)
            with mock.patch.object(setup_isolated, "CODEX_HISTORY", database):
                self.assertEqual(setup_isolated.history_summary(conn), [("one", 1, 1, 10)])
                self.assertEqual(setup_isolated.history_summary(conn), [("one", 1, 1, 10)])
                self.assertEqual(len(statements), 1)
                conn.execute("INSERT INTO thread_items VALUES ('one', 2, 20)")
                conn.commit()
                self.assertEqual(setup_isolated.history_summary(conn), [("one", 2, 2, 10)])


if __name__ == "__main__":
    unittest.main()
