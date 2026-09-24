/** Watch projector-owned JSONL appends and forward them to the stock DSH mux. */
import fs from "node:fs";
import path from "node:path";

const DEFAULT_ROOT = "/home/nahida/agents/sever/dsh/.dsh-codex/sessions";
const PRIME_BYTES = 64 * 1024;

/**
 * Follow complete records by byte offset. Filesystem notifications provide
 * low-latency delivery; a slow rescan covers lost notifications and new roots.
 * @param pushFrame Receives ordinary DSH session/event frames.
 * @param signal Optional owner lifetime.
 * @param options Root and timing overrides for isolated tests/deployments.
 * @returns Idempotent disposer for the watcher and both timers.
 */
export function startCodexTailer(pushFrame, signal, options = {}) {
  if (signal?.aborted) return () => {};
  const root = options.root || process.env.DSH_CODEX_SESSIONS_ROOT || DEFAULT_ROOT;
  const cursors = new Map();
  const dirty = new Set();
  let stopped = false;
  let flushTimer;
  let watcher;

  const readRange = (file, offset, length) => {
    let fd;
    try {
      fd = fs.openSync(file, "r");
      const buffer = Buffer.allocUnsafe(length);
      return buffer.subarray(0, fs.readSync(fd, buffer, 0, length, offset));
    } catch {
      return null;
    } finally {
      if (fd !== undefined) fs.closeSync(fd);
    }
  };

  const parseLines = (bytes, offset) => {
    const events = [];
    let start = 0;
    let end;
    while ((end = bytes.indexOf(10, start)) !== -1) {
      const line = bytes.subarray(start, end).toString("utf-8").trim();
      start = end + 1;
      if (!line) continue;
      try { events.push(JSON.parse(line)); }
      catch { /* Ignore malformed complete records, retain incomplete tails. */ }
    }
    return { events, offset: offset + start };
  };

  const prime = (file, stat) => {
    const cursor = { offset: 0, lastSeq: -1, ino: stat.ino, dev: stat.dev, mtime: stat.mtimeMs };
    // Only the final sequence is needed. Opening a browser must not parse
    // hundreds of megabytes of already-loaded history a second time.
    let start = Math.max(0, stat.size - PRIME_BYTES);
    let bytes = readRange(file, start, stat.size - start);
    if (bytes === null) return cursor;
    if (start > 0) {
      const boundary = bytes.indexOf(10);
      if (boundary < 0) return cursor;
      start += boundary + 1;
      bytes = bytes.subarray(boundary + 1);
    }
    const parsed = parseLines(bytes, start);
    cursor.offset = parsed.offset;
    for (const event of parsed.events) {
      if (typeof event.seq === "number") cursor.lastSeq = Math.max(cursor.lastSeq, event.seq);
    }
    return cursor;
  };

  const visit = (file, push) => {
    const key = path.basename(path.dirname(file));
    if (!key.startsWith("session-")) return;
    let stat;
    try { stat = fs.statSync(file); }
    catch { cursors.delete(file); return; }
    let cursor = cursors.get(file);
    if ((!cursor && !push) || (cursor && (
      stat.ino !== cursor.ino || stat.dev !== cursor.dev || stat.size < cursor.offset
      || (stat.size === cursor.offset && stat.mtimeMs !== cursor.mtime)))) {
      cursors.set(file, prime(file, stat));
      return;
    }
    if (!cursor) {
      // A newly projected session needs its opening events, unlike the initial
      // server scan, whose existing history is already delivered by history().
      cursor = { offset: 0, lastSeq: -1, ino: stat.ino, dev: stat.dev, mtime: stat.mtimeMs };
      cursors.set(file, cursor);
    }
    if (stat.size === cursor.offset) return;
    const bytes = readRange(file, cursor.offset, stat.size - cursor.offset);
    if (bytes === null) return;
    const { events, offset } = parseLines(bytes, cursor.offset);
    cursor.offset = offset;
    cursor.mtime = stat.mtimeMs;
    for (const event of events) {
      if (typeof event.seq !== "number" || event.seq <= cursor.lastSeq) continue;
      cursor.lastSeq = event.seq;
      if (push && !stopped) pushFrame({ type: "session/event", sessionId: key, event });
    }
  };

  const scan = (push) => {
    let workspaces;
    try { workspaces = fs.readdirSync(root, { withFileTypes: true }); }
    catch { return; }
    const found = new Set();
    for (const workspace of workspaces) {
      if (!workspace.isDirectory()) continue;
      const directory = path.join(root, workspace.name);
      let sessions;
      try { sessions = fs.readdirSync(directory, { withFileTypes: true }); }
      catch { continue; }
      for (const session of sessions) {
        if (!session.isDirectory() || !session.name.startsWith("session-")) continue;
        const file = path.join(directory, session.name, "session.jsonl");
        found.add(file);
        visit(file, push);
      }
    }
    for (const file of cursors.keys()) if (!found.has(file)) cursors.delete(file);
  };

  const schedule = (relative) => {
    if (stopped) return;
    if (relative && path.basename(relative.toString()) === "session.jsonl") {
      dirty.add(path.join(root, relative.toString()));
    }
    if (!dirty.size || flushTimer) return;
    flushTimer = setTimeout(() => {
      flushTimer = undefined;
      const files = [...dirty];
      dirty.clear();
      for (const file of files) {
        try { visit(file, true); }
        catch { /* A transient read failure is retried by the fallback scan. */ }
      }
    }, options.flushMs ?? 25);
    flushTimer.unref?.();
  };

  scan(false);
  try {
    watcher = fs.watch(root, { recursive: true, persistent: false }, (_event, relative) => schedule(relative));
    watcher.on("error", () => { watcher?.close(); watcher = undefined; });
  } catch { /* Missing root or unsupported watcher: the rescan still follows it. */ }
  const timer = setInterval(() => {
    try { scan(true); }
    catch { /* A transient filesystem error cannot take down the mux. */ }
  }, options.intervalMs ?? 1000);
  timer.unref?.();

  const stop = () => {
    if (stopped) return;
    stopped = true;
    watcher?.close();
    clearInterval(timer);
    clearTimeout(flushTimer);
    dirty.clear();
    cursors.clear();
    signal?.removeEventListener("abort", stop);
  };
  signal?.addEventListener("abort", stop, { once: true });
  return stop;
}
