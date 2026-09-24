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

test('startup reads a bounded tail and a new session pushes its first events', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-tail-start-'));
  const folder = path.join(root, 'workspace', 'session-existing');
  fs.mkdirSync(folder, { recursive: true });
  const file = path.join(folder, 'session.jsonl');
  const row = (seq) => JSON.stringify({ seq, type: 'assistant/chunk', data: { text: '中文' } }) + '\n';
  fs.writeFileSync(file, Array.from({ length: 20000 }, (_, seq) => row(seq)).join(''));
  const received = [];
  const original = fs.readSync;
  const lengths = [];
  let stop;
  try {
    fs.readSync = (...args) => { lengths.push(args[3]); return original(...args); };
    stop = startCodexTailer(frame => received.push(frame), undefined, { root, intervalMs: 5 });
  } finally {
    fs.readSync = original;
  }
  try {
    assert.ok(lengths.length > 0 && lengths.every(length => length <= 64 * 1024));
    fs.appendFileSync(file, row(20000));
    await until(() => received.length === 1);
    assert.equal(received[0].event.seq, 20000);
    const next = path.join(root, 'workspace', 'session-new');
    fs.mkdirSync(next);
    fs.writeFileSync(path.join(next, 'session.jsonl'), row(0) + row(1));
    await until(() => received.length === 3);
    assert.deepEqual(received.slice(1).map(frame => [frame.sessionId, frame.event.seq]), [
      ['session-new', 0], ['session-new', 1],
    ]);
  } finally {
    stop?.();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('watch notifications push before the fallback poll and stop on abort', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-tail-watch-'));
  const folder = path.join(root, 'workspace', 'session-watch');
  fs.mkdirSync(folder, { recursive: true });
  const file = path.join(folder, 'session.jsonl');
  fs.writeFileSync(file, '');
  const controller = new AbortController();
  const received = [];
  const stop = startCodexTailer(frame => received.push(frame), controller.signal,
    { root, intervalMs: 60000, flushMs: 1 });
  try {
    fs.appendFileSync(file, JSON.stringify({ seq: 0, type: 'assistant/chunk' }) + '\n');
    await until(() => received.length === 1);
    controller.abort();
    fs.appendFileSync(file, JSON.stringify({ seq: 1, type: 'assistant/chunk' }) + '\n');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(received.length, 1);
  } finally {
    stop();
    fs.rmSync(root, { recursive: true, force: true });
  }
});
