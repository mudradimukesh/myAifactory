import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import type { State } from '../src/contracts.ts';
import { Store, json, sha } from '../src/store.ts';

const timestamp = '2026-09-23T00:00:00.000Z';
function initial(): State {
  const model = { provider: 'codex' as const, model: 'fixture', effort: 'low' as const };
  const check = { id: 'unit', argv: ['node', '--test'], cwd: '.', timeoutMs: 1000, requirements: ['behavior'], outputPaths: [] };
  const event = { sequence: 1, at: timestamp, type: 'created', detail: {} };
  return {
    schemaVersion: 1, id: 'fixture', revision: 1, status: 'draft',
    project: {
      schemaVersion: 2, name: 'fixture', repository: '/fixture', base: 'a'.repeat(40),
      recipient: 'test operator', brief: 'fixture only', policies: ['fixture policy'], requirements: ['behavior'],
      checks: [check], artifact: 'build/result.txt', artifactCheck: { ...check, id: 'artifact' }, allowedPaths: ['.'],
      runtime: { kind: 'macos-sandbox', toolPaths: ['/usr/bin'], network: 'none', authHomes: { codex: null, claude: null } },
      models: { coordinator: model, developer: model, reviewer: model, inspector: model },
      limits: { maxAttempts: 4, maxReworks: 1, attemptTimeoutMs: 1000, maxWallMs: 10000, maxReportedTokens: 10000, verificationReserveAttempts: 2, maxLogBytes: 4096 },
      billing: 'subscription-only', retentionDays: 30,
    },
    specDigest: sha('spec'), profileDigest: sha('profile'), bundleDigest: sha('bundle'), policyDigest: sha('policy'),
    sourceBase: 'a'.repeat(40), createdAt: timestamp, updatedAt: timestamp,
    approval: { owner: 'test operator', statement: 'Local fixtures only' }, attempts: [], checks: [],
    reworks: 0, reportedTokens: 0, unknownUsage: false, elapsedMs: 0, suspended: false,
    lastEvent: event, history: [event],
  };
}

test('recovers only a missing or partial trailing event and rejects conflicting history', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-store-'));
  const store = new Store(root);
  try {
    await store.create(initial());
    const state = await store.transition('fixture', 'ready', 'fixture accepted');
    const events = path.join(store.dir('fixture'), 'events.jsonl');
    const first = JSON.stringify(state.history[0]) + '\n';
    for (const suffix of ['', '{"sequence":2']) {
      await writeFile(events, first + suffix);
      await store.reconcile('fixture', await store.read('fixture'));
      assert.equal(await readFile(events, 'utf8'), state.history.map(e => JSON.stringify(e) + '\n').join(''));
    }
    await writeFile(events, JSON.stringify({ ...state.history[0], type: 'tampered' }) + '\n');
    await assert.rejects(store.reconcile('fixture', state), /Event content conflict/);
    await writeFile(events, '');
    await assert.rejects(store.reconcile('fixture', state), /missing before last transition/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('rejects malformed persisted evidence and prevents invalid mutations from being committed', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-store-'));
  const store = new Store(root);
  try {
    await store.create(initial());
    const file = path.join(store.dir('fixture'), 'state.json');
    const original = await readFile(file, 'utf8');
    await assert.rejects(store.update('fixture', 'bad evidence', {}, state => { state.checks.push({ passed: true } as never); }));
    assert.equal(await readFile(file, 'utf8'), original);
    await writeFile(file, json({ ...initial(), checks: [{ passed: true }] }));
    await assert.rejects(store.read('fixture'));
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('holds the state-root reservation across execution calls until explicitly released', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-store-'));
  const store = new Store(root);
  try {
    await store.execution('first', async () => {});
    await assert.rejects(store.execution('second', async () => {}), /holds the state root/);
    await store.release('first');
    await store.execution('second', async () => {});
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('concurrent updates from two processes both commit without ELOCKED', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-store-'));
  const store = new Store(root);
  try {
    await store.create(initial());
    const storeModule = path.resolve(import.meta.dirname, '../src/store.ts');
    // Each writer holds the run lock for 30 ms per update, so the two processes contend on every write.
    const writer = (name: string) => promisify(execFile)(process.execPath, ['--input-type=module', '-e', `
      const { Store } = await import(${JSON.stringify(storeModule)});
      const store = new Store(${JSON.stringify(root)});
      for (let i = 0; i < 10; i++)
        await store.update('fixture', ${JSON.stringify(name)}, { i }, () => new Promise(resolve => setTimeout(resolve, 30)));
    `], { timeout: 30000 });
    await Promise.all([writer('writer_a'), writer('writer_b')]);
    const state = await store.read('fixture');
    assert.equal(state.revision, 21);
    assert.equal(state.history.filter(event => event.type === 'writer_a').length, 10);
    assert.equal(state.history.filter(event => event.type === 'writer_b').length, 10);
  } finally { await rm(root, { recursive: true, force: true }); }
});
