import type { ChatNode } from '../contract/chat-nodes.ts'
import type { ChatNodeStore } from '../contract/snapshot.ts'

/**
 * Text of a reasoning-only Assistant step; other material separates groups.
 * @param node - The current chat node.
 * @returns Its reasoning text, or undefined when the node breaks a group.
 */
export function reasoningOnlyText(node: ChatNode | undefined): string | undefined {
  if (node?.kind !== 'assistant-step' || node.data.status === 'interrupted'
    || node.data.blocks.length === 0) return undefined
  const sections: string[] = []
  for (const block of node.data.blocks) {
    if (block.kind !== 'reasoning') return undefined
    if (block.text.trim() !== '') sections.push(block.text)
  }
  return sections.length === 0 ? undefined : sections.join('\n\n')
}

/**
 * Consecutive reasoning-only steps in one Turn, keyed by their first row.
 * @param order - Display order of the chat nodes.
 * @param nodes - Store containing the current nodes.
 * @returns Group leads and their hidden continuation rows.
 */
export function reasoningGroups(order: readonly string[], nodes: ChatNodeStore): {
  readonly leads: ReadonlyMap<string, readonly string[]>
  readonly continuations: ReadonlySet<string>
} {
  const leads = new Map<string, readonly string[]>()
  const continuations = new Set<string>()
  for (let index = 0; index < order.length; index++) {
    const firstKey = order[index]
    if (firstKey === undefined) continue
    const first = nodes.get(firstKey) as ChatNode | undefined
    if (reasoningOnlyText(first) === undefined || first?.kind !== 'assistant-step') continue
    const group = [first.key]
    while (index + 1 < order.length) {
      const nextKey = order[index + 1]
      if (nextKey === undefined) break
      const next = nodes.get(nextKey) as ChatNode | undefined
      if (reasoningOnlyText(next) === undefined || next?.kind !== 'assistant-step'
        || next.data.turn !== first.data.turn) break
      group.push(next.key)
      index++
    }
    if (group.length < 2) continue
    leads.set(first.key, group)
    for (const key of group.slice(1)) continuations.add(key)
  }
  return { leads, continuations }
}
