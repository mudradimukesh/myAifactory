import assert from 'node:assert/strict';
import { mkdtemp, rm, lstat, symlink, readFile, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { after, test } from 'node:test';
import { request } from 'node:http';
import { createDashboardServer } from '../src/dashboard-server.ts';
import type { State } from '../src/contracts.ts';
import { Store, sha } from '../src/store.ts';
import { Dashboard, FactoryConflict, projectRun, factoryView } from '../src/dashboard.ts';
import { meterReadingSchema, type Segment } from '../src/contracts.ts';
import { identify } from '../src/process.ts';

test('segment projection preserves counters and sums role and run usage', () => {
  const state = runState();
  delete state.activeJob;
  const segment: Segment = { index: 1, kind: 'work', model: 'gpt-6-sol', startedAt: state.createdAt,
    endedAt: state.createdAt, reason: 'completed', raw: { input_tokens: 100, output_tokens: 20 },
    input: 100, cached: 0, output: 20, metered: 120, contextMax: 258400, trigger: 155040,
    peakContext: 120, source: 'final' };
  const attempt = state.attempts[0]!;
  attempt.status = 'completed'; attempt.inputTokens = 300; attempt.outputTokens = 60;
  attempt.segments = [segment, { ...segment, index: 2, kind: 'handoff' }, { ...segment, index: 3 }];
  state.attempts.push({ ...attempt, id: 'review-one', role: 'reviewer', segments: [{ ...segment, contextWindowMismatch: true }] });
  state.reportedTokens = 480;
  const view = projectRun(state);
  assert.deepEqual(view.roles.map(role => role.role), ['business', 'domain', 'architect', 'developer', 'reviewer', 'tester', 'coordinator']);
  for (const group of view.usageByRole) {
    const source = state.attempts.find(a => a.role === group.role)!;
    assert.deepEqual(group.agents.map(({ raw, input, cached, output, metered }) => ({ raw, input, cached, output, metered })),
      source.segments!.map(({ raw, input, cached, output, metered }) => ({ raw, input, cached, output, metered })));
    assert.equal(group.total.metered, group.agents.reduce((sum, row) => sum + row.metered!, 0));
    for (const key of ['input', 'cached', 'output'] as const)
      assert.equal(group.total[key], group.agents.reduce((sum, row) => sum + row[key]!, 0));
    assert.deepEqual(group.total.raw, { input_tokens: group.agents.length * 100, output_tokens: group.agents.length * 20 });
  }
  assert.equal(view.usageTotal.metered, state.reportedTokens);
  assert.ok(view.issues.some(issue => issue.code === 'context_window_mismatch'));
  attempt.segments.push({ ...segment, index: 4, raw: null, input: null, cached: null, output: null, metered: null });
  const unknown = projectRun(state).usageByRole[0]!;
  assert.equal(unknown.total.metered, null);
  assert.equal(unknown.lowerBound.metered, 360);
});

test('live usage and context do not show provider-start waiting text', () => {
  const state = runState();
  state.activeJob = { id: 'attempt-1', runtime: 'macos-sandbox', kind: 'architect', startedAt: state.createdAt };
  state.attempts[0].id = 'attempt-1';
  const reading = meterReadingSchema.parse({ segment: 1, kind: 'work', model: 'gpt-6-sol', raw: { input_tokens: 10 }, usage: { input: 10, cached: 0, output: 2 }, metered: 12,
    spentBefore: 0, allowance: 100, contextTokens: 50, peakContext: 50, contextMax: 100, trigger: 60, at: state.createdAt, source: 'meter', providerStartedAt: null, lastActivityAt: state.createdAt });
  const row = projectRun(state, undefined, value => value, undefined, [reading]).usageByRole[0].agents[0];
  assert.equal(row.providerStartedAt, null);
  assert.equal(row.input, 10);
  assert.equal(row.context?.contextTokens, 50);
  assert.equal(row.live, true);
});

test('context recommendation uses live occupancy instead of cumulative input', () => {
  const state = runState();
  state.activeJob!.id = state.attempts[0]!.id;
  state.attempts[0]!.inputTokens = 4148633;
  const meter = meterReadingSchema.parse({ segment: 1, kind: 'work', model: 'gpt-6-sol', raw: null,
    usage: null, metered: null, spentBefore: 0, allowance: 10000000, contextTokens: 122449,
    peakContext: 122449, contextMax: 258400, trigger: 155040, at: state.createdAt, source: 'meter' });
  const warning = (meters: typeof meter[]) => projectRun(state, undefined, undefined, undefined, meters)
    .issues.find(issue => issue.code === 'reported_input_threshold');
  assert.equal(warning([meter]), undefined);
  const reached = warning([{ ...meter, contextTokens: 155040 }]);
  assert.ok(reached);
  assert.match(reached.message, /155040.*155040/);
  assert.doesNotMatch(reached.recommendation, /occupancy is unknown/);
  assert.match(warning([])!.recommendation, /occupancy is unknown/);
  assert.match(warning([{ ...meter, contextTokens: null }])!.recommendation, /occupancy is unknown/);
});

test('snapshot reads active segment meters with current context and preserves unknown usage', { timeout: 10000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-meter-')); roots.push(root);
  const dashboard = new Dashboard(root), state = runState();
  state.activeJob!.id = state.attempts[0]!.id; state.activeJob!.kind = 'developer';
  state.attempts[0]!.status = 'running';
  await dashboard.store.create(state);
  const dir = path.join(dashboard.store.dir(state.id), 'attempts', state.activeJob!.id);
  const meter = meterReadingSchema.parse({ segment: 1, kind: 'work', model: 'gpt-6-sol', raw: { input_tokens: 100 },
    usage: { input: 100, cached: 50, output: 10 }, metered: 65, spentBefore: 0, allowance: 10000,
    contextTokens: 80, peakContext: 90, contextMax: 258400, trigger: 155040, at: state.createdAt, source: 'meter' });
  for (const index of [1, 2]) {
    await mkdir(path.join(dir, `segment-${index}`), { recursive: true });
    await writeFile(path.join(dir, `segment-${index}`, 'meter.json'), JSON.stringify({ ...meter, segment: index }));
  }
  const view = (await dashboard.snapshot()).runs[0]!;
  const rows = view.usageByRole[0]!.agents;
  assert.equal(rows.length, 2);
  assert.equal(rows[0]!.live, false);
  assert.equal(rows[1]!.live, true);
  assert.deepEqual(rows[1]!.raw, meter.raw);
  assert.equal(rows[1]!.metered, meter.metered);
  assert.deepEqual(rows[1]!.context, { contextMax: meter.contextMax, trigger: meter.trigger,
    contextTokens: meter.contextTokens, peakContext: meter.peakContext, progress: 80 / 155040 });
  await writeFile(path.join(dir, 'segment-2', 'meter.json'), JSON.stringify({ ...meter, segment: 2, raw: null, usage: null, metered: null }));
  assert.equal((await dashboard.snapshot()).runs[0]!.usageByRole[0]!.total.metered, null);
  await writeFile(path.join(dir, 'segment-2', 'meter.json'), JSON.stringify({ ...meter, segment: 2, usage: { input: -1, cached: 0, output: 0 } }));
  const corrupt = await dashboard.snapshot();
  assert.equal(corrupt.runs.length, 0);
  assert.ok(corrupt.issues.some(issue => issue.code === 'run_corrupt'));
  await dashboard.store.update(state.id, 'check_started', {}, s => { s.activeJob!.kind = 'check'; });
  const check = (await dashboard.snapshot()).runs[0]!;
  assert.equal(check.usageByRole[0]!.agents.some(row => row.live), false);
  await dashboard.store.update(state.id, 'job_finished', {}, s => { delete s.activeJob; });
  assert.equal((await dashboard.snapshot()).runs.length, 1);
});

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

test('settings accept a Claude developer', async () => {
  const app = await openDashboard();
  try {
    const { csrfToken } = await (await fetch(`${app.base}/api/session`)).json();
    const initial = await (await fetch(`${app.base}/api/dashboard`)).json();
    const settings = { ...initial.settings, models: { ...initial.settings.models, developer: { provider: 'claude', model: 'claude-sonnet-5', effort: 'medium' } } };
    const response = await fetch(`${app.base}/api/settings`, { method: 'PUT', headers: { Origin: app.base, 'X-CSRF-Token': csrfToken }, body: JSON.stringify(settings) });
    assert.equal(response.status, 200);
    const after = await (await fetch(`${app.base}/api/dashboard`)).json();
    assert.deepEqual(after.settings.models.developer, { provider: 'claude', model: 'claude-sonnet-5', effort: 'medium' });
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

async function activityRunDir(content: string | null, jobId = 'job-one') {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-activity-'));
  roots.push(root);
  const dir = path.join(root, 'attempts', jobId, 'capture');
  await mkdir(dir, { recursive: true });
  if (content !== null) await writeFile(path.join(dir, 'stdout.log'), content);
  return root;
}

test('activity summarizes a Codex capture log without showing message text or reasoning', async () => {
  const log = [
    { type: 'item.started', item: { type: 'command_execution', command: 'npm test' } },
    { type: 'item.completed', item: { type: 'command_execution', command: 'npm test', exit_code: 0 } },
    { type: 'item.completed', item: { type: 'agent_message', text: 'All tests pass, this text must never appear.' } },
    { type: 'item.completed', item: { type: 'reasoning', text: 'Secret chain of thought.' } },
    { type: 'turn.completed', usage: {} },
  ].map(event => JSON.stringify(event)).join('\n') + '\n';
  const root = await activityRunDir(log);
  const view = await factoryView(runState(), value => value, root);
  assert.deepEqual(view.activity.map(entry => entry.summary), ['running npm test', 'ran npm test (exit 0)', 'message', 'reasoning', 'turn finished']);
  assert.deepEqual(view.activity.map(entry => entry.ageSeconds === null), [true, true, true, true, false]);
  assert.ok(view.activity.every(entry => entry.job === 'job-one'));
});

test('activity summarizes a Claude capture log, skipping user/tool_result lines', async () => {
  const log = [
    { type: 'assistant', message: { content: [{ type: 'tool_use', name: 'Edit', input: { file_path: 'src/app.ts' } }] } },
    { type: 'assistant', message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: 'npm test' } }] } },
    { type: 'assistant', message: { content: [{ type: 'tool_use', name: 'StructuredOutput', input: { plan: 'secret plan text' } }] } },
    { type: 'user', message: { content: [{ type: 'tool_result', content: 'secret tool output' }] } },
    { type: 'result', subtype: 'success', result: 'Implemented and committed', usage: {} },
  ].map(event => JSON.stringify(event)).join('\n') + '\n';
  const root = await activityRunDir(log);
  const view = await factoryView(runState(), value => value, root);
  assert.deepEqual(view.activity.map(entry => entry.summary), ['Edit src/app.ts', 'Bash npm test', 'StructuredOutput', 'finished']);
});

test('activity drops the leading partial line when the capture log exceeds 64 KB', async () => {
  const padding = 'x'.repeat(70000);
  const events = [
    { type: 'item.completed', item: { type: 'command_execution', command: 'first', exit_code: 0 } },
    { type: 'item.completed', item: { type: 'command_execution', command: 'second', exit_code: 0 } },
  ].map(event => JSON.stringify(event)).join('\n');
  const log = `${padding}\n${events}\n`;
  assert.ok(Buffer.byteLength(log) > 65536);
  const root = await activityRunDir(log);
  const view = await factoryView(runState(), value => value, root);
  assert.deepEqual(view.activity.map(entry => entry.summary), ['ran first (exit 0)', 'ran second (exit 0)']);
});

test('activity is empty when the capture log is missing', async () => {
  const root = await activityRunDir(null);
  const view = await factoryView(runState(), value => value, root);
  assert.deepEqual(view.activity, []);
});

test('activity redacts secrets found in a command summary', async () => {
  const log = JSON.stringify({ type: 'item.completed', item: { type: 'command_execution', command: 'deploy --token sk-secret123', exit_code: 0 } }) + '\n';
  const root = await activityRunDir(log);
  const redact = (value: string) => value.replaceAll('sk-secret123', '[REDACTED]');
  const view = await factoryView(runState(), redact, root);
  assert.deepEqual(view.activity.map(entry => entry.summary), ['ran deploy --token [REDACTED] (exit 0)']);
});

test('activity redacts a secret that crosses the 160-character cut', async () => {
  const log = JSON.stringify({ type: 'item.completed', item: { type: 'command_execution', command: 'a'.repeat(150) + ' sk-secret123', exit_code: 0 } }) + '\n';
  const root = await activityRunDir(log);
  const view = await factoryView(runState(), value => value.replaceAll('sk-secret123', '[REDACTED]'), root);
  assert.ok(!view.activity[0]!.summary.includes('sk-sec'));
});

test('an unreadable activity log leaves the run view intact', async () => {
  const root = await activityRunDir('');
  const file = path.join(root, 'attempts', runState().activeJob!.id, 'capture', 'stdout.log');
  await rm(file); await mkdir(file);
  assert.deepEqual((await factoryView(runState(), value => value, root)).activity, []);
});

test('activity cuts a long command summary to 160 characters', async () => {
  const command = 'a'.repeat(500);
  const log = JSON.stringify({ type: 'item.completed', item: { type: 'command_execution', command, exit_code: 1 } }) + '\n';
  const root = await activityRunDir(log);
  const view = await factoryView(runState(), value => value, root);
  assert.equal(view.activity[0]!.summary.length, 163);
  assert.ok(view.activity[0]!.summary.endsWith('...'));
});

test('a check job has no activity', async () => {
  const log = JSON.stringify({ type: 'item.completed', item: { type: 'command_execution', command: 'npm test', exit_code: 0 } }) + '\n';
  const state = runState();
  state.activeJob = { ...state.activeJob!, kind: 'check' };
  const root = await activityRunDir(log);
  const view = await factoryView(state, value => value, root);
  assert.deepEqual(view.activity, []);
});

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

test('pause refuses a live unsupervised job without changing state', { timeout: 10000 }, async () => {
  const app = await openDashboard();
  try {
    const store = new Store(app.root), state = runState();
    const self = await identify(process.pid);
    assert.ok(self);
    state.activeJob!.process = self;
    await store.create(state);
    const post = await session(app.base);
    const response = await post('/api/runs/run-one/control', { action: 'pause' });
    assert.equal(response.status, 409);
    const body = await response.json();
    assert.equal(body.code, 'factory_conflict');
    assert.equal(body.message, 'A worker started outside the dashboard is running. Pause cannot freeze it. Use Stop.');
    assert.deepEqual(await store.read(state.id), state);
  } finally { await app.close(); }
});

test('idle pause says nothing was running', { timeout: 10000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-idle-pause-')); roots.push(root);
  const dashboard = new Dashboard(root), state = runState();
  state.activeJob = undefined;
  await dashboard.store.create(state);
  const result = await dashboard.control(state.id, { action: 'pause' });
  assert.equal(result.changed, true);
  assert.equal(result.message, 'Paused. Nothing was running.');
  assert.equal((await dashboard.store.read(state.id)).control, 'suspend');
});

test('control takes the start, pause and stop body and projects a factory view', { timeout: 10000 }, async () => {
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
      canStart: true, canPause: true, canStop: true, canReset: false, startLabel: 'Start', reason: null, orphans: [], activity: [] });
    const paused = await control({ action: 'pause' });
    assert.equal(paused.status, 200);
    const pausedBody = await paused.json();
    assert.equal(pausedBody.changed, true);
    assert.equal(pausedBody.message, 'Paused. Nothing was running.');
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

test('reset returns a quiescent failed run to ready and preserves history and attempts', { timeout: 10000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-reset-')); roots.push(root);
  const dashboard = new Dashboard(root); const state = runState(); state.status = 'failed'; delete state.activeJob;
  await dashboard.store.create(state);
  const result = await dashboard.control(state.id, { action: 'reset' });
  assert.equal(result.changed, true);
  assert.equal(result.factory.canStart, true);
  const reset = await dashboard.store.read(state.id);
  assert.equal(reset.status, 'ready');
  assert.equal(reset.attempts.length, state.attempts.length);
  assert.equal(reset.history.at(-1)?.type, 'factory_reset');
  assert.equal(reset.activeJob, undefined);
  assert.equal(reset.supervisor, undefined);
});

test('reset rejects running and control-pending runs', { timeout: 10000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-reset-reject-')); roots.push(root);
  const dashboard = new Dashboard(root); const running = runState(); await dashboard.store.create(running);
  await assert.rejects(() => dashboard.control(running.id, { action: 'reset' }), FactoryConflict);
  const pending = runState(); pending.id = 'run-pending'; pending.status = 'failed'; delete pending.activeJob; pending.control = 'suspend';
  await dashboard.store.create(pending);
  await assert.rejects(() => dashboard.control(pending.id, { action: 'reset' }), FactoryConflict);
});

test('reset is disabled and rejected during a visual-review wait', { timeout: 10000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-reset-visual-')); roots.push(root);
  const dashboard = new Dashboard(root); const state = runState();
  state.status = 'awaiting_input'; state.priorStatus = 'verified'; delete state.activeJob;
  await dashboard.store.create(state);
  assert.equal((await dashboard.snapshot()).runs[0]!.factory?.canReset, false);
  await assert.rejects(() => dashboard.control(state.id, { action: 'reset' }), FactoryConflict);
  assert.equal((await dashboard.store.read(state.id)).status, 'awaiting_input');
});

for (const priorStatus of ['running', 'verifying'] as const)
test(`reset accepts a stalled ${priorStatus} execution`, { timeout: 10000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), `factory-reset-${priorStatus}-`)); roots.push(root);
  const dashboard = new Dashboard(root), state = runState();
  state.status = 'awaiting_input'; state.priorStatus = priorStatus; delete state.activeJob;
  await dashboard.store.create(state);
  const result = await dashboard.control(state.id, { action: 'reset' });
  assert.equal(result.changed, true);
  assert.equal((await dashboard.store.read(state.id)).status, 'ready');
});

test('start rejects a stale revision without launching or writing', { timeout: 10000 }, async () => {
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
