import fs from 'fs';

const file = '/home/nahida/.local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-host-apiproxy/lib/index.js';
let code = fs.readFileSync(file, 'utf-8');

// 1. Add import at top
const importStatement = 'import { handleCodexPrompt, handleCodexArchive, handleCodexRename } from "/home/nahida/agents/sever/dsh/bridge_hook.js";\n';
if (!code.includes('handleCodexPrompt')) {
  code = importStatement + code;
}

// 2. Patch prompt
const targetPrompt = 'async prompt(request) {\n\t\t\t\tconst { sessionId, mode, content, clientTimeZone } = request.payload;';
const replacementPrompt = `async prompt(request) {
\t\t\t\tconst { sessionId, mode, content, clientTimeZone } = request.payload;
\t\t\t\tif (process.env.DSH_HOME && process.env.DSH_HOME.includes(".dsh-codex")) {
\t\t\t\t\tconst bridgeRes = await handleCodexPrompt(sessionId, mode, content);
\t\t\t\t\tif (!bridgeRes.ok) {
\t\t\t\t\t\treturn err(request, {
\t\t\t\t\t\t\tcode: "internal",
\t\t\t\t\t\t\tmessage: "Codex queue failed: " + bridgeRes.error,
\t\t\t\t\t\t\tdetails: { sessionId }
\t\t\t\t\t\t});
\t\t\t\t\t}
\t\t\t\t\treturn ok(request, { accepted: true });
\t\t\t\t}`;

if (!code.includes('handleCodexPrompt(sessionId, mode, content)')) {
  if (code.includes(targetPrompt)) {
    code = code.replace(targetPrompt, replacementPrompt);
    console.log('Patched prompt successfully');
  } else {
    console.error('Target prompt not found');
    process.exit(1);
  }
}

// 3. Patch archive
const targetArchive = 'await ctx.workspaceRegistry.archiveSession(sessionId);';
const replacementArchive = `if (process.env.DSH_HOME && process.env.DSH_HOME.includes(".dsh-codex")) {
\t\t\t\t\thandleCodexArchive(sessionId);
\t\t\t\t}
\t\t\t\tawait ctx.workspaceRegistry.archiveSession(sessionId);`;

if (!code.includes('handleCodexArchive(sessionId)')) {
  if (code.includes(targetArchive)) {
    code = code.replace(targetArchive, replacementArchive);
    console.log('Patched archive successfully');
  } else {
    console.error('Target archive not found');
    process.exit(1);
  }
}

// 4. Patch rename
const targetRename = 'const accepted = titles.rename(found.agent.session, title);';
const replacementRename = `if (process.env.DSH_HOME && process.env.DSH_HOME.includes(".dsh-codex")) {
\t\t\t\t\thandleCodexRename(sessionId, title);
\t\t\t\t}
\t\t\t\tconst accepted = titles.rename(found.agent.session, title);`;

if (!code.includes('handleCodexRename(sessionId, title)')) {
  if (code.includes(targetRename)) {
    code = code.replace(targetRename, replacementRename);
    console.log('Patched rename successfully');
  } else {
    console.error('Target rename not found');
    process.exit(1);
  }
}

fs.writeFileSync(file, code, 'utf-8');
console.log('Saved index.js');
