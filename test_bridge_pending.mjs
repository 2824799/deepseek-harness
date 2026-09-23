import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

test('blank Codex history header comes from pending control state', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-pending-'));
  const oldHome = process.env.DSH_HOME;
  try {
    process.env.DSH_HOME = root;
    fs.writeFileSync(path.join(root, 'pending-threads.json'), JSON.stringify({
      'thread-one': { cwd: '/project', createdAt: 123, workspaceId: 'workspace-one' },
    }));
    const bridge = await import('./bridge_hook.js?pending-header-test');
    assert.deepEqual(bridge.codexPendingMeta('session-thread-one'), {
      id: 'session-thread-one', version: 0, createdAt: 123000,
      cwd: '/project', delegationDepth: 0, agentPreset: 'standard',
    });
    assert.equal(bridge.codexPendingMeta('session-missing'), null);
  } finally {
    if (oldHome === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = oldHome;
    fs.rmSync(root, { recursive: true, force: true });
  }
});
