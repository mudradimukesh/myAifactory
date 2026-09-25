import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createDashboardServer } from '../src/dashboard-server.ts';
import type { State, VisualEvidence } from '../src/contracts.ts';
import { Store, sha } from '../src/store.ts';

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZsAAAAASUVORK5CYII=', 'base64');
const candidate = 'b'.repeat(40);
const specDigest = sha('approved specification');

function visualState(): State {
  const at = '2026-09-24T00:00:00.000Z';
  const model = { provider: 'codex' as const, model: 'gpt-5.6-sol', effort: 'high' as const };
  const check = { id: 'smoke', argv: ['node', '--test'], cwd: '.', timeoutMs: 1000, requirements: ['render'], outputPaths: [] };
  const event = { sequence: 1, at, type: 'created', detail: {} };
  return {
    schemaVersion: 1, id: 'visual-run', revision: 1, status: 'awaiting_input', priorStatus: 'verified',
    project: { schemaVersion: 2, name: 'visual-demo', repository: '/local/demo', base: 'a'.repeat(40),
      recipient: 'operator', brief: 'Review rendering', policies: ['policy'], requirements: ['render'], checks: [check],
      artifact: 'render-manifest.json', artifactCheck: { ...check, id: 'artifact' }, allowedPaths: ['src/'],
      runtime: { kind: 'macos-sandbox', toolPaths: ['/usr/bin'], network: 'none', authHomes: { codex: null, claude: null } },
      models: { coordinator: model, developer: model, reviewer: model, inspector: model },
      limits: { maxAttempts: 4, maxReworks: 1, attemptTimeoutMs: 1000, maxWallMs: 10000,
        maxReportedTokens: 10000, verificationReserveAttempts: 2, maxLogBytes: 4096 },
      billing: 'subscription-only', retentionDays: 30 },
    specDigest, profileDigest: sha('profile'), bundleDigest: sha('bundle'), policyDigest: sha('policy'),
    sourceBase: 'a'.repeat(40), candidate, createdAt: at, updatedAt: at,
    approval: { owner: 'operator', statement: 'approved brief' }, attempts: [], checks: [],
    reworks: 0, reportedTokens: 0, unknownUsage: false, elapsedMs: 0, suspended: false,
    lastEvent: event, history: [event],
  };
}

async function fixture() {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-visual-'));
  const sourceRoot = await mkdtemp(path.join(tmpdir(), 'factory-render-source-'));
  await writeFile(path.join(sourceRoot, 'view.png'), png);
  const store = new Store(root);
  await store.create(visualState());
  const source: VisualEvidence = { id: 'view-one', label: 'Living room perspective', sourceId: 'case-one',
    path: 'view.png', sha256: sha(png), mimeType: 'image/png', candidate, specDigest };
  const [image] = await store.importVisualEvidence('visual-run', 'visual-review', 'approve-renders', [source], sourceRoot);
  await store.update('visual-run', 'visual_review_created', {}, state => {
    state.questionBatches = [{ id: 'visual-review', title: 'Render review', createdAt: state.createdAt,
      questions: [{ id: 'approve-renders', prompt: 'Approve the render?', owner: 'operator', impact: 'Controls handoff',
        recommendation: 'Inspect the image', options: ['approve', 'request_changes'], affectedTicketIds: [], visualEvidence: [image] }] }];
  });
  const server = createDashboardServer(root);
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw Error('Dashboard did not bind');
  const base = `http://127.0.0.1:${address.port}`;
  return { root, sourceRoot, store, image, base,
    close: async () => { await new Promise<void>(resolve => server.close(() => resolve())); await rm(root, { recursive: true, force: true }); await rm(sourceRoot, { recursive: true, force: true }); } };
}

async function answer(base: string, revision: number, value: string, owner = 'operator') {
  const { csrfToken } = await (await fetch(`${base}/api/session`)).json();
  return fetch(`${base}/api/runs/visual-run/answers`, { method: 'POST',
    headers: { Origin: base, 'X-CSRF-Token': csrfToken },
    body: JSON.stringify({ expectedRevision: revision, batchId: 'visual-review', owner,
      answers: [{ questionId: 'approve-renders', value }] }) });
}

test('dashboard serves only question-linked image bytes and records explicit approval', async () => {
  const app = await fixture();
  try {
    const view = await (await fetch(`${app.base}/api/dashboard`)).json();
    const projected = view.runs[0].questionBatches[0].questions[0].visualEvidence[0];
    assert.equal(projected.sourceId, 'case-one');
    assert.equal(projected.label, 'Living room perspective');
    assert.equal('path' in projected, false);
    const response = await fetch(`${app.base}${projected.url}`);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('content-type'), 'image/png');
    assert.deepEqual(Buffer.from(await response.arrayBuffer()), png);
    assert.equal((await fetch(`${app.base}/api/runs/visual-run/questions/visual-review/approve-renders/images/unlinked`)).status, 404);
    assert.equal((await answer(app.base, 2, 'approve')).status, 200);
    assert.equal((await app.store.read('visual-run')).questionBatches?.[0].answer?.answers[0].value, 'approve');
  } finally { await app.close(); }
});

test('changed image bytes block approval but permit a request for changes', async () => {
  const app = await fixture();
  try {
    await writeFile(path.join(app.store.dir('visual-run'), app.image.path), Buffer.from('changed'));
    const projected = (await (await fetch(`${app.base}/api/dashboard`)).json()).runs[0].questionBatches[0].questions[0].visualEvidence[0];
    assert.equal((await fetch(`${app.base}${projected.url}`)).status, 500);
    assert.equal((await answer(app.base, 2, 'approve')).status, 500);
    assert.equal((await app.store.read('visual-run')).questionBatches?.[0].answer, undefined);
    assert.equal((await answer(app.base, 2, 'request_changes')).status, 200);
  } finally { await app.close(); }
});

test('import rejects traversal and a candidate change hides earlier images', async () => {
  const app = await fixture();
  try {
    await assert.rejects(app.store.importVisualEvidence('visual-run', 'visual-review', 'approve-renders',
      [{ ...app.image, path: '../outside.png' }], app.sourceRoot), /Unsafe relative path/);
    const [again] = await app.store.importVisualEvidence('visual-run', 'visual-review', 'approve-renders',
      [{ ...app.image, path: 'view.png' }], app.sourceRoot);
    assert.deepEqual(await readFile(path.join(app.store.dir('visual-run'), again.path)), png);
    await app.store.update('visual-run', 'candidate_replaced', {}, state => { state.candidate = 'c'.repeat(40); });
    assert.equal((await fetch(`${app.base}/api/runs/visual-run/questions/visual-review/approve-renders/images/view-one`)).status, 404);
    assert.equal((await answer(app.base, 3, 'approve')).status, 400);
  } finally { await app.close(); }
});

for (const value of ['approve', 'request_changes']) {
  test(`wrong visual answer owner cannot consume the batch for ${value}`, async () => {
    const app = await fixture();
    try {
      const wrong = await answer(app.base, 2, value, 'other-operator');
      const afterWrong = await app.store.read('visual-run');
      const correct = await answer(app.base, afterWrong.revision, value);
      assert.equal(wrong.status, 400, await wrong.text());
      assert.equal(afterWrong.revision, 2);
      assert.equal(afterWrong.questionBatches?.[0].answer, undefined);
      assert.equal(correct.status, 200, await correct.text());
      const saved = await app.store.read('visual-run');
      assert.equal(saved.revision, 3);
      assert.equal(saved.questionBatches?.[0].answer?.owner, 'operator');
      assert.equal(saved.questionBatches?.[0].answer?.answers[0].value, value);
    } finally { await app.close(); }
  });
}
