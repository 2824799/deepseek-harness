/** Adapt the installed, compatible UI Conversation bundle for Codex reasoning rows. */
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const runtime = process.env.DSH_CODEX_RUNTIME_ROOT ?? fileURLToPath(new URL('../.codex-dsh-runtime/', import.meta.url))
const target = join(runtime, 'node_modules/@deepseek-ai/dsh-client-ui-conversation/lib/client.js')
let code = readFileSync(target, 'utf8')
const marker = 'codexReasoningRuns'

function replace(needle, replacement) {
  if (!code.includes(needle)) throw new Error('Codex reasoning patch anchor missing: ' + needle.slice(0, 100))
  code = code.replace(needle, replacement)
}

if (!code.includes(marker)) {
  replace('const ChatNodeSeat = (0, react.memo)(function ChatNodeSeat({ nodeKey, selectedCallId, cwd, openFile, inspectCall, forkAt, renderMessageImages, fileMentions, useSession, renderSlot, t }) {',
    'const ChatNodeSeat = (0, react.memo)(function ChatNodeSeat({ nodeKey, mergedReasoningText, mergedReasoningRunning, selectedCallId, cwd, openFile, inspectCall, forkAt, renderMessageImages, fileMentions, useSession, renderSlot, t }) {')
  replace('...owner,\n\t\t\t\tnode: routedNode\n\t\t\t};',
    '...owner,\n\t\t\t\tnode: routedNode,\n\t\t\t\tmergedReasoningText,\n\t\t\t\tmergedReasoningRunning\n\t\t\t};')
  replace('function AssistantNodeView({ node, useTurnData, openFile, renderMessageImages, fileMentions, t }) {',
    'function AssistantNodeView({ node, mergedReasoningText, mergedReasoningRunning, useTurnData, openFile, renderMessageImages, fileMentions, t }) {')
  replace('blocks: data.blocks,\n\t\t\t\tstreaming: data.status === "running",',
    'blocks: mergedReasoningText === void 0 ? data.blocks : [{ kind: "reasoning", text: mergedReasoningText }],\n\t\t\t\tstreaming: mergedReasoningRunning ?? data.status === "running",')

  // The source bundle contains one old-style reasoning block renderer. Join
  // adjacent blocks while leaving prose, media, and tool boundaries intact.
  replace('case "reasoning":\n\t\t\t\t\t\trendered.push((0, react_jsx_runtime.jsx)(ReasoningRow, {\n\t\t\t\t\t\t\ttext: block.text,\n\t\t\t\t\t\t\trunning: streaming && i === last,\n\t\t\t\t\t\t\tt\n\t\t\t\t\t\t}, i));\n\t\t\t\t\t\tbreak;',
    'case "reasoning": {\n\t\t\t\t\t\tconst start = i;\n\t\t\t\t\t\tconst sections = [block.text];\n\t\t\t\t\t\twhile (blocks[i + 1]?.kind === "reasoning") sections.push(blocks[++i].text);\n\t\t\t\t\t\trendered.push((0, react_jsx_runtime.jsx)(ReasoningRow, {\n\t\t\t\t\t\t\ttext: sections.join("\\n\\n"),\n\t\t\t\t\t\t\trunning: streaming && i === last,\n\t\t\t\t\t\t\tt\n\t\t\t\t\t\t}, start));\n\t\t\t\t\t\tbreak;\n\t\t\t\t\t}')

  const helpers = [
    'function codexReasoningOnlyText(node) {',
    '  if (node?.kind !== "assistant-step" || node.data.status === "interrupted" || node.data.blocks.length === 0) return void 0;',
    '  const sections = [];',
    '  for (const block of node.data.blocks) {',
    '    if (block.kind !== "reasoning") return void 0;',
    '    if (block.text.trim() !== "") sections.push(block.text);',
    '  }',
    '  return sections.length === 0 ? void 0 : sections.join("\\n\\n");',
    '}',
    'function codexReasoningRuns(order, nodes) {',
    '  const rows = [];',
    '  for (let i = 0; i < order.length; i++) {',
    '    const first = nodes.get(order[i]);',
    '    if (codexReasoningOnlyText(first) === void 0) { rows.push({ key: order[i] }); continue; }',
    '    const members = [order[i]];',
    '    while (i + 1 < order.length) {',
    '      const next = nodes.get(order[i + 1]);',
    '      if (codexReasoningOnlyText(next) === void 0 || next.data.turn !== first.data.turn) break;',
    '      members.push(order[++i]);',
    '    }',
    '    rows.push(members.length > 1 ? { key: members[0], members } : { key: members[0] });',
    '  }',
    '  return rows;',
    '}',
    'function CodexReasoningSeat({ members, useSession, ...seatProps }) {',
    '  const text = useSession((s) => {',
    '    const sections = [];',
    '    for (const key of members) {',
    '      const section = codexReasoningOnlyText(s.chat.nodes.get(key));',
    '      if (section === void 0) return void 0;',
    '      sections.push(section);',
    '    }',
    '    return sections.join("\\n\\n");',
    '  });',
    '  const running = useSession((s) => s.chat.nodes.get(members[members.length - 1])?.data.status === "running");',
    '  if (text === void 0) return (0, react_jsx_runtime.jsx)(react.Fragment, { children: members.map((nodeKey) =>',
    '    (0, react_jsx_runtime.jsx)(ChatNodeSeat, { ...seatProps, useSession, nodeKey }, nodeKey)) });',
    '  return (0, react_jsx_runtime.jsx)(ChatNodeSeat, { ...seatProps, useSession, nodeKey: members[0], mergedReasoningText: text, mergedReasoningRunning: running });',
    '}',
  ].join('\n') + '\n\t\t'
  replace('const ChatNodeSeat = (0, react.memo)(function ChatNodeSeat', helpers + 'const ChatNodeSeat = (0, react.memo)(function ChatNodeSeat')

  const oldMap = 'order.map((nodeKey) => (0, react_jsx_runtime.jsx)(ChatNodeSeat, {'
  const newMap = 'codexReasoningRuns(order, nodeStore).map((row) => (0, react_jsx_runtime.jsx)(row.members ? CodexReasoningSeat : ChatNodeSeat, {\n\t\t\t\t\t\t\t\t...row.members === void 0 ? {} : { members: row.members },'
  replace(oldMap, newMap)
  replace('nodeKey,\n\t\t\t\t\t\t\t\tuseSession,\n\t\t\t\t\t\t\t\tselectedCallId,',
    'nodeKey: row.key,\n\t\t\t\t\t\t\t\tuseSession,\n\t\t\t\t\t\t\t\tselectedCallId,')
  replace('renderSlot,\n\t\t\t\t\t\t\t\tt\n\t\t\t\t\t\t\t}, nodeKey)),',
    'renderSlot,\n\t\t\t\t\t\t\t\tt\n\t\t\t\t\t\t\t}, row.key)),')

  writeFileSync(target, code)
  console.log('patched Codex reasoning rows:', target)
} else {
  console.log('Codex reasoning rows already patched:', target)
}
