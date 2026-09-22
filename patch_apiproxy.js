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
// Both import lines are re-checked independently: an earlier run could have
// installed the tailer import while the bridge import still named an older,
// shorter hook list, and guarding on one line alone left the other stale.
const BRIDGE_IMPORT = `import { handleCodexPrompt, handleCodexArchive, handleCodexRename, handleCodexCreate, handleCodexFork, handleCodexCancel, handleCodexModel, handleCodexModelState } from "${BRIDGE}";`;
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
// Codex owns the archived flag, and the DSH registry is updated beside it so
// the sidebar hides the conversation without waiting for a restart. Recording
// it in the registry does not resume the session — it only records the id — so
// this does not add a second writer to the projected log.
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

// --- 4. rename -> Codex -----------------------------------------------
// The Codex branch returns before the DSH title service runs. Renaming through
// DSH resumes the session, which makes the host a second writer of the
// projected session log and collides with the projector's sequence numbers;
// the browser then rejects the whole log as corrupt. The projector reads the
// Codex thread name on its next sweep, so the title still reaches the page.
ensure('handleCodexRename(sessionId, title)', () => code.replace(
  'const accepted = titles.rename(found.agent.session, title);',
  `if (${GUARD}) {
\t\t\t\t\tconst renamed = handleCodexRename(sessionId, title);
\t\t\t\t\tif (!renamed || !renamed.ok) {
\t\t\t\t\t\treturn err(request, {
\t\t\t\t\t\t\tcode: "internal",
\t\t\t\t\t\t\tmessage: "Codex rename failed: " + (renamed && renamed.error),
\t\t\t\t\t\t\tdetails: { sessionId }
\t\t\t\t\t\t});
\t\t\t\t\t}
\t\t\t\t\treturn ok(request, { title: title.trim(), seq: 3 });
\t\t\t\t}
\t\t\t\tconst accepted = titles.rename(found.agent.session, title);`));

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

if (changed) {
  fs.writeFileSync(PKG, code, 'utf-8');
  console.log('saved', PKG);
} else {
  console.log('already patched; nothing to do');
}
