import fs from "node:fs";
import path from "node:path";
import cp from "node:child_process";
import os from "node:os";

const KEEPER = "/home/nahida/agents/sever/dsh/codex_keeper.py";

const LIVE_FILE = path.join(process.env.DSH_HOME || "/home/nahida/agents/sever/dsh/.dsh-codex", "live-state.json");
const WORKSPACE_FILE = path.join(process.env.DSH_HOME || "/home/nahida/agents/sever/dsh/.dsh-codex", "storages", "workspace.json");

let liveCache = { mtime: 0, doc: {} };

/** Read the projector's live-state document, cached on mtime. */
export function codexLiveState() {
  let mtime = 0;
  try {
    mtime = fs.statSync(LIVE_FILE).mtimeMs;
  } catch {
    return liveCache.doc;
  }
  if (mtime !== liveCache.mtime) {
    try {
      liveCache = { mtime, doc: JSON.parse(fs.readFileSync(LIVE_FILE, "utf-8")) };
    } catch {
      liveCache = { mtime, doc: liveCache.doc };
    }
  }
  return liveCache.doc;
}

/** Extra session.list columns Codex owns: running flag and durable title. */
export function codexSessionListExtras(sessionId) {
  const doc = codexLiveState();
  const running = Array.isArray(doc.running) && doc.running.includes(sessionId.replace(/^session-/, ""));
  const title = (doc.titles || {})[sessionId];
  return {
    running,
    ...title === undefined ? {} : { title }
  };
}

/** The workspace registry file as the web should see it right now. */
export function codexWorkspaceSnapshot() {
  try {
    const doc = JSON.parse(fs.readFileSync(WORKSPACE_FILE, "utf-8"));
    const tables = doc.tables || {};
    const workspaces = tables.workspaces || {};
    const ids = (doc.global || {}).workspaceIds || Object.keys(workspaces);
    const items = [];
    for (const id of ids) {
      const entry = workspaces[id];
      if (!entry) continue;
      items.push({
        workspaceId: id,
        path: entry.path,
        title: entry.title,
        sessionIds: entry.sessionIds || [],
        createdAt: entry.createdAt,
        updatedAt: entry.updatedAt
      });
    }
    return { items, archivedSessionIds: (doc.global || {}).archivedSessionIds || [] };
  } catch {
    return null;
  }
}

/**
 * Watch the two projector-owned files and report changes. The host mux stream
 * uses this to push sidebar updates without waiting for a page reload.
 */
export function codexWatchLive(onChange) {
  const seen = { live: "", workspace: "" };
  const check = () => {
    for (const [key, file] of [["live", LIVE_FILE], ["workspace", WORKSPACE_FILE]]) {
      let content = "";
      try {
        content = fs.readFileSync(file, "utf-8");
      } catch {
        continue;
      }
      // The projector rewrites both files on every sweep; only a real content
      // change should wake the sidebar, or every poll would fan a full frame
      // set out to every open page.
      if (content === seen[key]) continue;
      seen[key] = content;
      onChange(key);
    }
  };
  check();
  const timer = setInterval(() => {
    try {
      check();
    } catch {
      /* a missed tick costs one poll interval, nothing more */
    }
  }, 700);
  if (timer.unref) timer.unref();
  return () => clearInterval(timer);
}

export async function handleCodexPrompt(sessionId, mode, content) {
  const textParts = content.filter(p => p.type === "text").map(p => p.text);
  const fullText = textParts.join("\n");
  const imgParts = content.filter(p => p.type === "image");
  const savedImgPaths = [];
  if (imgParts.length > 0) {
    const tmpDir = "/tmp/codex_dsh_uploads";
    fs.mkdirSync(tmpDir, { recursive: true });
    for (let i = 0; i < imgParts.length; i++) {
      const img = imgParts[i];
      const ext = img.mediaType ? img.mediaType.split("/")[1] || "png" : "png";
      const imgFile = path.join(tmpDir, `upload_${Date.now()}_${i}.${ext}`);
      fs.writeFileSync(imgFile, Buffer.from(img.data, "base64"));
      savedImgPaths.push(imgFile);
    }
  }
  const bridgePayload = JSON.stringify({
    text: fullText,
    images: savedImgPaths,
    mode: mode || "followup"
  });
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "prompt",
    sessionId,
    bridgePayload
  ], { encoding: "utf-8" });

  let parsed;
  try {
    parsed = JSON.parse((res.stdout || "").trim());
  } catch (e) {
    parsed = { ok: false, error: res.stderr || res.stdout || String(e) };
  }
  return parsed;
}

export function handleCodexArchive(sessionId) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "archive",
    sessionId
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

export function handleCodexRename(sessionId, title) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "rename",
    sessionId,
    title
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

export function handleCodexCreate(workspacePath) {
  // A blank Codex thread is reaped about a minute after its creating
  // connection closes, so creation runs in a detached holder process that
  // keeps that connection open until the first message persists the thread.
  // The holder reports the new id through a temp file; this call only waits
  // for that line, never for the holder itself.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-create-"));
  const out = path.join(dir, "id");
  const fd = fs.openSync(out, "w");
  const child = cp.spawn("python3", [KEEPER, "hold", workspacePath || "None"], {
    detached: true,
    stdio: ["ignore", fd, fd]
  });
  child.unref();
  fs.closeSync(fd);
  for (let i = 0; i < 75; i++) {
    let line = "";
    try {
      line = fs.readFileSync(out, "utf-8").trim();
    } catch {
      line = "";
    }
    if (line) {
      fs.rmSync(dir, { recursive: true, force: true });
      if (line.startsWith("{")) {
        try {
          return JSON.parse(line);
        } catch {
          return { ok: false, error: line };
        }
      }
      return { ok: true, threadId: line, sessionId: line };
    }
    cp.spawnSync("sleep", ["0.2"]);
  }
  fs.rmSync(dir, { recursive: true, force: true });
  return { ok: false, error: "thread creation timed out" };
}

export function handleCodexFork(sessionId) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "fork",
    sessionId
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

export function handleCodexCancel(sessionId) {
  cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "cancel",
    sessionId
  ]);
}

/**
 * Push the web UI's model choice through to Codex for this thread.
 *
 * The web picker is DSH-local state: without this the choice only changed the
 * label in the browser while Codex kept answering with the model from its own
 * config.toml. Codex takes provider-qualified ids verbatim, so the DSH catalog
 * id is forwarded unchanged.
 */
export function handleCodexModel(sessionId, model, reasoningEffort) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "model",
    sessionId,
    model,
    reasoningEffort || ""
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

/**
 * Read the model this thread is on without asking DSH to resume it.
 *
 * session.models resolves the session's agent, and this edition's session store
 * is the projected log, so the stock read appends a second writer's events to
 * it and the browser rejects the log as corrupt. The recorded choice is the
 * same answer and costs nothing.
 */
export function handleCodexModelState(sessionId) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "model-state",
    sessionId
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

/** Switch this thread's sandbox and approval preset through Codex. */
export function handleCodexPermission(sessionId, preset) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "permission",
    sessionId,
    preset
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}
