#!/usr/bin/env python3
"""Route DSH workspace commands to Codex's project APIs.

The projected workspace registry is a read model. This module reads it only to
preserve existing web workspace IDs while Codex remains the sole project owner.
"""

import datetime
import json
import os
import sys
import uuid

import codex_link


STORAGE = os.path.join(os.environ.get("DSH_HOME") or os.path.join(
    os.path.dirname(__file__), ".dsh-codex"), "storages", "workspace.json")


def _previous_workspace(path):
    try:
        with open(STORAGE, encoding="utf-8") as handle:
            entries = json.load(handle).get("tables", {}).get("workspaces") or {}
        for workspace_id, entry in entries.items():
            if entry.get("path") == path:
                return {"workspaceId": workspace_id, **entry}
    except (OSError, ValueError):
        pass
    return {}


def _workspace_id(path):
    return _previous_workspace(path).get("workspaceId") or str(
        uuid.uuid5(uuid.NAMESPACE_URL, path))


def _view(project, path):
    previous = _previous_workspace(path)
    def timestamp(value):
        value = value or 0
        if value > 100_000_000_000:
            value /= 1000
        return datetime.datetime.fromtimestamp(
            value, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return {"workspaceId": _workspace_id(path), "path": path,
            "title": project["name"], "sessionIds": previous.get("sessionIds") or [],
            "createdAt": previous.get("createdAt") or timestamp(project.get("createdAt")),
            "updatedAt": timestamp(project.get("updatedAt") or project.get("createdAt"))}


def _projects(ws):
    projects = []
    cursor = None
    while True:
        result = ws.call("project/list", {"cursor": cursor} if cursor else {}, timeout=15)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error")))
        value = result.get("value") or {}
        projects.extend(value.get("data") or [])
        cursor = value.get("nextCursor")
        if not cursor:
            return projects


def _by_path(projects, path):
    for project in projects:
        for root in project.get("roots") or []:
            if (root.get("path") if isinstance(root, dict) else root) == path:
                return project
    return None


def _checked(ws, method, params):
    result = ws.call(method, params, timeout=20)
    if not result.get("ok"):
        raise RuntimeError(f"{method}: {result.get('error')}")
    return result.get("value") or {}


def execute(action, payload):
    """Apply a workspace action through Codex; never write DSH projection data."""
    ws = codex_link.connect(experimental=True)
    try:
        projects = _projects(ws)
        if action == "create":
            path = os.path.abspath(os.path.expanduser(payload["path"]))
            if not os.path.isdir(path):
                raise ValueError(f"workspace directory does not exist: {path}")
            existing = _by_path(projects, path)
            if existing is not None:
                return {"ok": True, "workspace": _view(existing, path), "created": False}
            value = _checked(ws, "project/create", {
                "idempotencyKey": str(uuid.uuid4()), "name": os.path.basename(path) or path,
                "roots": [{"path": path}],
            })
            return {"ok": True, "workspace": _view(value["project"], path), "created": True}

        snapshot_id = payload.get("workspaceId")
        try:
            with open(STORAGE, encoding="utf-8") as handle:
                entries = json.load(handle).get("tables", {}).get("workspaces") or {}
        except (OSError, ValueError):
            entries = {}
        paths_by_id = {}
        for item in projects:
            for root in item.get("roots") or []:
                root_path = root.get("path") if isinstance(root, dict) else root
                if root_path:
                    paths_by_id[_workspace_id(root_path)] = root_path
        path = (entries.get(snapshot_id) or {}).get("path") or paths_by_id.get(snapshot_id)
        if not path:
            raise ValueError(f"unknown workspace: {snapshot_id}") from None
        project = _by_path(projects, path)
        if project is None:
            raise ValueError(f"Codex project is no longer present: {path}")
        project_id = project["id"]
        if action == "rename":
            title = payload["title"].strip()
            if not title:
                raise ValueError("workspace title is empty")
            value = _checked(ws, "project/update", {"projectId": project_id, "name": title})
            return {"ok": True, "workspace": _view(value["project"], path)}
        if action == "delete":
            _checked(ws, "project/delete", {"projectId": project_id})
            return {"ok": True, "deleted": True}
        if action == "insertBefore":
            before_id = payload.get("beforeWorkspaceId")
            before_path = ((entries.get(before_id) or {}).get("path") or
                           paths_by_id.get(before_id)) if before_id else None
            before_project = _by_path(projects, before_path) if before_path else None
            if before_id and before_project is None:
                raise ValueError(f"unknown workspace: {before_id}")
            _checked(ws, "project/move", {"projectId": project_id,
                                          "beforeProjectId": before_project["id"] if before_project else None})
            ordered = _projects(ws)
            return {"ok": True, "workspaceIds": [
                _workspace_id(root["path"])
                for item in ordered for root in item.get("roots") or []
                if isinstance(root, dict) and root.get("path")
            ]}
        if action == "insertSessionBefore":
            # Codex owns membership, while this API also accepts a manual
            # sibling order. Codex has no corresponding per-project manual
            # thread reorder method. Reject that request before changing the
            # thread's project so a failed reorder cannot partially mutate it.
            if payload.get("beforeSessionId"):
                raise ValueError("Codex does not support manual conversation order")
            thread_id = payload["sessionId"].removeprefix("session-")
            _checked(ws, "thread/metadata/update", {
                "threadId": thread_id, "projectId": project_id})
            view = _view(project, path)
            if payload["sessionId"] not in view["sessionIds"]:
                view["sessionIds"] = [payload["sessionId"], *view["sessionIds"]]
            return {"ok": True, "workspace": view}
        raise ValueError(f"unsupported workspace action: {action}")
    finally:
        ws.close()


if __name__ == "__main__":
    try:
        print(json.dumps(execute(sys.argv[1], json.loads(sys.argv[2])), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
