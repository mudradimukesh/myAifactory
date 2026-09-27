import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { promisify } from 'node:util';
import { createRun } from '../src/coordinator.ts';
import type { OwnedProcess, Project, State } from '../src/contracts.ts';
import { Dashboard, FactoryConflict, StopIncomplete, factoryView } from '../src/dashboard.ts';
import { createDashboardServer } from '../src/dashboard-server.ts';
import { git } from '../src/git.ts';
import { identify, terminateOwned } from '../src/process.ts';
import { move } from '../src/store.ts';

const fixtureScript = path.resolve(import.meta.dirname, 'fixtures', 'supervisor-fixture.ts');
const cliScript = path.resolve(import.meta.dirname, '../src/factory-cli.ts');
const exec = promisify(execFile);
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
const model = { provider: 'codex' as const, model: 'fixture-model', effort: 'low' as const };

// The deadline runs as a real timer racing the poll loop, so a single slow or blocked
// read (lock contention, a stalled `ps` call) still fails at `ms` instead of hanging.
async function until<T>(read: () => Promise<T>, done: (value: T) => boolean, ms = 10000): Promise<T> {
  let value: T | undefined;
  let expired = false;
  let timer!: NodeJS.Timeout;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => { expired = true; reject(new Error(`condition not reached within ${ms}ms: ${JSON.stringify(value)}`)); }, ms);
  });
  const poll = (async () => {
    value = await read();
    while (!done(value) && !expired) { await sleep(50); value = await read(); }
    return value;
  })();
  try { return await Promise.race([poll, deadline]); }
  finally { clearTimeout(timer); }
}
function gone(pid: number) {
  try { process.kill(pid, 0); return false; } catch (error) { return (error as NodeJS.ErrnoException).code === 'ESRCH'; }
}
async function psStat(pid: number) {
  try { return (await exec('/bin/ps', ['-o', 'stat=', '-p', String(pid)])).stdout.trim(); } catch { return ''; }
}
async function supervisorPids(root: string, run: string) {
  const { stdout } = await exec('/bin/ps', ['-A', '-o', 'pid=,command=']);
  return stdout.split('\n').filter(line => line.includes(fixtureScript) && line.includes(` ${root} ${run} `)).map(line => Number(line.trim().split(/\s+/)[0]));
}

async function fixture(t: test.TestContext) {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-supervisor-'));
  const owned: OwnedProcess[] = [];
  t.after(async () => {
    // Test-owned processes only, each proven by recorded identity.
    for (const record of owned) await terminateOwned(record, 0);
    await rm(root, { recursive: true, force: true });
  });
  const source = path.join(root, 'source');
  await mkdir(source);
  await git(source, ['init']);
  await git(source, ['config', 'user.name', 'Fixture']);
  await git(source, ['config', 'user.email', 'fixture@localhost']);
  await writeFile(path.join(source, 'README.md'), 'base\n');
  await git(source, ['add', '.']);
  await git(source, ['commit', '-m', 'Base']);
  const check = { id: 'unit', argv: ['node', '--version'], cwd: '.', timeoutMs: 1000, requirements: ['behavior'], outputPaths: [] };
  const project: Project = { schemaVersion: 2, name: 'fixture', repository: source, base: await git(source, ['rev-parse', 'HEAD']),
    recipient: 'operator', brief: 'Fix the behavior', policies: ['No arbitrary subprocesses'], requirements: ['behavior'],
    checks: [check], artifact: 'result.txt', artifactCheck: { ...check, id: 'artifact' }, allowedPaths: ['src'],
    runtime: { kind: 'macos-sandbox', toolPaths: ['/usr/bin'], network: 'none', authHomes: { codex: null, claude: null } },
    models: { coordinator: model, developer: model, reviewer: model, inspector: model },
    limits: { maxAttempts: 6, maxReworks: 1, attemptTimeoutMs: 60000, maxWallMs: 300000,
      maxReportedTokens: 10000, verificationReserveAttempts: 2, maxLogBytes: 16384 }, billing: 'subscription-only', retentionDays: 30 };
  const dashboard = new Dashboard(path.join(root, 'state'), fetch, { supervisorCommand: (stateRoot, run, launchId) => [process.execPath, fixtureScript, stateRoot, run, launchId] });
  const created = await createRun(dashboard.store, 'run', project, { owner: 'operator', statement: 'Approved brief' });
  const read = () => dashboard.store.read('run');
  const view = async () => (await dashboard.snapshot()).runs[0].factory!;
  // Waits for the planner job to record its process and for its grandchild to report a pid.
  const job = async () => {
    const state = await until(read, s => s.activeJob?.process !== undefined);
    const leader = state.activeJob!.process!;
    const stderr = path.join(dashboard.store.dir('run'), 'attempts', state.activeJob!.id, 'capture', 'stderr.log');
    const grandchild = Number(await until(() => readFile(stderr, 'utf8').catch(() => ''), text => text.length > 0));
    const grandchildIdentity = await identify(grandchild);
    assert.ok(grandchildIdentity);
    assert.equal(grandchildIdentity.pgid, leader.pgid);
    owned.push(leader);
    return { leader, grandchild };
  };
  const supervisor = async () => {
    const record = (await read()).supervisor!.process;
    owned.push(record);
    return record;
  };
  return { dashboard, created, read, view, job, supervisor, supervisorPids: () => supervisorPids(dashboard.store.root, 'run') };
}

for (const command of ['run', 'resume']) test(`CLI ${command} records supervisor lifetime and preserves its JSON output`, { timeout: 10000 }, async t => {
  const f = await fixture(t);
  await f.dashboard.store.transition('run', 'awaiting_input', 'Fixture needs operator input');
  const { stdout, stderr } = await exec(process.execPath, [cliScript, command, f.dashboard.store.root, 'run'], { timeout: 15000 });
  const state = await f.read();
  const events = state.history.filter(event => event.type.startsWith('supervisor_'));
  assert.deepEqual(events.map(event => event.type), ['supervisor_started', 'supervisor_exited']);
  assert.ok(state.supervisor);
  assert.equal(gone(state.supervisor.process.pid), true);
  assert.equal(stderr, '');
  assert.equal(stdout, JSON.stringify({ run: state.id, status: state.status, revision: state.revision - 1,
    attempts: state.attempts.length, candidate: state.candidate ?? null,
    reportedTokens: state.reportedTokens, unknownUsage: state.unknownUsage, reason: state.reason ?? null }) + '\n');
});

for (const command of ['run', 'resume', 'supervise']) test(`CLI ${command} reports supervisor conflicts with exit 3`, { timeout: 10000 }, async t => {
  const f = await fixture(t);
  const self = await identify(process.pid);
  assert.ok(self);
  await f.dashboard.store.update('run', 'fixture_supervisor', {}, state => {
    state.supervisor = { launchId: 'fixture', process: self, launchedAt: state.createdAt };
  });
  const before = await f.read();
  await assert.rejects(exec(process.execPath, [cliScript, command, f.dashboard.store.root, 'run', 'fixture-conflict'], { timeout: 15000 }), error => {
    assert.ok(error instanceof Error && 'code' in error && 'stdout' in error && 'stderr' in error);
    assert.equal(error.code, 3);
    assert.equal(error.stdout, '');
    assert.equal(error.stderr, `Supervisor ${self.pid} already runs run\n`);
    return true;
  });
  assert.deepEqual(await f.read(), before);
});

test('the dashboard starts, pauses, resumes and stops one supervised run', async t => {
  const f = await fixture(t);
  const started = await f.dashboard.control('run', { action: 'start', expectedRevision: f.created.revision });
  assert.equal(started.changed, true);
  assert.equal(started.factory.state, 'running');
  const supervisor = await f.supervisor();
  const { leader, grandchild } = await f.job();
  assert.deepEqual(await f.supervisorPids(), [supervisor.pid]);

  const duplicate = await f.dashboard.control('run', { action: 'start', expectedRevision: (await f.read()).revision });
  assert.equal(duplicate.changed, false);
  await sleep(500);
  assert.deepEqual(await f.supervisorPids(), [supervisor.pid]);

  assert.equal((await f.dashboard.control('run', { action: 'pause' })).changed, true);
  const paused = await until(f.view, v => v.state === 'paused');
  assert.equal(paused.activeJob?.frozen, true);
  assert.equal(paused.startLabel, 'Resume');
  assert.deepEqual([paused.canStart, paused.canPause, paused.canStop], [true, false, true]);
  const frozen = await Promise.all([psStat(leader.pid), psStat(grandchild)]);
  assert.ok(frozen.every(stat => stat.includes('T')));

  const resumed = await f.dashboard.control('run', { action: 'start', expectedRevision: (await f.read()).revision });
  assert.equal(resumed.changed, true);
  await until(f.view, v => v.state === 'running');
  await until(() => Promise.all([psStat(leader.pid), psStat(grandchild)]), stats => stats.every(stat => /^[SR]/.test(stat)));
  assert.equal((await f.read()).suspended, false);

  const stopped = await f.dashboard.control('run', { action: 'stop' });
  assert.equal(stopped.changed, true);
  assert.equal(stopped.factory.state, 'terminal');
  for (const pid of [supervisor.pid, leader.pid, grandchild]) assert.equal(await until(async () => gone(pid), value => value, 3000), true, `pid ${pid} survived`);
  const state = await f.read();
  assert.equal(state.status, 'cancelled');
  assert.equal(state.reason, 'Operator stopped the factory');
  const attempt = state.attempts.find(a => a.id === 'plan-architect');
  assert.equal(attempt?.status, 'failed');
  assert.equal(attempt?.result?.reason, 'cancelled');
  assert.equal(state.attempts.some(a => a.result?.passed), false);
  assert.equal(state.checks.some(c => c.passed), false);
  assert.equal(state.supervisor, undefined);
  assert.equal(state.activeJob, undefined);
  assert.equal(state.control, undefined);
  assert.deepEqual(state.history.filter(e => e.type === 'supervisor_exited').map(e => (e.detail as { outcome: string }).outcome), ['cancelled']);
  assert.equal(await f.dashboard.store.holder(), null);
});

test('Stop releases a non-leader supervisor without killing its unrelated group leader', async t => {
  const f = await fixture(t);
  const groupLeader = spawn(process.execPath, ['-e', `const {spawn}=require('node:child_process'); const child=spawn(process.execPath,['-e','process.stdout.write(String(process.pid));setInterval(()=>{},1000)'],{stdio:['ignore','pipe','ignore']}); child.stdout.pipe(process.stdout); setInterval(()=>{},1000);`], { detached: true, stdio: ['ignore', 'pipe', 'ignore'] });
  assert.ok(groupLeader.pid);
  try {
    const childPid = Number((await once(groupLeader.stdout!, 'data'))[0].toString());
    const nonLeader = await identify(childPid);
    const leader = await identify(groupLeader.pid);
    assert.ok(nonLeader && leader && nonLeader.pid !== nonLeader.pgid);
    await f.dashboard.store.update('run', 'fixture_non_leader_supervisor', {}, state => {
      state.supervisor = { launchId: 'fixture', process: nonLeader, launchedAt: state.createdAt };
    });
    await f.dashboard.store.execution('run', async () => {});
    const stopped = await f.dashboard.control('run', { action: 'stop' });
    assert.equal(stopped.changed, true);
    assert.equal(await f.dashboard.store.holder(), null);
    assert.equal(await until(async () => !gone(leader.pid), value => value), true);
  } finally {
    if (groupLeader.pid) { try { process.kill(-groupLeader.pid, 'SIGKILL'); } catch {} }
  }
});

test('a killed supervisor shows as exited and Stop recovers the orphaned job', async t => {
  const f = await fixture(t);
  await f.dashboard.control('run', { action: 'start', expectedRevision: f.created.revision });
  const supervisor = await f.supervisor();
  const { leader, grandchild } = await f.job();
  process.kill(supervisor.pid, 'SIGKILL');
  const exited = await until(f.view, v => v.state === 'exited');
  assert.equal(exited.reason?.includes('without recording its exit'), true);
  assert.deepEqual([exited.canStart, exited.canPause, exited.canStop], [true, true, true]);
  assert.equal(exited.orphans.length, 0, 'the job leader is alive, so nothing is an orphan');
  assert.equal(gone(leader.pid), false, 'the worker outlives its supervisor until Stop');

  const stopped = await f.dashboard.control('run', { action: 'stop' });
  assert.equal(stopped.changed, true);
  for (const pid of [leader.pid, grandchild]) assert.equal(gone(pid), true, `pid ${pid} survived`);
  const state = await f.read();
  assert.equal(state.status, 'cancelled');
  assert.equal(state.attempts.find(a => a.id === 'plan-architect')?.status, 'interrupted');
  assert.equal(state.unknownUsage, true);
  assert.equal(state.activeJob, undefined);
  assert.equal(state.supervisor, undefined);
  assert.equal(state.history.at(-1)?.type, 'run_cancelled_recovered');
  assert.equal(await f.dashboard.store.holder(), null);
});

test('a paused worker is stopped after its supervisor dies', async t => {
  const f = await fixture(t);
  await f.dashboard.control('run', { action: 'start', expectedRevision: f.created.revision });
  const supervisor = await f.supervisor();
  const { leader, grandchild } = await f.job();
  await f.dashboard.control('run', { action: 'pause' });
  await until(f.view, v => v.state === 'paused');
  process.kill(supervisor.pid, 'SIGKILL');
  const exited = await until(f.view, v => v.state === 'exited');
  assert.equal(exited.activeJob?.frozen, false);
  await f.dashboard.control('run', { action: 'stop' });
  for (const pid of [leader.pid, grandchild]) assert.equal(gone(pid), true, `pid ${pid} survived`);
  assert.equal((await f.read()).status, 'cancelled');
});

test('Start succeeds when the supervisor reaches its waiting point before the first dashboard poll', async t => {
  const f = await fixture(t);
  const waiting = await f.dashboard.store.update('run', 'fixture_waiting', {}, s => { move(s, 'awaiting_input', 'Needs operator input'); });
  const started = await f.dashboard.control('run', { action: 'start', expectedRevision: waiting.revision });
  assert.equal(started.changed, true);
  assert.equal((await f.read()).status, 'awaiting_input');
  await until(f.view, v => v.state === 'idle');
  assert.deepEqual(await f.supervisorPids(), []);
});

test('HTTP run reaches Finished only after supervisor exit and serves its handoff', async t => {
  const f = await fixture(t);
  const server = createDashboardServer(f.dashboard.store.root, f.dashboard);
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise<void>(resolve => server.close(() => resolve())));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  const base = `http://127.0.0.1:${address.port}`;
  const { csrfToken } = await (await fetch(`${base}/api/session`)).json();
  const post = (url: string, body: unknown) => fetch(`${base}${url}`, { method: 'POST', headers: { Origin: base, 'X-CSRF-Token': csrfToken }, body: JSON.stringify(body) });
  const created = await post('/api/runs', { id: 'handoff', project: f.created.project, approval: f.created.approval });
  assert.equal(created.status, 201);
  const verified = await f.dashboard.store.update('handoff', 'fixture_verified', {}, state => {
    state.status = 'verified'; state.candidate = state.sourceBase;
  });
  const started = await post('/api/runs/handoff/control', { action: 'start', expectedRevision: verified.revision });
  assert.equal(started.status, 200);
  const finished = await until(async () => {
    const response = await fetch(`${base}/api/dashboard`);
    return (await response.json()).runs.find((run: { id: string }) => run.id === 'handoff');
  }, run => run?.factory?.state === 'terminal');
  assert.equal(finished.status, 'handoff_ready');
  const state = await f.dashboard.store.read('handoff');
  assert.equal(state.shutdown?.phase, 'complete');
  assert.equal(state.supervisor ? gone(state.supervisor.process.pid) : true, true);
  for (const owned of state.ownedProcesses ?? []) assert.equal(gone(owned.process.pid), true);
  const handoff = await fetch(`${base}/api/runs/handoff/handoff.md`);
  assert.equal(handoff.status, 200);
  assert.match(await handoff.text(), /Status: handoff_ready/);
});

test('worker spawn persists a segment identity before the active job ends', async t => {
  const f = await fixture(t);
  await f.dashboard.control('run', { action: 'start', expectedRevision: f.created.revision });
  const { leader } = await f.job();
  const state = await f.read();
  assert.deepEqual(state.ownedProcesses?.map(record => ({ jobId: record.jobId, segment: record.segment, pid: record.process.pid })),
    [{ jobId: 'plan-architect', segment: 1, pid: leader.pid }]);
  await f.dashboard.control('run', { action: 'stop' });
});

test('Start rejects an orphaned worker group and Stop preserves ownership until every member exits', async t => {
  const f = await fixture(t);
  await f.dashboard.control('run', { action: 'start', expectedRevision: f.created.revision });
  const supervisor = await f.supervisor();
  const { leader, grandchild } = await f.job();
  process.kill(supervisor.pid, 'SIGKILL');
  process.kill(leader.pid, 'SIGKILL');
  await until(async () => gone(leader.pid), value => value);
  const before = await f.read();
  assert.equal((await factoryView(before)).state, 'exited');
  await assert.rejects(f.dashboard.control('run', { action: 'start', expectedRevision: before.revision }), FactoryConflict);
  await assert.rejects(f.dashboard.control('run', { action: 'stop' }), StopIncomplete);
  const stopped = await f.read();
  assert.equal(stopped.status, before.status);
  assert.deepEqual(stopped.activeJob?.process, leader);
  assert.deepEqual(stopped.supervisor?.process, supervisor);
  assert.equal(await f.dashboard.store.holder(), 'run');
  assert.equal(gone(grandchild), false);

  process.kill(grandchild, 'SIGKILL');
  await until(async () => gone(grandchild), value => value);
  const retry = await f.dashboard.control('run', { action: 'stop' });
  assert.equal(retry.factory.state, 'terminal');
  assert.equal((await f.read()).status, 'cancelled');
  assert.equal(await f.dashboard.store.holder(), null);
});

test('start, pause and stop leave a failed run byte for byte unchanged', async t => {
  const f = await fixture(t);
  await f.dashboard.store.update('run', 'fixture_failed', {}, (s: State) => { move(s, 'running', 'fixture'); move(s, 'failed', 'fixture'); });
  const file = path.join(f.dashboard.store.dir('run'), 'state.json');
  const bytes = await readFile(file);
  await assert.rejects(f.dashboard.control('run', { action: 'start', expectedRevision: (await f.read()).revision }), error => error instanceof FactoryConflict && /failed/.test(error.message));
  assert.equal((await f.dashboard.control('run', { action: 'pause' })).changed, false);
  const stop = await f.dashboard.control('run', { action: 'stop' });
  assert.equal(stop.changed, false);
  assert.equal(stop.factory.state, 'terminal');
  assert.deepEqual(await readFile(file), bytes);
  assert.deepEqual(await f.supervisorPids(), []);
});

test('Stop cleans a failed run with live owned processes without changing its terminal status', async t => {
  const f = await fixture(t);
  await f.dashboard.control('run', { action: 'start', expectedRevision: f.created.revision });
  const supervisor = await f.supervisor();
  const { leader, grandchild } = await f.job();
  await f.dashboard.store.update('run', 'fixture_failed', {}, s => { move(s, 'running', 'fixture'); move(s, 'failed', 'fixture'); });
  assert.equal((await f.view()).canStop, true);
  const stopped = await f.dashboard.control('run', { action: 'stop' });
  assert.equal(stopped.changed, true);
  assert.match(stopped.message, /remains failed/);
  for (const pid of [supervisor.pid, leader.pid, grandchild]) assert.equal(gone(pid), true, `pid ${pid} survived`);
  const state = await f.read();
  assert.equal(state.status, 'failed');
  assert.equal(state.supervisor, undefined);
  assert.equal(state.activeJob, undefined);
  assert.equal(await f.dashboard.store.holder(), null);
  assert.equal((await f.view()).canStop, false);
  assert.equal((await f.dashboard.control('run', { action: 'stop' })).changed, false);
});
