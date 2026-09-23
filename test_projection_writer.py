"""The web streamer hands Codex deltas to one projection owner."""

import os
import json
import tempfile
import threading
import unittest
from unittest import mock

import projection_writer
import codex_stream
import codex_pending


class ProjectionWriterTests(unittest.TestCase):
    def test_blank_thread_log_is_created_by_projector(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.multiple(codex_pending,
                                     BASE_DIR=root,
                                     FILE=os.path.join(root, "pending-threads.json"),
                                     LOCK=os.path.join(root, "projection.lock")):
                codex_pending.register("thread-one", "workspace-one", "/project")
                log = codex_pending.session_file("thread-one", "/project")
                self.assertFalse(os.path.exists(log))
                codex_pending.materialize("thread-one", codex_pending.load()["thread-one"])
                self.assertTrue(os.path.isfile(log))

    def test_socket_request_is_written_by_daemon_drain(self):
        with tempfile.TemporaryDirectory() as root:
            socket_path = os.path.join(root, "writer.sock")
            observed = []
            writer = projection_writer.ProjectionWriter(
                socket_path, append=lambda request: observed.append(request) or
                {"seq": 7, "turn": 2, "step": 1})
            response = []
            sender = threading.Thread(target=lambda: response.append(
                projection_writer.send_chunks(
                    "session-test", [{"type": "text-delta", "text": "中文", "index": 0}],
                    min_turn=1, socket_path=socket_path)))
            try:
                sender.start()
                self.assertTrue(writer.drain(timeout=2))
                sender.join(timeout=2)
                self.assertFalse(sender.is_alive())
                self.assertEqual(response[0]["seq"], 7)
                self.assertEqual(observed[0]["chunks"][0]["text"], "中文")
                self.assertEqual(observed[0]["minTurn"], 1)
            finally:
                writer.close()

    def test_daemon_rejects_missing_session_without_creating_a_log(self):
        with mock.patch.object(projection_writer.codex_stream,
                               "find_session_file", return_value=None):
            self.assertIsNone(projection_writer.ProjectionWriter._append({
                "sessionId": "session-missing", "chunks": []}))

    def test_real_log_append_keeps_one_sequence_and_step(self):
        with tempfile.TemporaryDirectory() as root:
            log_dir = os.path.join(root, "sessions", "workspace", "session-test")
            os.makedirs(log_dir)
            log = os.path.join(log_dir, "session.jsonl")
            lines = [
                {"type": "session", "version": 0, "id": "session-test",
                 "createdAt": 1, "cwd": root, "delegationDepth": 0},
                {"seq": 0, "type": "turn/start", "time": 1, "data": {"turn": 1}},
                {"seq": 1, "type": "step/start", "time": 1,
                 "data": {"turn": 1, "step": 1}},
            ]
            with open(log, "w", encoding="utf-8") as handle:
                handle.writelines(json.dumps(line) + "\n" for line in lines)
            with mock.patch.object(codex_stream, "SESSIONS_ROOT",
                                   os.path.join(root, "sessions")):
                writer = projection_writer.ProjectionWriter(
                    os.path.join(root, "writer.sock"))
                response = []
                sender = threading.Thread(target=lambda: response.append(
                    projection_writer.send_chunks(
                        "session-test", [{"type": "text-delta", "text": "中文", "index": 0}],
                        min_turn=0, socket_path=writer.socket_path)))
                try:
                    sender.start()
                    self.assertTrue(writer.drain(timeout=2))
                    sender.join(timeout=2)
                    self.assertEqual(response[0]["seq"], 2)
                    with open(log, encoding="utf-8") as handle:
                        event = json.loads(handle.readlines()[-1])
                    self.assertEqual(event["seq"], 2)
                    self.assertEqual(event["data"]["turn"], 1)
                    self.assertEqual(event["data"]["step"], 1)
                    self.assertEqual(event["data"]["chunk"]["text"], "中文")
                finally:
                    writer.close()


if __name__ == "__main__":
    unittest.main()
