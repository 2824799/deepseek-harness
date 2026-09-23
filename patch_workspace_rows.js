/** Keep the Codex web edition's provisional session out of its sidebar. */
import fs from 'node:fs'

const client = '/home/nahida/.local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-client-ui-workspace/lib/client.js'
const source = fs.readFileSync(client, 'utf8')
const before = 'return session.origin !== "subagent" && !archived.has(session.id) && (!session.blank || session.id === current);'
const after = 'return session.origin !== "subagent" && !archived.has(session.id) && (!session.blank || (window.location.port !== "3080" && session.id === current));'

if (!source.includes(after)) {
  if (!source.includes(before)) throw new Error('installed Workspace browser changed; update the patch against its new source')
  fs.writeFileSync(client, source.replace(before, after))
}
