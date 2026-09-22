/**
 * Idempotent patcher for the installed DSH host ApiProxy.
 *
 * The Codex web edition needs three things the stock package does not do:
 *   1. route session.prompt / rename / archive / create to Codex instead of
 *      the DSH-local agent,
 *   2. stream projected Codex events into the browser mux stream, and
 *   3. keep those hooks present after a package upgrade.
 *
 * Run with: node patch_apiproxy.js
 */
import fs from "node:fs";

const PKG = '/home/nahida/.local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-host-apiproxy/lib/index.js';
const BRIDGE = '/home/nahida/agents/sever/dsh/bridge_hook.js';
const TAILER = '/home/nahida/agents/sever/dsh/codex_tailer.js';
const GUARD = 'process.env.DSH_HOME && process.env.DSH_HOME.includes(".dsh-codex")';

let code = fs.readFileSync(PKG, 'utf-8');
let changed = false;

function ensure(needle, apply) {
  if (code.includes(needle)) return;
  const next = apply();
  if (next === code) {
    console.error('  MISS: could not apply hook for', JSON.stringify(needle).slice(0, 60));
    process.exitCode = 1;
    return;
  }
  code = next;
  changed = true;
  console.log('  applied:', JSON.stringify(needle).slice(0, 60));
}

// --- 1. imports ---------------------------------------------------------
const OLD_IMPORT = /^import \{ handleCodexPrompt[^\n]*\n(?:import \{ startCodexTailer \}[^\n]*\n)?/;
const NEW_IMPORT = `import { handleCodexPrompt, handleCodexArchive, handleCodexRename, handleCodexCreate, handleCodexFork, handleCodexCancel } from "${BRIDGE}";\n` +
                   `import { startCodexTailer } from "${TAILER}";\n`;
if (!code.includes('startCodexTailer')) {
  if (OLD_IMPORT.test(code)) code = code.replace(OLD_IMPORT, NEW_IMPORT);
  else code = NEW_IMPORT + code;
  changed = true;
  console.log('  applied: bridge imports');
}

// --- 2. session.prompt -> Codex ----------------------------------------
ensure('handleCodexPrompt(sessionId, mode, content)', () => code.replace(
  'async prompt(request) {\n\t\t\t\tconst { sessionId, mode, content, clientTimeZone } = request.payload;',
  `async prompt(request) {
\t\t\t\tconst { sessionId, mode, content, clientTimeZone } = request.payload;
\t\t\t\tif (${GUARD}) {
\t\t\t\t\tconst bridgeRes = await handleCodexPrompt(sessionId, mode, content);
\t\t\t\t\tif (!bridgeRes.ok) return err(request, {
\t\t\t\t\t\tcode: "internal",
\t\t\t\t\t\tmessage: "Codex send failed: " + bridgeRes.error,
\t\t\t\t\t\tdetails: { sessionId }
\t\t\t\t\t});
\t\t\t\t\treturn ok(request, { accepted: true });
\t\t\t\t}`));

// --- 3. archive -> Codex ----------------------------------------------
ensure('handleCodexArchive(sessionId)', () => code.replace(
  'await ctx.workspaceRegistry.archiveSession(sessionId);',
  `if (${GUARD}) {
\t\t\t\t\thandleCodexArchive(sessionId);
\t\t\t\t}\n\t\t\t\tawait ctx.workspaceRegistry.archiveSession(sessionId);`));

// --- 4. rename -> Codex -----------------------------------------------
ensure('handleCodexRename(sessionId, title)', () => code.replace(
  'const accepted = titles.rename(found.agent.session, title);',
  `if (${GUARD}) {
\t\t\t\t\thandleCodexRename(sessionId, title);
\t\t\t\t}\n\t\t\t\tconst accepted = titles.rename(found.agent.session, title);`));

// --- 5. session.create -> real Codex thread ---------------------------
ensure('handleCodexCreate(', () => code.replace(
  'async create(request) {\n\t\t\t\tconst sessionId = request.payload.sessionId ?? `session-${randomUUID()}`;',
  `async create(request) {
\t\t\t\tif (${GUARD}) {
\t\t\t\t\tconst created = handleCodexCreate(request.payload.cwd);
\t\t\t\t\tif (created && created.ok && created.sessionId) {
\t\t\t\t\t\treturn ok(request, { sessionId: "session-" + created.threadId, agentPreset: "standard" });
\t\t\t\t\t}
\t\t\t\t\treturn err(request, { code: "internal", message: "Codex thread/start failed: " + (created && created.error), details: {} });
\t\t\t\t}
\t\t\t\tconst sessionId = request.payload.sessionId ?? \`session-\${randomUUID()}\`;`));

// --- 6. live tail of projected Codex events into the mux stream -------
// Started once per host process and fanned out through the same broadcast()
// channel DSH uses for its own session events.
ensure('startCodexTailer(', () => {
  const anchor = '\t/** Send one transient frame to every connected mux consumer. */\n\tfunction broadcast(payload) {\n\t\tconst envelope = frame(payload);\n\t\tfor (const queue of muxQueues) queue.push(envelope);\n\t}';
  if (!code.includes(anchor)) {
    console.error('  MISS: broadcast() anchor not found');
    return code;
  }
  return code.replace(anchor, anchor +
    `\n\tif (${GUARD}) startCodexTailer((payload) => broadcast(payload));`);
});

// --- 7. stop button -> Codex turn/interrupt ----------------------------
ensure('handleCodexCancel(sessionId)', () => code.replace(
  'cancel(request) {\n\t\t\t\tconst { sessionId } = request.payload;',
  `cancel(request) {
\t\t\t\tconst { sessionId } = request.payload;
\t\t\t\tif (${GUARD}) {
\t\t\t\t\thandleCodexCancel(sessionId);
\t\t\t\t\treturn Promise.resolve(ok(request, { accepted: true }));
\t\t\t\t}`));

if (changed) {
  fs.writeFileSync(PKG, code, 'utf-8');
  console.log('saved', PKG);
} else {
  console.log('already patched; nothing to do');
}
