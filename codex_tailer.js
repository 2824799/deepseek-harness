/**
 * Live-tail bridge for the DSH web edition.
 *
 * The web edition renders Codex threads that the sync daemon projects into
 * JSONL files. DSH only emits `session/event` mux frames for agents it runs
 * itself, so a projected Codex thread would otherwise stay frozen in the
 * browser until the page was reloaded. This tailer watches the projected
 * session files and pushes every newly appended event into the mux stream,
 * which is what makes the web conversation update while Codex is working.
 */
import fs from "node:fs";
import path from "node:path";

const DEFAULT_ROOT = "/home/nahida/agents/sever/dsh/.dsh-codex/sessions";

export function startCodexTailer(pushFrame, signal, options = {}) {
  const root = options.root || process.env.DSH_CODEX_SESSIONS_ROOT || DEFAULT_ROOT;
  const intervalMs = options.intervalMs || 1200;
  const lastSeq = new Map();
  const seenMtime = new Map();

  const scan = (push) => {
    let workspaces;
    try {
      workspaces = fs.readdirSync(root, { withFileTypes: true });
    } catch {
      return;
    }
    for (const workspace of workspaces) {
      if (!workspace.isDirectory()) continue;
      const workspaceDir = path.join(root, workspace.name);
      let sessions;
      try {
        sessions = fs.readdirSync(workspaceDir, { withFileTypes: true });
      } catch {
        continue;
      }
      for (const session of sessions) {
        if (!session.isDirectory() || !session.name.startsWith("session-")) continue;
        const file = path.join(workspaceDir, session.name, "session.jsonl");
        let stat;
        try {
          stat = fs.statSync(file);
        } catch {
          continue;
        }
        const key = session.name;
        if (seenMtime.get(key) === stat.mtimeMs) continue;
        seenMtime.set(key, stat.mtimeMs);

        let text;
        try {
          text = fs.readFileSync(file, "utf-8");
        } catch {
          continue;
        }

        const lines = text.split("\n");
        for (let index = 1; index < lines.length; index++) {
          const line = lines[index].trim();
          if (!line) continue;
          let event;
          try {
            event = JSON.parse(line);
          } catch {
            continue;
          }
          if (typeof event.seq !== "number") continue;
          const previous = lastSeq.get(key);
          if (previous === undefined) {
            lastSeq.set(key, event.seq);
            continue;
          }
          if (event.seq <= previous) continue;
          lastSeq.set(key, event.seq);
          if (push) pushFrame({ type: "session/event", sessionId: key, event });
        }
      }
    }
  };

  // Prime the sequence watermark so the first live push carries only new work.
  scan(false);
  const timer = setInterval(() => {
    try {
      scan(true);
    } catch {
      // A transient read failure must never take down the mux stream.
    }
  }, intervalMs);
  if (timer.unref) timer.unref();

  const stop = () => clearInterval(timer);
  signal?.addEventListener("abort", stop, { once: true });
  return stop;
}
