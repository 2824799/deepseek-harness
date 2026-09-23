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

const pendingImport = 'import { codexPendingMeta } from "' + BRIDGE + '";';
if (!code.includes(pendingImport)) {
  code = pendingImport + '\n' + code;
  changed = true;
}

// --- 1. imports ---------------------------------------------------------
// Both import lines are re-checked independently: an earlier run could have
// installed the tailer import while the bridge import still named an older,
// shorter hook list, and guarding on one line alone left the other stale.
const BRIDGE_IMPORT = `import { handleCodexPrompt, handleCodexArchive, handleCodexRename, handleCodexCreate, handleCodexFork, handleCodexCancel, handleCodexModel, handleCodexModelState, codexModelCatalog, handleCodexWorkspace } from "${BRIDGE}";`;
const LIVE_IMPORT = `import { codexSessionListExtras, codexWorkspaceSnapshot, codexWatchLive } from "${BRIDGE}";`;
const TAILER_IMPORT = `import { startCodexTailer } from "${TAILER}";`;
if (!code.includes(BRIDGE_IMPORT)) {
  const existing = code.match(/^import \{ handleCodex[A-Za-z, ]*\} from "[^"\n]*";$/m);
  if (existing) code = code.replace(existing[0], BRIDGE_IMPORT);
  else code = BRIDGE_IMPORT + '\n' + code;
  changed = true;
  console.log('  applied: bridge imports');
}
if (!code.includes(TAILER_IMPORT)) {
  if (code.includes('import { startCodexTailer }')) {
    code = code.replace(/^import \{ startCodexTailer \}[^\n]*$/m, TAILER_IMPORT);
  } else {
    const bridgeIdx = code.indexOf(BRIDGE_IMPORT);
    const at = code.indexOf('\n', bridgeIdx) + 1;
    code = code.slice(0, at) + TAILER_IMPORT + '\n' + code.slice(at);
  }
  changed = true;
  console.log('  applied: tailer import');
}
if (!code.includes(LIVE_IMPORT)) {
  const tailerIdx = code.indexOf(TAILER_IMPORT);
  const at = code.indexOf('\n', tailerIdx) + 1;
  code = code.slice(0, at) + LIVE_IMPORT + '\n' + code.slice(at);
  changed = true;
  console.log('  applied: live import');
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
// Codex owns the archived flag; the sync daemon updates the sidebar snapshot.
ensure('handleCodexArchive(sessionId)', () => code.replace(
  `			async archiveSession(request) {
				const { sessionId } = request.payload;`,
  `			async archiveSession(request) {
				const { sessionId } = request.payload;
				if (${GUARD}) {
					const archived = handleCodexArchive(sessionId);
					if (!archived || !archived.ok) {
						return err(request, {
							code: "internal",
							message: "Codex archive failed: " + (archived && archived.error),
							details: { sessionId }
						});
					}
					try {
						await ctx.workspaceRegistry.archiveSession(sessionId);
					} catch (error) {
						if (!(error instanceof WorkspaceUnknownSessionError)) throw error;
					}
					return ok(request, { archivedSessionIds: [...ctx.workspaceRegistry.archivedSessionIds] });
				}`));

// Upgrade the older hook, which also wrote to DSH's workspace registry.
ensure('codexArchiveSingleWriter', () => code.replace(
  '\t\t\t\t\ttry {\n\t\t\t\t\t\tawait ctx.workspaceRegistry.archiveSession(sessionId);\n\t\t\t\t\t} catch (error) {\n\t\t\t\t\t\tif (!(error instanceof WorkspaceUnknownSessionError)) throw error;\n\t\t\t\t\t}\n\t\t\t\t\treturn ok(request, { archivedSessionIds: [...ctx.workspaceRegistry.archivedSessionIds] });',
  '\t\t\t\t\tconst archivedIds = codexWorkspaceSnapshot()?.archivedSessionIds ?? []; // codexArchiveSingleWriter\n\t\t\t\t\treturn ok(request, { archivedSessionIds: [...new Set([...archivedIds, sessionId])] });',
));

// --- 4. rename -> Codex -----------------------------------------------
// The original rename hook sat below agentFor(), which resumed the DSH agent
// and appended a session/end-seed into Codex's projected log. Intercept before
// any DSH session resolution so the read model remains single-writer.
ensure('codexRenameBeforeAgent', () => code.replace(
  'async rename(request) {\n\t\t\t\tconst { sessionId, title } = request.payload;',
  [
    'async rename(request) {',
    '\t\t\t\tconst { sessionId, title } = request.payload;',
    '\t\t\t\tif (' + GUARD + ') { // codexRenameBeforeAgent',
    '\t\t\t\t\tconst renamed = handleCodexRename(sessionId, title);',
    '\t\t\t\t\tif (!renamed || !renamed.ok) return err(request, { code: "internal", message: "Codex rename failed: " + (renamed && renamed.error), details: { sessionId } });',
    '\t\t\t\t\treturn ok(request, { title: title.trim(), seq: 3 });',
    '\t\t\t\t}',
  ].join('\n'),
));

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

// Workspace picks carry an id, not a cwd. Resolve against the live snapshot,
// which also contains workspaces added from the browser.
ensure('handleCodexCreate(cwd, workspaceId)', () => code.replace(
  'const created = handleCodexCreate(request.payload.cwd);',
  [
    'const workspaceId = request.payload.workspaceId;',
    'const selected = workspaceId === undefined ? undefined : codexWorkspaceSnapshot()?.items.find(item => item.workspaceId === workspaceId);',
    'if (workspaceId !== undefined && selected === undefined) return err(request, { code: "workspace-not-found", message: "workspace not found", details: { workspaceId } });',
    'const cwd = selected?.path ?? request.payload.cwd;',
    'const created = handleCodexCreate(cwd, workspaceId);',
  ].join('\n\t\t\t\t\t'),
));

// A native DSH fork creates and writes a DSH-owned session. Codex mode must
// create the child through Codex instead. The current bridge supports latest
// state only; reject an anchored historical cut rather than silently forking
// the wrong point in the conversation.
ensure('codexForkBeforeDshWrite', () => code.replace(
  'async fork(request) {\n\t\t\t\tconst { sessionId, atSeq } = request.payload;',
  [
    'async fork(request) {',
    '\t\t\t\tconst { sessionId, atSeq } = request.payload;',
    '\t\t\t\tif (' + GUARD + ') { // codexForkBeforeDshWrite',
    '\t\t\t\t\tif (atSeq !== void 0) return err(request, { code: "fork-unavailable", message: "Forking from a historical event is not supported by the Codex bridge", details: { sessionId } });',
    '\t\t\t\t\tconst forked = handleCodexFork(sessionId);',
    '\t\t\t\t\tif (!forked || !forked.ok || !forked.threadId) return err(request, { code: "internal", message: "Codex fork failed: " + (forked && forked.error), details: { sessionId } });',
    '\t\t\t\t\treturn ok(request, { sessionId: "session-" + forked.threadId });',
    '\t\t\t\t}',
  ].join('\n'),
));

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
\t\t\t\t\tconst stopped = handleCodexCancel(sessionId);
\t\t\t\t\tif (!stopped || !stopped.ok) return Promise.resolve(err(request, {
\t\t\t\t\t\tcode: "internal", message: "Codex stop failed: " + (stopped && stopped.error), details: { sessionId }
\t\t\t\t\t}));
\t\t\t\t\treturn Promise.resolve(ok(request, { accepted: stopped.interrupted === true }));
\t\t\t\t}`));

// Upgrade the older stop hook in an already patched installation.
ensure('const stopped = handleCodexCancel(sessionId)', () => code.replace(
  'handleCodexCancel(sessionId);\n\t\t\t\t\treturn Promise.resolve(ok(request, { accepted: true }));',
  'const stopped = handleCodexCancel(sessionId);\n\t\t\t\t\tif (!stopped || !stopped.ok) return Promise.resolve(err(request, { code: "internal", message: "Codex stop failed: " + (stopped && stopped.error), details: { sessionId } }));\n\t\t\t\t\treturn Promise.resolve(ok(request, { accepted: stopped.interrupted === true }));'));

// --- 8. model picker -> Codex turn/start model override -----------------
ensure('handleCodexModel(sessionId, model, reasoningEffort)', () => code.replace(
  'async selectModel(request) {\n\t\t\t\tconst { sessionId, provider, model, reasoningEffort } = request.payload;',
  `async selectModel(request) {
\t\t\t\tconst { sessionId, provider, model, reasoningEffort } = request.payload;
\t\t\t\tif (${GUARD}) {
\t\t\t\t\tconst pushed = handleCodexModel(sessionId, model, reasoningEffort);
\t\t\t\t\tif (!pushed || !pushed.ok) {
\t\t\t\t\t\treturn err(request, {
\t\t\t\t\t\t\tcode: "internal",
\t\t\t\t\t\t\tmessage: "Codex model switch failed: " + (pushed && pushed.error),
\t\t\t\t\t\t\tdetails: { sessionId }
\t\t\t\t\t\t});
\t\t\t\t\t}
\t\t\t\t\treturn ok(request, { selected: { provider, model, ...reasoningEffort === undefined ? {} : { reasoningEffort } } });
\t\t\t\t}`));

// --- 9. model catalog read -> Codex, without resuming the session -------
// DSH answers session.models by resolving the session's agent, and this
// edition stores sessions in the projected log, so that read would make the
// host a second writer and corrupt the log's sequence numbers. The catalog is
// still DSH's, only the "current selection" line comes from Codex.
ensure('handleCodexModelState(sessionId)', () => code.replace(
  `			async models(request) {
				const { sessionId } = request.payload;
				const found = await agentFor(sessionId);
				if ("error" in found) return err(request, found.error);
				const current = selectionFor(found.agent).current;`,
  `			async models(request) {
				const { sessionId } = request.payload;
				if (${GUARD}) {
					const state = handleCodexModelState(sessionId);
					if (!state || !state.ok) {
						return err(request, {
							code: "internal",
							message: "Codex model read failed: " + (state && state.error),
							details: { sessionId }
						});
					}
					const catalog = await buildModelCatalog(ctx);
					return ok(request, {
						current: state.current,
						routable: true,
						groups: catalog.groups,
						failures: catalog.failures
					});
				}
				const found = await agentFor(sessionId);
				if ("error" in found) return err(request, found.error);
				const current = selectionFor(found.agent).current;`));

// The upstream DSH catalog omits effort menus for several OpenCodex models.
// Overlay Codex model/list so the picker exposes the actual levels and default.
ensure('codexModelCatalog(catalog.groups)', () => code.replace(
  'groups: catalog.groups,',
  'groups: codexModelCatalog(catalog.groups),'));

// --- 10. /permission -> Codex sandbox + approval ------------------------
// The permission picker is a slash command, and the stock handler resolves the
// session's agent before running it — which resumes the session and appends a
// second writer's events to the projected log. Codex owns the sandbox and
// approval pair, so the command is answered at the gateway instead.
const GATEWAY = '/home/nahida/.local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-api-gateway/lib/index.js';
{
  let gcode = fs.readFileSync(GATEWAY, 'utf-8');
  if (!gcode.includes('handleCodexPermission')) {
    const importAnchor = /^import {[^\n]*\} from "@deepseek-ai\/cordis";$/m;
    const dispatchAnchor = `	async dispatchRpc(endpoint, payload, signal) {
		return this.invokeRpc(endpoint, payload, signal);
	}`;
    if (importAnchor.test(gcode) && gcode.includes(dispatchAnchor)) {
      gcode = gcode.replace(importAnchor,
        (match) => match + `\nimport { handleCodexPermission } from "${BRIDGE}";`);
      gcode = gcode.replace(dispatchAnchor, `	async dispatchRpc(endpoint, payload, signal) {
		if (${GUARD} && endpoint === "commands/execute") {
			const line = payload && payload.args && payload.args.line;
			if (typeof line === "string" && line.startsWith("/permission")) {
				const parts = line.trim().split(/\\s+/);
				const switched = handleCodexPermission(payload.args.agentId, parts[1] || "");
				if (!switched || !switched.ok) return {
					ok: false,
					error: {
						code: "internal",
						message: "Codex permission switch failed: " + (switched && switched.error),
						details: {}
					}
				};
				return { ok: true, value: { commandId: "codex-permission", result: { kind: "success" } } };
			}
		}
		return this.invokeRpc(endpoint, payload, signal);
	}`);
      fs.writeFileSync(GATEWAY, gcode, 'utf-8');
      console.log('  applied: gateway /permission hook');
    } else {
      console.error('  MISS: gateway anchors not found');
      process.exitCode = 1;
    }
  }
}

// --- 11. session.list rows carry Codex's running flag and title ---------
// The stock summary answers running:false for every cold session because no
// DSH agent is attached, and it carries no title at all, so the sidebar fell
// back to the directory basename. Both facts live in the projector's
// live-state.json; merging them here keeps the listing honest on every poll.
ensure('codexSessionListExtras(item.sessionId)', () => code.replace(
  'items.sort((a, b) => b.updatedAt - a.updatedAt);\n\t\treturn items;',
  `items.sort((a, b) => b.updatedAt - a.updatedAt);
\t\tif (${GUARD}) for (const item of items) Object.assign(item, codexSessionListExtras(item.sessionId));
\t\treturn items;`));

// --- 12. Codex history must bypass DSH's attached session snapshot -------
// DSH can attach a projected session while it is still growing. Its attached
// events then freeze at that sequence, and even persistence.inspect() returns
// the same in-memory view. readFrom() reads the current physical log instead.
ensure('codexReadStoredHistory', () => code.replace(
  'async function historySourceFor(sessionId) {',
  [
    'async function historySourceFor(sessionId) {',
    '\t\tif (' + GUARD + ') { // codexReadStoredHistory',
    '\t\t\tconst persistence = ctx.get("sessionPersistence");',
    '\t\t\tif (persistence === void 0) throw new Error("Codex history persistence unavailable");',
    '\t\t\tconst stored = await persistence.readFrom(sessionId, 0);',
    '\t\t\treturn { kind: "detached", header: stored.meta, events: stored.events };',
    '\t\t}',
  ].join('\n'),
));

// A new Codex thread can be opened before the next projector sweep. Its
// temporary header is served from pending control state, not written by the
// request path to the projected session log.
ensure('codexPendingMeta(sessionId)', () => code.replace(
  '\t\t\tconst stored = await persistence.readFrom(sessionId, 0);\n\t\t\treturn { kind: "detached", header: stored.meta, events: stored.events };',
  '\t\t\ttry {\n\t\t\t\tconst stored = await persistence.readFrom(sessionId, 0);\n\t\t\t\treturn { kind: "detached", header: stored.meta, events: stored.events };\n\t\t\t} catch (error) {\n\t\t\t\tif (String(error).includes("not found")) {\n\t\t\t\t\tconst pending = codexPendingMeta(sessionId);\n\t\t\t\t\tif (pending) return { kind: "detached", header: pending, events: [] };\n\t\t\t\t}\n\t\t\t\tthrow error;\n\t\t\t}',
));

// Workspace mutations go to Codex. The installed DSH registry is a read model
// and must never be a second writer to workspace.json.
ensure('codexWorkspaceCreate', () => code.replace(
  '\t\t\tasync create(request) {\n\t\t\t\tconst { path } = request.payload;\n\t\t\t\ttry {',
  '\t\t\tasync create(request) {\n\t\t\t\tconst { path } = request.payload;\n\t\t\t\tif (' + GUARD + ') { // codexWorkspaceCreate\n\t\t\t\t\tconst result = handleCodexWorkspace("create", { path });\n\t\t\t\t\treturn result.ok ? ok(request, { workspace: result.workspace, created: result.created }) : err(request, { code: "workspace-invalid-path", message: String(result.error), details: { path } });\n\t\t\t\t}\n\t\t\t\ttry {'
));
ensure('codexWorkspaceRename', () => code.replace(
  '\t\t\tasync rename(request) {\n\t\t\t\tconst { payload } = request;\n\t\t\t\tconst workspace = ctx.workspaceRegistry.get(',
  '\t\t\tasync rename(request) {\n\t\t\t\tconst { payload } = request;\n\t\t\t\tif (' + GUARD + ') { // codexWorkspaceRename\n\t\t\t\t\tconst result = handleCodexWorkspace("rename", payload);\n\t\t\t\t\treturn result.ok ? ok(request, { workspace: result.workspace }) : err(request, { code: "internal", message: String(result.error), details: { workspaceId: payload.workspaceId } });\n\t\t\t\t}\n\t\t\t\tconst workspace = ctx.workspaceRegistry.get('
));
ensure('codexWorkspaceDelete', () => code.replace(
  '\t\t\tasync delete(request) {\n\t\t\t\tconst { workspaceId } = request.payload;\n\t\t\t\tconst operation = workspaceCreationChain',
  '\t\t\tasync delete(request) {\n\t\t\t\tconst { workspaceId } = request.payload;\n\t\t\t\tif (' + GUARD + ') { // codexWorkspaceDelete\n\t\t\t\t\tconst result = handleCodexWorkspace("delete", { workspaceId });\n\t\t\t\t\treturn result.ok ? ok(request, { deleted: true }) : err(request, { code: "internal", message: String(result.error), details: { workspaceId } });\n\t\t\t\t}\n\t\t\t\tconst operation = workspaceCreationChain'
));
ensure('codexWorkspaceMove', () => code.replace(
  '\t\t\tasync insertBefore(request) {\n\t\t\t\tconst { workspaceId, beforeWorkspaceId } = request.payload;\n\t\t\t\ttry {',
  '\t\t\tasync insertBefore(request) {\n\t\t\t\tconst { workspaceId, beforeWorkspaceId } = request.payload;\n\t\t\t\tif (' + GUARD + ') { // codexWorkspaceMove\n\t\t\t\t\tconst result = handleCodexWorkspace("insertBefore", { workspaceId, beforeWorkspaceId });\n\t\t\t\t\treturn result.ok ? ok(request, { workspaceIds: result.workspaceIds }) : err(request, { code: "internal", message: String(result.error), details: { workspaceId } });\n\t\t\t\t}\n\t\t\t\ttry {'
));
ensure('codexWorkspaceSessionMove', () => code.replace(
  '\t\t\tasync insertSessionBefore(request) {\n\t\t\t\tconst { payload } = request;\n\t\t\t\tconst workspace = ctx.workspaceRegistry.get(',
  '\t\t\tasync insertSessionBefore(request) {\n\t\t\t\tconst { payload } = request;\n\t\t\t\tif (' + GUARD + ') { // codexWorkspaceSessionMove\n\t\t\t\t\tconst result = handleCodexWorkspace("insertSessionBefore", payload);\n\t\t\t\t\treturn result.ok ? ok(request, { workspace: result.workspace }) : err(request, { code: "workspace-move-invalid", message: String(result.error), details: { workspaceId: payload.workspaceId, sessionId: payload.sessionId } });\n\t\t\t\t}\n\t\t\t\tconst workspace = ctx.workspaceRegistry.get('
));

// --- 13. workspace.list reads the projector's file ----------------------
// The registry caches workspace.json once at boot, so a project Codex deleted
// kept listing until the host restarted. The projector rewrites the file on
// every sweep; serving it straight from disk makes the sidebar follow Codex.
ensure('codexWorkspaceSnapshot()', () => code.replace(
  `\t\tworkspace: {
\t\t\tlist(request) {
\t\t\t\treturn Promise.resolve(ok(request, {
\t\t\t\t\titems: ctx.workspaceRegistry.list().map(workspaceView),
\t\t\t\t\tarchivedSessionIds: [...ctx.workspaceRegistry.archivedSessionIds]
\t\t\t\t}));
\t\t\t},`,
  `\t\tworkspace: {
\t\t\tlist(request) {
\t\t\t\tif (${GUARD}) {
\t\t\t\t\tconst snapshot = codexWorkspaceSnapshot();
\t\t\t\t\tif (snapshot) return Promise.resolve(ok(request, snapshot));
\t\t\t\t}
\t\t\t\treturn Promise.resolve(ok(request, {
\t\t\t\t\titems: ctx.workspaceRegistry.list().map(workspaceView),
\t\t\t\t\tarchivedSessionIds: [...ctx.workspaceRegistry.archivedSessionIds]
\t\t\t\t}));
\t\t\t},`));

// --- 13. host stream pushes projector rewrites to open pages ------------
// The host stream replays its committed registry state on connect and then
// only reacts to in-process events, so a projector rewrite never reached an
// open page. The watcher re-pushes the frame shapes the client already
// handles whenever the projector's files change on disk.
ensure('codexWatchLive(() => {', () => code.replace(
  `\t\t\t\treturn queue.iterate(signal, () => {
\t\t\t\t\tfor (const dispose of disposers) dispose();
\t\t\t\t});`,
  `\t\t\t\tif (${GUARD}) disposers.push(codexWatchLive(() => {
\t\t\t\t\tconst snapshot = codexWorkspaceSnapshot();
\t\t\t\t\tif (!snapshot) return;
\t\t\t\t\tfor (const workspace of snapshot.items) queue.push(frame({
\t\t\t\t\t\ttype: "host/workspace-changed",
\t\t\t\t\t\tworkspace
\t\t\t\t\t}));
\t\t\t\t\tqueue.push(frame({
\t\t\t\t\t\ttype: "host/archived-sessions-changed",
\t\t\t\t\t\tarchivedSessionIds: [...snapshot.archivedSessionIds]
\t\t\t\t\t}));
\t\t\t\t}));
\t\t\t\treturn queue.iterate(signal, () => {
\t\t\t\t\tfor (const dispose of disposers) dispose();
\t\t\t\t});`));

// Publish project removals as well as updates to already open pages.
ensure('const knownCodexWorkspaceIds = new Set(', () => code.replace(
  '\t\t\t\tif (' + GUARD + ') disposers.push(codexWatchLive(() => {',
  '\t\t\t\tconst knownCodexWorkspaceIds = new Set();\n\t\t\t\tif (' + GUARD + ') disposers.push(codexWatchLive(() => {'));
ensure('const currentCodexWorkspaceIds = new Set(', () => code.replace(
  '\t\t\t\t\tif (!snapshot) return;\n\t\t\t\t\tfor (const workspace of snapshot.items) queue.push(frame({',
  '\t\t\t\t\tif (!snapshot) return;\n\t\t\t\t\tconst currentCodexWorkspaceIds = new Set(snapshot.items.map((item) => item.workspaceId));\n\t\t\t\t\tfor (const workspaceId of knownCodexWorkspaceIds) if (!currentCodexWorkspaceIds.has(workspaceId)) queue.push(frame({ type: "host/workspace-removed", workspaceId }));\n\t\t\t\t\tknownCodexWorkspaceIds.clear();\n\t\t\t\t\tfor (const workspaceId of currentCodexWorkspaceIds) knownCodexWorkspaceIds.add(workspaceId);\n\t\t\t\t\tfor (const workspace of snapshot.items) queue.push(frame({'));

// The first watcher frame must remove boot-time ghosts and publish Codex order.
ensure('codexWorkspaceInitialIds', () => code.replace(
  'const knownCodexWorkspaceIds = new Set();',
  'const knownCodexWorkspaceIds = new Set(committedWorkspaces.map((workspace) => String(workspace.id))); // codexWorkspaceInitialIds'
));
ensure('codexWorkspaceOrderFrame', () => code.replace(
  /(for \(const workspace of snapshot\.items\) queue\.push\(frame\(\{[\s\S]*?workspace[\s\S]*?\}\)\);)/,
  '$1' + String.fromCharCode(10) + String.fromCharCode(9).repeat(5) +
  'queue.push(frame({ type: "host/workspace-order-changed", workspaceIds: snapshot.items.map((item) => item.workspaceId) })); // codexWorkspaceOrderFrame'
));

// --- 14. client accepts the extra session.list columns ------------------
{
  const CC = '/home/nahida/.local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-client-connection/lib/client.js';
  let ccode = fs.readFileSync(CC, 'utf-8');
  const NEEDLE = 'agentPreset: string().optional(),\n\t\t\tprojections: lazy(() => sessionProjectionsBlockSchema).optional()';
  if (!ccode.includes('title: string().optional(),')) {
    if (ccode.includes(NEEDLE)) {
      ccode = ccode.replace(NEEDLE, 'agentPreset: string().optional(),\n\t\t\ttitle: string().optional(),\n\t\t\tprojections: lazy(() => sessionProjectionsBlockSchema).optional()');
      fs.writeFileSync(CC, ccode, 'utf-8');
      console.log('  applied: client session summary title column');
    } else {
      console.error('  MISS: client session summary anchor');
      process.exitCode = 1;
    }
  }
}

// --- 15. client keeps the session list fresh while the page is open -----
{
  const CR = '/home/nahida/.local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-client-runtime/lib/client.js';
  let rcode = fs.readFileSync(CR, 'utf-8');
  if (!rcode.includes('codexListTimer')) {
    const anchor = '\t\t\trefreshList() {\n\t\t\t\tif (this.listInflight !== null) return this.listInflight;\n\t\t\t\tthis.listState = "loading";';
    if (rcode.includes(anchor)) {
      rcode = rcode.replace(anchor, `\t\t\trefreshList(silent = false) {
\t\t\t\tif (this.codexListTimer === void 0) this.codexListTimer = setInterval(() => {
\t\t\t\t\tif (this.listInflight === null) this.refreshList(true);
\t\t\t\t}, 2000);
\t\t\t\tif (this.listInflight !== null) return this.listInflight;
\t\t\t\tif (!silent) this.listState = "loading";`);
      fs.writeFileSync(CR, rcode, 'utf-8');
      console.log('  applied: client silent list poll');
    } else {
      console.error('  MISS: client refreshList anchor');
      process.exitCode = 1;
    }
  }
}

// The installed runtime is shared with the stock UI; its periodic Codex
// refresh belongs only to the independent 3080 page. This also upgrades an
// earlier patch that installed the timer without a port guard.
{
  const CR = '/home/nahida/.local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-client-runtime/lib/client.js';
  let rcode = fs.readFileSync(CR, 'utf-8');
  const unscoped = 'if (this.codexListTimer === void 0) this.codexListTimer = setInterval(() => {';
  if (rcode.includes(unscoped)) {
    rcode = rcode.replace(unscoped, 'if (this.codexListTimer === void 0 && globalThis.location?.port === "3080") this.codexListTimer = setInterval(() => {');
    fs.writeFileSync(CR, rcode, 'utf-8');
    console.log('  applied: scope list poll to Codex web edition');
  }
}

// A Codex project may be removed and then re-added with its stable id. Clear
// the client tombstone when the projector sends a fresh workspace frame.
{
  const CR = '/home/nahida/.local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-client-runtime/lib/client.js';
  let rcode = fs.readFileSync(CR, 'utf-8');
  const anchor = 'if (envelope.payload.type === "host/workspace-changed") this.upsert(envelope.payload.workspace);';
  if (!rcode.includes('codexReaddedWorkspace')) {
    if (rcode.includes(anchor)) {
      rcode = rcode.replace(anchor, 'if (envelope.payload.type === "host/workspace-changed") {\n\t\t\t\t\tif (globalThis.location?.port === "3080") { this.removedIds.delete(envelope.payload.workspace.workspaceId); /* codexReaddedWorkspace */ }\n\t\t\t\t\tthis.upsert(envelope.payload.workspace);\n\t\t\t\t}');
      fs.writeFileSync(CR, rcode, 'utf-8');
      console.log('  applied: client project re-add');
    } else {
      console.error('  MISS: client project re-add anchor');
      process.exitCode = 1;
    }
  }
}

if (changed) {
  fs.writeFileSync(PKG, code, 'utf-8');
  console.log('saved', PKG);
} else {
  console.log('already patched; nothing to do');
}
