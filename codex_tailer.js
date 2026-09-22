/**
 * Live-tail bridge for the DSH web edition.
 *
 * The web edition renders Codex threads that the sync daemon projects into
 * JSONL files. DSH only emits session/event mux frames for agents it runs
 * itself, so a projected Codex thread would otherwise stay frozen in the
 * browser until the page was reloaded. This tailer watches the projected
 * session files and pushes every newly appended event into the mux stream,
 * which is what makes the web conversation update while Codex is working.
 *
 * Reads are offset-based. A streamed turn appends token deltas every few
 * milliseconds, so re-reading and re-parsing the whole log on each change would
 * cost hundreds of megabytes of parsing per second on a long conversation.
 */
import fs from "node:fs";
import path from "node:path";

const DEFAULT_ROOT = "/home/nahida/agents/sever/dsh/.dsh-codex/sessions";

export function startCodexTailer(pushFrame, signal, options = {}) {
  const root = options.root || process.env.DSH_CODEX_SESSIONS_ROOT || DEFAULT_ROOT;
  const intervalMs = options.intervalMs || 250;
  const cursors = new Map();

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
        let cursor = cursors.get(key);
        if (cursor === undefined) {
          // Prime from the whole file so the first live push carries only new
          // work, then follow from the end.
          const primed = prime(file, stat.size);
          cursors.set(key, primed);
          continue;
        }
        if (stat.size === cursor.offset) continue;
        if (stat.size < cursor.offset) {
          // The projector rewrote this log from scratch; re-prime.
          cursors.set(key, prime(file, stat.size));
          continue;
        }
        const chunk = readRange(file, cursor.offset, stat.size - cursor.offset);
        if (chunk === null) continue;
        const { events, offset } = parseLines(chunk, cursor.offset);
        cursor.offset = offset;
        if (push) {
          for (const event of events) {
            if (typeof event.seq !== "number") continue;
            if (event.seq <= cursor.lastSeq) continue;
            cursor.lastSeq = event.seq;
            pushFrame({ type: "session/event", sessionId: key, event });
          }
        } else {
          for (const event of events) {
            if (typeof event.seq === "number" && event.seq > cursor.lastSeq) cursor.lastSeq = event.seq;
          }
        }
      }
    }
  };

  const prime = (file, size) => {
    const cursor = { offset: 0, lastSeq: -1 };
    if (size === 0) return cursor;
    const chunk = readRange(file, 0, size);
    if (chunk === null) return cursor;
    const { events, offset } = parseLines(chunk, 0);
    cursor.offset = offset;
    for (const event of events) {
      if (typeof event.seq === "number" && event.seq > cursor.lastSeq) cursor.lastSeq = event.seq;
    }
    return cursor;
  };

  const readRange = (file, offset, length) => {
    let fd;
    try {
      fd = fs.openSync(file, "r");
      const buffer = Buffer.allocUnsafe(length);
      const read = fs.readSync(fd, buffer, 0, length, offset);
      return buffer.subarray(0, read).toString("utf-8");
    } catch {
      return null;
    } finally {
      if (fd !== undefined) {
        try {
          fs.closeSync(fd);
        } catch {
          /* the handle is already gone; nothing left to release */
        }
      }
    }
  };

  const parseLines = (text, offset) => {
    const events = [];
    let start = 0;
    let consumed = offset;
    let index = text.indexOf("\n");
    while (index !== -1) {
      const line = text.slice(start, index).trim();
      consumed += index + 1 - start;
      start = index + 1;
      if (line) {
        try {
          events.push(JSON.parse(line));
        } catch {
          // A torn or unreadable line is skipped; the next scan re-reads from
          // the last complete line, so nothing is lost permanently.
        }
      }
      index = text.indexOf("\n", start);
    }
    // The offset stops at the last complete line, so a half-written line is
    // re-read (and completed) by the next scan instead of being lost.
    return { events, offset: consumed };
  };

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
