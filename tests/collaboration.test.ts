import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { after, test } from 'node:test';
import { createDashboardServer } from '../src/dashboard-server.ts';
import type { State } from '../src/contracts.ts';
import { Dashboard } from '../src/dashboard.ts';
import { Store, sha } from '../src/store.ts';

const roots: string[] = [];
after(async () => { await Promise.all(roots.map(root => rm(root, { recursive: true, force: true }))); });

async function openDashboard() {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-dashboard-'));
  roots.push(root);
  const server = createDashboardServer(root);
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Dashboard did not bind a TCP port');
  return { root, base: `http://127.0.0.1:${address.port}`, close: () => new Promise<void>(resolve => server.close(() => resolve())) };
}

function runState(): State {
  const at = '2026-09-23T00:00:00.000Z';
  const model = { provider: 'codex' as const, model: 'gpt-6-sol', effort: 'high' as const };
  const check = { id: 'smoke', argv: ['node', '--test'], cwd: '.', timeoutMs: 1000, requirements: ['journey'], outputPaths: [] };
  const event = { sequence: 1, at, type: 'created', detail: { leak: 'maps-private-key' } };
  return {
    schemaVersion: 1, id: 'run-one', revision: 1, status: 'running',
    project: { schemaVersion: 2, name: 'demo', repository: '/local/demo', base: 'a'.repeat(40), recipient: 'operator',
      brief: 'Build demo', policies: ['policy'], requirements: ['journey'], checks: [check], artifact: 'build/app',
      artifactCheck: { ...check, id: 'artifact' }, allowedPaths: ['src/'], runtime: { kind: 'macos-sandbox',
        toolPaths: ['/usr/bin'], network: 'none', authHomes: { codex: null, claude: null } },
      models: { coordinator: model, developer: model, reviewer: model, inspector: model },
      limits: { maxAttempts: 4, maxReworks: 1, attemptTimeoutMs: 1000, maxWallMs: 10000,
        maxReportedTokens: 10000, verificationReserveAttempts: 2, maxLogBytes: 4096 },
      billing: 'subscription-only', retentionDays: 30 },
    specDigest: sha('spec'), profileDigest: sha('profile'), bundleDigest: sha('bundle'), policyDigest: sha('policy'),
    sourceBase: 'a'.repeat(40), createdAt: at, updatedAt: at,
    approval: { owner: 'operator', statement: 'test' },
    attempts: [{ id: 'attempt-one', role: 'developer', model, startedAt: at, status: 'failed', candidate: 'a'.repeat(40),
      inputTokens: null, outputTokens: null, cachedInputTokens: null }], checks: [],
    reworks: 1, reportedTokens: 700, unknownUsage: false, elapsedMs: 1000, suspended: false,
    activeJob: { id: 'job-one', runtime: 'macos-sandbox', kind: 'worker', startedAt: at },
    lastEvent: event, history: [event],
  };
}

test('existing runs expose empty discovery and ticket collections', async () => {
  const app = await openDashboard();
  try {
    await new Store(app.root).create(runState());
    const view = await (await fetch(`${app.base}/api/dashboard`)).json();
    assert.deepEqual(view.runs[0].specifications, []);
    assert.deepEqual(view.runs[0].tickets, []);
    assert.deepEqual(view.runs[0].questionBatches, []);
  } finally { await app.close(); }
});

function collaborationState(): State {
  const state = runState();
  const at = state.createdAt;
  delete state.activeJob;
  return {
    ...state, status: 'awaiting_input', priorStatus: 'running', suspended: true, control: 'suspend',
    specifications: [
      { id: 'business-brief', kind: 'business', title: 'Approved booking journey', revision: 2,
        content: 'Customers can cancel before dispatch.', digest: sha('Customers can cancel before dispatch.'),
        requirements: ['journey'], approval: { owner: 'client', statement: 'Approved cancellation policy', approvedAt: at } },
      { id: 'domain-rules', kind: 'domain', title: 'Dispatch invariants', revision: 1,
        content: 'Dispatch prevents cancellation.', digest: sha('Dispatch prevents cancellation.'), requirements: ['journey'],
        approval: { owner: 'client', statement: 'Approved dispatch rule', approvedAt: at } },
    ],
    tickets: [{ id: 'cancellation', title: 'Cancel a booking', requirements: ['journey'],
      specificationIds: ['business-brief', 'domain-rules'], status: 'blocked',
      architectNote: 'Worker awaits retention decision.', updatedAt: at, attemptIds: ['attempt-one'] }],
    questionBatches: [{ id: 'policy-questions', title: 'Retention decisions', createdAt: at, questions: [
      { id: 'retention', prompt: 'How long should cancelled bookings remain?', owner: 'client',
        impact: 'Controls storage and deletion.', recommendation: '30 days', options: ['30 days', '90 days'], affectedTicketIds: ['cancellation'] },
      { id: 'notice', prompt: 'What should the cancellation notice say?', owner: 'client',
        impact: 'Changes the customer confirmation.', recommendation: 'Booking cancelled.', options: [], affectedTicketIds: ['cancellation'] },
    ] }],
  };
}

async function submitAnswers(app: Awaited<ReturnType<typeof openDashboard>>, answers: unknown, expectedRevision = 1) {
  const { csrfToken } = await (await fetch(`${app.base}/api/session`)).json();
  return fetch(`${app.base}/api/runs/run-one/answers`, { method: 'POST',
    headers: { Origin: app.base, 'X-CSRF-Token': csrfToken },
    body: JSON.stringify({ expectedRevision, batchId: 'policy-questions', owner: 'client', answers }) });
}

const completeAnswers = [
  { questionId: 'retention', value: '30 days' },
  { questionId: 'notice', value: 'Your booking has been cancelled.' },
];

test('approved business and domain specifications and architect ticket progress reach the operator', async () => {
  const app = await openDashboard();
  try {
    await new Store(app.root).create(collaborationState());
    const view = await (await fetch(`${app.base}/api/dashboard`)).json();
    assert.deepEqual(view.runs[0].specifications.map((spec: { kind: string; title: string; revision: number; content: string; approval: { owner: string } }) =>
      [spec.kind, spec.title, spec.revision, spec.content, spec.approval.owner]), [
      ['business', 'Approved booking journey', 2, 'Customers can cancel before dispatch.', 'client'],
      ['domain', 'Dispatch invariants', 1, 'Dispatch prevents cancellation.', 'client'],
    ]);
    assert.equal(view.runs[0].tickets[0].status, 'blocked');
    assert.equal(view.runs[0].tickets[0].architectNote, 'Worker awaits retention decision.');
    assert.deepEqual(view.runs[0].tickets[0].attemptIds, ['attempt-one']);
    assert.equal(view.runs[0].questionBatches[0].questions[0].impact, 'Controls storage and deletion.');
  } finally { await app.close(); }
});

test('complete operator answers persist for factory reconciliation without resuming or resetting allowances', async () => {
  const app = await openDashboard();
  try {
    await new Store(app.root).create(collaborationState());
    const response = await submitAnswers(app, completeAnswers);
    assert.equal(response.status, 200, await response.text());
    const state = await new Store(app.root).read('run-one');
    assert.equal(state.revision, 2);
    assert.equal(state.status, 'awaiting_input');
    assert.equal(state.priorStatus, 'running');
    assert.equal(state.control, 'suspend');
    assert.equal(state.suspended, true);
    assert.equal(state.reportedTokens, 700);
    assert.equal(state.reworks, 1);
    assert.equal(state.elapsedMs, 1000);
    assert.equal(state.questionBatches?.[0].answer?.owner, 'client');
    assert.equal(state.questionBatches?.[0].answer?.source, 'operator');
    assert.deepEqual(state.questionBatches?.[0].answer?.answers, completeAnswers);
    const view = await (await fetch(`${app.base}/api/dashboard`)).json();
    assert.deepEqual(view.runs[0].questionBatches[0].answer.answers, completeAnswers);
    assert.equal(view.runs[0].issues.some((issue: { code: string }) => issue.code === 'answers_pending_review'), true);
    assert.equal(view.runs[0].issues.some((issue: { code: string }) => issue.code === 'input_required'), false);
    assert.equal((await submitAnswers(app, completeAnswers)).status, 409);
    assert.equal((await new Store(app.root).read('run-one')).revision, 2);
  } finally { await app.close(); }
});

for (const [label, answers] of [
  ['partial batch', completeAnswers.slice(0, 1)],
  ['duplicate question', [completeAnswers[0], completeAnswers[0]]],
  ['unknown question', [completeAnswers[0], { questionId: 'unknown', value: 'Answer' }]],
  ['invalid option', [{ questionId: 'retention', value: 'forever' }, completeAnswers[1]]],
  ['blank free text', [completeAnswers[0], { questionId: 'notice', value: '   ' }]],
] satisfies [string, unknown][]) {
  test(`rejects ${label} without recording any answer`, async () => {
    const app = await openDashboard();
    try {
      await new Store(app.root).create(collaborationState());
      const response = await submitAnswers(app, answers);
      assert.equal(response.status, 400, await response.text());
      const state = await new Store(app.root).read('run-one');
      assert.equal(state.revision, 1);
      assert.equal(state.questionBatches?.[0].answer, undefined);
    } finally { await app.close(); }
  });
}

test('GitHub issue receives approved specifications and architect progress, then reuses its saved identity', async () => {
  const app = await openDashboard();
  try {
    const state = collaborationState();
    state.project.repository = 'https://github.com/acme/widget';
    await new Store(app.root).create(state);
    const writes: { method: string; url: string; body: { title: string; body: string; state: string } }[] = [];
    const dashboard = new Dashboard(app.root, async (input, init) => {
      const url = String(input);
      if (!init?.method) return Response.json(url.endsWith('/42') ? { number: 42, body: '<!-- factory:run-one:cancellation -->' } : []);
      assert.equal(typeof init.body, 'string');
      if (typeof init.body !== 'string') throw new Error('Missing issue payload');
      writes.push({ method: init.method, url, body: JSON.parse(init.body) });
      return Response.json({ number: 42, html_url: 'https://github.com/acme/widget/issues/42' });
    });
    await dashboard.saveCredential({ kind: 'github', value: `github_pat_${'A'.repeat(24)}` });
    await dashboard.syncTickets('run-one', { expectedRevision: 1 });
    assert.equal(writes[0]?.method, 'POST');
    assert.equal(writes[0]?.url, 'https://api.github.com/repos/acme/widget/issues');
    assert.equal(writes[0]?.body.title, 'Cancel a booking');
    assert.equal(writes[0]?.body.state, 'open');
    assert.match(writes[0]?.body.body ?? '', /Customers can cancel before dispatch\./);
    assert.match(writes[0]?.body.body ?? '', /Dispatch prevents cancellation\./);
    assert.match(writes[0]?.body.body ?? '', /Approved by client: Approved cancellation policy/);
    assert.match(writes[0]?.body.body ?? '', /Progress: blocked/);
    assert.match(writes[0]?.body.body ?? '', /Worker awaits retention decision\./);
    assert.equal((await new Store(app.root).read('run-one')).tickets?.[0].github?.url, 'https://github.com/acme/widget/issues/42');
    await dashboard.syncTickets('run-one', { expectedRevision: 2 });
    assert.equal(writes[1]?.method, 'PATCH');
    assert.equal(writes[1]?.url, 'https://api.github.com/repos/acme/widget/issues/42');
  } finally { await app.close(); }
});

test('architect ticket follows worker progress but completion alone never accepts delivery', async () => {
  const app = await openDashboard();
  try {
    const state = collaborationState();
    state.questionBatches = [];
    state.status = 'running';
    const store = new Store(app.root);
    await store.create(state);
    await store.update('run-one', 'worker_started', {}, state => { state.attempts[0].status = 'running'; });
    let view = await (await fetch(`${app.base}/api/dashboard`)).json();
    assert.equal(view.runs[0].tickets[0].status, 'in_progress');
    await store.update('run-one', 'worker_completed', {}, state => { state.attempts[0].status = 'completed'; });
    view = await (await fetch(`${app.base}/api/dashboard`)).json();
    assert.equal(view.runs[0].tickets[0].status, 'in_progress');
    assert.equal((await store.read('run-one')).tickets?.[0].status, 'in_progress');
    await store.update('run-one', 'worker_interrupted', {}, state => { state.attempts[0].status = 'interrupted'; });
    view = await (await fetch(`${app.base}/api/dashboard`)).json();
    assert.equal(view.runs[0].tickets[0].status, 'blocked');
  } finally { await app.close(); }
});

test('GitHub sync refuses an unrelated issue without changing state', async () => {
  const app = await openDashboard();
  try {
    const state = collaborationState();
    state.project.repository = 'https://github.com/acme/widget';
    if (!state.tickets?.[0]) throw new Error('Missing fixture ticket');
    state.tickets[0].github = { number: 42, url: 'https://github.com/acme/widget/issues/42', syncedAt: state.createdAt };
    await new Store(app.root).create(state);
    let writes = 0;
    const dashboard = new Dashboard(app.root, async (_input, init) => {
      assert.equal(init?.redirect, 'error');
      if (init?.method) writes++;
      return Response.json({ number: 42, body: 'Unrelated issue' });
    });
    await dashboard.saveCredential({ kind: 'github', value: `github_pat_${'A'.repeat(24)}` });
    await assert.rejects(dashboard.syncTickets('run-one', { expectedRevision: 1 }), /ownership mismatch/);
    assert.equal(writes, 0);
    assert.equal((await new Store(app.root).read('run-one')).revision, 1);
  } finally { await app.close(); }
});

test('GitHub sync redacts configured secrets and excludes pull requests from retry recovery', async () => {
  const app = await openDashboard();
  try {
    const state = collaborationState();
    const secret = 'private-application-value';
    state.project.repository = 'https://github.com/acme/widget';
    if (!state.tickets?.[0]) throw new Error('Missing fixture ticket');
    state.tickets[0].title = `Cancellation ${secret}`;
    state.tickets[0].architectNote = secret;
    await new Store(app.root).create(state);
    let written = '';
    const dashboard = new Dashboard(app.root, async (_input, init) => {
      assert.equal(init?.redirect, 'error');
      if (!init?.method) return Response.json([{ number: 5, body: '<!-- factory:run-one:cancellation -->', pull_request: {} }]);
      assert.equal(init.method, 'POST');
      written = String(init.body);
      return Response.json({ number: 42, html_url: 'https://github.com/acme/widget/issues/42' });
    });
    await dashboard.saveCredential({ kind: 'github', value: `github_pat_${'A'.repeat(24)}` });
    await dashboard.saveCredential({ kind: 'application', name: 'api-key', value: secret });
    await dashboard.syncTickets('run-one', { expectedRevision: 1 });
    assert.equal(written.includes(secret), false);
    assert.match(written, /\[REDACTED\]/);
  } finally { await app.close(); }
});

test('GitHub remote failure preserves local revision and ticket identity', async () => {
  const app = await openDashboard();
  try {
    const state = collaborationState();
    state.project.repository = 'https://github.com/acme/widget';
    await new Store(app.root).create(state);
    const dashboard = new Dashboard(app.root, async (_input, init) => init?.method ? new Response('unavailable', { status: 503 }) : Response.json([]));
    await dashboard.saveCredential({ kind: 'github', value: `github_pat_${'A'.repeat(24)}` });
    await assert.rejects(dashboard.syncTickets('run-one', { expectedRevision: 1 }), /sync failed/);
    const saved = await new Store(app.root).read('run-one');
    assert.equal(saved.revision, 1);
    assert.equal(saved.tickets?.[0].github, undefined);
  } finally { await app.close(); }
});

test('GitHub retry recovers a remotely created ticket before making another create', async () => {
  const app = await openDashboard();
  try {
    const state = collaborationState();
    state.project.repository = 'https://github.com/acme/widget';
    await new Store(app.root).create(state);
    const methods: string[] = [];
    const dashboard = new Dashboard(app.root, async (input, init) => {
      const issue = { number: 42, body: '<!-- factory:run-one:cancellation -->' };
      if (!init?.method) return Response.json(String(input).endsWith('/42') ? issue : [issue]);
      methods.push(init.method);
      return Response.json({ number: 42, html_url: 'https://github.com/acme/widget/issues/42' });
    });
    await dashboard.saveCredential({ kind: 'github', value: `github_pat_${'A'.repeat(24)}` });
    await dashboard.syncTickets('run-one', { expectedRevision: 1 });
    assert.deepEqual(methods, ['PATCH']);
    assert.equal((await new Store(app.root).read('run-one')).tickets?.[0].github?.number, 42);
  } finally { await app.close(); }
});

test('approved specification and surfaced question history cannot be replaced', async () => {
  const app = await openDashboard();
  try {
    const state = collaborationState();
    const store = new Store(app.root);
    await store.create(state);
    const input = { expectedRevision: 1, specifications: state.specifications, tickets: state.tickets, questionBatches: state.questionBatches };
    await assert.rejects(store.recordCollaboration('run-one', { ...input, specifications: [] }), /history must be preserved/);
    await assert.rejects(store.recordCollaboration('run-one', { ...input, questionBatches: [] }), /history must be preserved/);
    assert.equal((await store.read('run-one')).revision, 1);
  } finally { await app.close(); }
});

test('accepted ticket becomes blocked when candidate acceptance is reopened', async () => {
  const app = await openDashboard();
  try {
    const state = collaborationState();
    state.status = 'verified';
    state.questionBatches = [];
    state.candidate = 'a'.repeat(40);
    const file = { path: 'evidence.json', sha256: sha('evidence') };
    state.review = { attemptId: 'attempt-one', file, record: { schemaVersion: 1, candidate: state.candidate, specDigest: state.specDigest, requirements: ['journey'], verdict: 'pass', findings: [] } };
    state.checks = [{ id: 'smoke', kind: 'check', candidate: state.candidate, specDigest: state.specDigest, definitionDigest: sha('definition'), runtime: 'macos-sandbox',
      startedAt: state.createdAt, endedAt: state.createdAt, exitCode: 0, signal: null, reason: 'completed', stdout: file, stderr: file, passed: true, sourceUnchanged: true, argv: ['node', '--test'], cwd: '.' }];
    if (!state.tickets?.[0]) throw new Error('Missing fixture ticket');
    state.tickets[0].status = 'done';
    const store = new Store(app.root);
    await store.create(state);
    assert.equal((await new Dashboard(app.root).snapshot()).runs[0].tickets[0].status, 'done');
    await store.transition('run-one', 'changes_requested', 'New failing acceptance evidence');
    assert.equal((await store.read('run-one')).tickets?.[0].status, 'blocked');
  } finally { await app.close(); }
});
