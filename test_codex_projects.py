"""Workspace commands must mutate Codex and leave the DSH snapshot untouched."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import codex_projects


class ProjectConnection:
    def __init__(self, project):
        self.projects = [project]
        self.calls = []
        self.closed = False

    def call(self, method, params, timeout=0):
        self.calls.append((method, params))
        if method == "project/list":
            return {"ok": True, "value": {"data": self.projects, "nextCursor": None}}
        if method == "project/create":
            project = {"id": "new", "name": params["name"], "roots": params["roots"],
                       "createdAt": 1000000000}
            self.projects.append(project)
            return {"ok": True, "value": {"project": project}}
        if method == "project/update":
            project = next(item for item in self.projects if item["id"] == params["projectId"])
            project["name"] = params["name"]
            return {"ok": True, "value": {"project": project}}
        if method == "project/delete":
            self.projects = []
            return {"ok": True, "value": {}}
        if method in ("project/move", "thread/metadata/update"):
            return {"ok": True, "value": {}}
        raise AssertionError(method)

    def close(self):
        self.closed = True


class CodexProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "existing")
        self.new_root = os.path.join(self.tmp.name, "new")
        os.mkdir(self.root)
        os.mkdir(self.new_root)
        self.storage = os.path.join(self.tmp.name, "workspace.json")
        self.workspace_id = "existing-web-id"
        with open(self.storage, "w", encoding="utf-8") as handle:
            json.dump({"tables": {"workspaces": {
                self.workspace_id: {"path": self.root, "title": "old",
                                    "sessionIds": ["session-older"],
                                    "createdAt": "2026-09-20T00:00:00Z"}
            }}}, handle)
        with open(self.storage, "rb") as handle:
            self.before = handle.read()
        self.project = {"id": "native-project-id", "name": "old",
                        "roots": [{"path": self.root}], "createdAt": 1000000000}
        self.ws = ProjectConnection(self.project)
        storage_patch = patch.object(codex_projects, "STORAGE", self.storage)
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        connection_patch = patch.object(codex_projects.codex_link, "connect",
                                        return_value=self.ws)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)

    def assert_snapshot_untouched(self):
        with open(self.storage, "rb") as handle:
            self.assertEqual(handle.read(), self.before)
        self.assertTrue(self.ws.closed)

    def test_create_routes_to_codex_and_preserves_existing_id(self):
        existing = codex_projects.execute("create", {"path": self.root})
        self.assertFalse(existing["created"])
        self.assertEqual(existing["workspace"]["workspaceId"], self.workspace_id)
        created = codex_projects.execute("create", {"path": self.new_root})
        self.assertTrue(created["created"])
        self.assertEqual(created["workspace"]["path"], self.new_root)
        # The projector has not updated workspace.json yet. A second browser
        # action must still resolve the Codex project by its new web ID.
        renamed = codex_projects.execute("rename", {
            "workspaceId": created["workspace"]["workspaceId"], "title": "new title"})
        self.assertEqual(renamed["workspace"]["title"], "new title")
        self.assertIn("project/create", [method for method, _ in self.ws.calls])
        self.assert_snapshot_untouched()

    def test_rename_delete_move_and_assign_use_codex(self):
        renamed = codex_projects.execute("rename", {
            "workspaceId": self.workspace_id, "title": "updated"})
        self.assertEqual(renamed["workspace"]["sessionIds"], ["session-older"])
        moved = codex_projects.execute("insertSessionBefore", {
            "workspaceId": self.workspace_id, "sessionId": "session-new"})
        self.assertEqual(moved["workspace"]["sessionIds"],
                         ["session-new", "session-older"])
        codex_projects.execute("insertBefore", {"workspaceId": self.workspace_id})
        codex_projects.execute("delete", {"workspaceId": self.workspace_id})
        self.assertEqual([method for method, _ in self.ws.calls if method != "project/list"],
                         ["project/update", "thread/metadata/update",
                          "project/move", "project/delete"])
        self.assert_snapshot_untouched()

    def test_manual_thread_order_is_rejected_without_partial_project_change(self):
        with self.assertRaisesRegex(ValueError, "manual conversation order"):
            codex_projects.execute("insertSessionBefore", {
                "workspaceId": self.workspace_id, "sessionId": "session-new",
                "beforeSessionId": "session-older"})
        self.assertNotIn("thread/metadata/update", [method for method, _ in self.ws.calls])
        self.assert_snapshot_untouched()


if __name__ == "__main__":
    unittest.main()
