import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { startCodexTailer } from './codex_tailer.js';

async function until(predicate) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.fail('live tail did not deliver the appended events');
}

test('live tail keeps byte offsets across Chinese text and a split JSONL line', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-tailer-'));
  const sessionId = 'session-tail-test';
  const folder = path.join(root, 'workspace', sessionId);
  fs.mkdirSync(folder, { recursive: true });
  const file = path.join(folder, 'session.jsonl');
  const row = (seq, text) => JSON.stringify({ seq, type: 'assistant/chunk', data: { text } }) + '\n';
  fs.writeFileSync(file, row(0, '已有消息'));
  const received = [];
  const stop = startCodexTailer((frame) => received.push(frame), undefined, { root, intervalMs: 5 });
  try {
    fs.appendFileSync(file, row(1, '新的中文回复'));
    await until(() => received.length === 1);
    const partial = Buffer.from(row(2, '跨两次写入的消息'));
    fs.appendFileSync(file, partial.subarray(0, partial.length - 2));
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.equal(received.length, 1);
    fs.appendFileSync(file, partial.subarray(partial.length - 2));
    await until(() => received.length === 2);
    fs.appendFileSync(file, row(3, 'ASCII follows 中文'));
    await until(() => received.length === 3);
    assert.deepEqual(received.map((frame) => frame.event.seq), [1, 2, 3]);
    assert.deepEqual(received.map((frame) => frame.sessionId), [sessionId, sessionId, sessionId]);
  } finally {
    stop();
    fs.rmSync(root, { recursive: true, force: true });
  }
});
