import assert from 'node:assert/strict';
import { mkdtemp, rm, lstat, symlink, readFile, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { after, test } from 'node:test';
import { request } from 'node:http';
import { createDashboardServer } from '../src/dashboard-server.ts';
import type { State } from '../src/contracts.ts';
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

test('empty dashboard reports execution controls as available', async () => {
  const app = await openDashboard();
  try {
    const response = await fetch(`${app.base}/api/dashboard`);
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.deepEqual(body.capabilities, { execution: true, liveHeartbeat: false, githubSync: true });
    assert.deepEqual(body.runs, []);
    assert.equal(body.settings.budget.maxAttempts > body.settings.budget.verificationReserveAttempts, true);
    assert.equal(body.credentials.github, false);
  } finally { await app.close(); }
});

test('settings require local origin and CSRF, and reject limits that the project schema cannot use', async () => {
  const app = await openDashboard();
  try {
    const session = await (await fetch(`${app.base}/api/session`)).json();
    const initial = await (await fetch(`${app.base}/api/dashboard`)).json();
    const settings = { ...initial.settings, repositoryUrl: 'https://github.com/acme/widget', budget: { ...initial.settings.budget, maxAttempts: 101 } };
    const hostile = await fetch(`${app.base}/api/settings`, { method: 'PUT', headers: { Origin: 'https://evil.example', 'X-CSRF-Token': session.csrfToken }, body: JSON.stringify(settings) });
    assert.equal(hostile.status, 403);
    const noCsrf = await fetch(`${app.base}/api/settings`, { method: 'PUT', headers: { Origin: app.base }, body: JSON.stringify(settings) });
    assert.equal(noCsrf.status, 403);
    const invalid = await fetch(`${app.base}/api/settings`, { method: 'PUT', headers: { Origin: app.base, 'X-CSRF-Token': session.csrfToken }, body: JSON.stringify(settings) });
    assert.equal(invalid.status, 400);
    const after = await (await fetch(`${app.base}/api/dashboard`)).json();
    assert.equal(after.settings.repositoryUrl, '');
  } finally { await app.close(); }
});

test('credentials remain private and concurrent application key writes preserve both keys', async () => {
  const app = await openDashboard();
  try {
    const { csrfToken } = await (await fetch(`${app.base}/api/session`)).json();
    const put = (body: unknown) => fetch(`${app.base}/api/credentials`, { method: 'PUT', headers: { Origin: app.base, 'X-CSRF-Token': csrfToken }, body: JSON.stringify(body) });
    const [one, two] = await Promise.all([
      put({ kind: 'application', name: 'maps', value: 'maps-private-key' }),
      put({ kind: 'application', name: 'payments', value: 'payments-private-key' }),
    ]);
    assert.equal(one.status, 200);
    assert.equal(two.status, 200);
    const response = await (await fetch(`${app.base}/api/dashboard`)).text();
    assert.equal(response.includes('maps-private-key'), false);
    assert.equal(response.includes('payments-private-key'), false);
    assert.deepEqual(JSON.parse(response).credentials.applicationKeys, ['maps', 'payments']);
    const credentialPath = path.join(app.root, '.dashboard', 'credentials.json');
    assert.equal((await lstat(credentialPath)).mode & 0o777, 0o600);
    const removed = await put({ kind: 'application', name: 'maps', value: '' });
    assert.equal(removed.status, 200);
    assert.deepEqual((await (await fetch(`${app.base}/api/dashboard`)).json()).credentials.applicationKeys, ['payments']);
  } finally { await app.close(); }
});

test('planted private settings symlink is rejected without reading or overwriting its target', async () => {
  const app = await openDashboard();
  try {
    const target = path.join(app.root, 'outside.json');
    const { mkdir, writeFile } = await import('node:fs/promises');
    await mkdir(path.join(app.root, '.dashboard'), { mode: 0o700 });
    await writeFile(target, '{"secret":"outside-secret"}');
    await symlink(target, path.join(app.root, '.dashboard', 'settings.json'));
    const response = await fetch(`${app.base}/api/dashboard`);
    assert.equal(response.status, 500);
    assert.equal((await response.text()).includes('outside-secret'), false);
    assert.equal(await readFile(target, 'utf8'), '{"secret":"outside-secret"}');
  } finally { await app.close(); }
});

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

test('run projection flags unconfirmed job and missing token usage; recovery keeps counters but no freeform event details', async () => {
  const app = await openDashboard();
  try {
    await new Store(app.root).create(runState());
    const view = await (await fetch(`${app.base}/api/dashboard`)).json();
    assert.equal(view.runs[0].reportedTokens, 700);
    assert.equal(view.runs[0].attempts[0].inputTokens, null);
    assert.equal(view.runs[0].issues.some((x: { code: string }) => x.code === 'job_unconfirmed'), true);
    assert.equal(view.runs[0].issues.some((x: { code: string }) => x.code === 'usage_unknown'), true);
    const recovery = await fetch(`${app.base}/api/runs/run-one/recovery`);
    assert.equal(recovery.status, 200);
    assert.match(recovery.headers.get('content-disposition') ?? '', /attachment/);
    const packet = await recovery.text();
    assert.equal(packet.includes('maps-private-key'), false);
    assert.equal(JSON.parse(packet).counters.reportedTokens, 700);
    assert.equal(JSON.parse(packet).kind, 'recovery_brief');
  } finally { await app.close(); }
});

async function session(base: string) {
  const { csrfToken } = await (await fetch(`${base}/api/session`)).json();
  return (url: string, value: unknown, token: string | null = csrfToken) => fetch(`${base}${url}`, {
    method: 'POST', headers: { Origin: base, ...(token ? { 'X-CSRF-Token': token } : {}) }, body: JSON.stringify(value),
  });
}

test('control takes the start, pause and stop body and projects a factory view', async () => {
  const app = await openDashboard();
  try {
    const store = new Store(app.root);
    await store.create(runState());
    const post = await session(app.base);
    const control = (value: unknown) => post('/api/runs/run-one/control', value);
    assert.equal((await control({ action: 'suspend', expectedRevision: 1 })).status, 400);
    assert.equal((await control({ action: 'pause', expectedRevision: 1 })).status, 400);
    const initial = (await (await fetch(`${app.base}/api/dashboard`)).json()).runs[0];
    assert.equal('controlPending' in initial, false);
    assert.deepEqual(initial.factory, { state: 'idle', supervisor: null, activeJob: { id: 'job-one', kind: 'worker', startedAt: '2026-09-23T00:00:00.000Z', frozen: false },
      canStart: true, canPause: true, canStop: true, startLabel: 'Start', reason: null, orphans: [] });
    const paused = await control({ action: 'pause' });
    assert.equal(paused.status, 200);
    const pausedBody = await paused.json();
    assert.equal(pausedBody.changed, true);
    assert.equal(pausedBody.factory.state, 'idle');
    assert.equal(pausedBody.factory.startLabel, 'Resume');
    assert.equal(pausedBody.factory.canPause, false);
    assert.equal((await store.read('run-one')).control, 'suspend');
    assert.equal((await (await control({ action: 'pause' })).json()).changed, false);
    assert.equal((await store.read('run-one')).revision, 2);
    // Stop with no supervisor records the cancellation and resolves the unproven job record.
    const stopped = await control({ action: 'stop' });
    assert.equal(stopped.status, 200);
    const stoppedBody = await stopped.json();
    assert.equal(stoppedBody.changed, true);
    assert.equal(stoppedBody.factory.state, 'terminal');
    assert.deepEqual([stoppedBody.factory.canStart, stoppedBody.factory.canPause, stoppedBody.factory.canStop], [false, false, false]);
    const state = await store.read('run-one');
    assert.equal(state.status, 'cancelled');
    assert.equal(state.activeJob, undefined);
    assert.equal(state.control, undefined);
    assert.equal(state.history.at(-1)?.type, 'run_cancelled_recovered');
    const bytes = await readFile(path.join(store.dir('run-one'), 'state.json'));
    const repeat = await control({ action: 'stop' });
    assert.equal((await repeat.json()).changed, false);
    const start = await control({ action: 'start', expectedRevision: state.revision });
    assert.equal(start.status, 409);
    assert.equal((await start.json()).code, 'factory_conflict');
    assert.deepEqual(await readFile(path.join(store.dir('run-one'), 'state.json')), bytes);
  } finally { await app.close(); }
});

test('start rejects a stale revision without launching or writing', async () => {
  const app = await openDashboard();
  try {
    const store = new Store(app.root);
    await store.create(runState());
    await store.update('run-one', 'fixture_changed', {}, () => {});
    const bytes = await readFile(path.join(store.dir('run-one'), 'state.json'));
    const response = await (await session(app.base))('/api/runs/run-one/control', { action: 'start', expectedRevision: 1 });
    assert.equal(response.status, 409);
    assert.equal((await response.json()).code, 'stale_revision');
    assert.deepEqual(await readFile(path.join(store.dir('run-one'), 'state.json')), bytes);
    await assert.rejects(lstat(path.join(store.dir('run-one'), 'supervisor')), { code: 'ENOENT' });
  } finally { await app.close(); }
});

test('control and Claude login routes reject requests without the session CSRF token', async () => {
  const app = await openDashboard();
  try {
    const store = new Store(app.root);
    await store.create(runState());
    const post = await session(app.base);
    for (const [url, value] of [['/api/runs/run-one/control', { action: 'pause' }], ['/api/auth/claude/login', {}],
      ['/api/auth/claude/login/code', { code: 'fixture-code' }], ['/api/auth/claude/login/cancel', {}]] as const) {
      for (const token of [null, 'wrong-token']) {
        const response = await post(url, value, token);
        assert.equal(response.status, 403, url);
        assert.equal((await response.json()).code, 'csrf_rejected');
      }
    }
    assert.equal((await store.read('run-one')).revision, 1);
    const script = await fetch(`${app.base}/claude-login.js`);
    assert.equal(script.status, 200);
    assert.match(script.headers.get('content-type') ?? '', /^text\/javascript/);
  } finally { await app.close(); }
});

test('malformed GitHub setup is rejected and no credential value enters responses', async () => {
  const app = await openDashboard();
  try {
    const { csrfToken } = await (await fetch(`${app.base}/api/session`)).json();
    const headers = { Origin: app.base, 'X-CSRF-Token': csrfToken };
    const settings = (await (await fetch(`${app.base}/api/dashboard`)).json()).settings;
    const badRepo = await fetch(`${app.base}/api/settings`, { method: 'PUT', headers,
      body: JSON.stringify({ ...settings, repositoryUrl: 'https://user:password@github.com/acme/repo' }) });
    assert.equal(badRepo.status, 400);
    const badToken = await fetch(`${app.base}/api/credentials`, { method: 'PUT', headers,
      body: JSON.stringify({ kind: 'github', value: 'password' }) });
    assert.equal(badToken.status, 400);
    const good = await fetch(`${app.base}/api/credentials`, { method: 'PUT', headers,
      body: JSON.stringify({ kind: 'github', value: `github_pat_${'A'.repeat(24)}` }) });
    assert.equal(good.status, 200);
    assert.equal((await good.text()).includes('github_pat_'), false);
    const view = await (await fetch(`${app.base}/api/dashboard`)).text();
    assert.equal(view.includes('github_pat_'), false);
    assert.equal(JSON.parse(view).credentials.github, true);
    const check = await fetch(`${app.base}/api/auth/check`, { method: 'POST', headers, body: JSON.stringify({ provider: 'claude' }) });
    assert.deepEqual((await check.json()).ok, false);
  } finally { await app.close(); }
});

test('a corrupt run appears as an issue while another run remains visible', async () => {
  const app = await openDashboard();
  try {
    await new Store(app.root).create(runState());
    await mkdir(path.join(app.root, 'broken'));
    await writeFile(path.join(app.root, 'broken', 'state.json'), '{broken');
    const response = await fetch(`${app.base}/api/dashboard`);
    assert.equal(response.status, 200);
    const view = await response.json();
    assert.deepEqual(view.runs.map((run: { id: string }) => run.id), ['run-one']);
    assert.equal(view.issues[0].code, 'run_corrupt');
    assert.equal(view.issues[0].runId, 'broken');
  } finally { await app.close(); }
});

test('short application secrets are rejected and saved secrets only redact freeform display text', async () => {
  const app = await openDashboard();
  try {
    const { csrfToken } = await (await fetch(`${app.base}/api/session`)).json();
    const short = await fetch(`${app.base}/api/credentials`, { method: 'PUT',
      headers: { Origin: app.base, 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ kind: 'application', name: 'short', value: 'a' }) });
    assert.equal(short.status, 400);
    const secret = 'private-secret-value';
    const saved = await fetch(`${app.base}/api/credentials`, { method: 'PUT',
      headers: { Origin: app.base, 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ kind: 'application', name: 'long', value: secret }) });
    assert.equal(saved.status, 200);
    const settings = (await (await fetch(`${app.base}/api/dashboard`)).json()).settings;
    const configured = await fetch(`${app.base}/api/settings`, { method: 'PUT',
      headers: { Origin: app.base, 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ ...settings, brief: `Build with ${secret}` }) });
    assert.equal(configured.status, 200);
    await new Store(app.root).create(runState());
    const view = await (await fetch(`${app.base}/api/dashboard`)).json();
    assert.equal(view.settings.brief, 'Build with [REDACTED]');
    assert.equal(view.settings.models.coordinator.model, 'gpt-6-astra');
    assert.equal(view.runs[0].id, 'run-one');
    assert.equal(view.runs[0].status, 'running');
    assert.equal(typeof view.settings.repositoryUrl, 'string');
    assert.deepEqual(view.credentials.applicationKeys, ['long']);
  } finally { await app.close(); }
});

test('host spoofing and oversized writes are refused by the HTTP boundary', async () => {
  const app = await openDashboard();
  try {
    const spoofed = await new Promise<number>((resolve, reject) => {
      const req = request(`${app.base}/api/session`, { headers: { Host: 'evil.example' } }, res => {
        res.resume(); res.on('end', () => resolve(res.statusCode ?? 0));
      });
      req.on('error', reject); req.end();
    });
    assert.equal(spoofed, 403);
    const { csrfToken } = await (await fetch(`${app.base}/api/session`)).json();
    const huge = await fetch(`${app.base}/api/settings`, { method: 'PUT',
      headers: { Origin: app.base, 'X-CSRF-Token': csrfToken }, body: 'x'.repeat(70000) });
    assert.equal(huge.status, 413);
    assert.equal((await huge.text()).includes('x'.repeat(100)), false);
  } finally { await app.close(); }
});

test('corrupt stored settings are treated as a record fault, not a bad user submission', async () => {
  const app = await openDashboard();
  try {
    await mkdir(path.join(app.root, '.dashboard'), { mode: 0o700 });
    await writeFile(path.join(app.root, '.dashboard', 'settings.json'), '{broken', { mode: 0o600 });
    const response = await fetch(`${app.base}/api/dashboard`);
    assert.equal(response.status, 500);
    assert.equal((await response.json()).code, 'record_unavailable');
  } finally { await app.close(); }
});
