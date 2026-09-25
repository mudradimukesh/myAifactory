import assert from 'node:assert/strict';
import fsPromises, { appendFile, cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createRun, registerVisualReview, runRun, stepRun } from '../src/coordinator.ts';
import { meterReadingSchema } from '../src/contracts.ts';
import type { Project, WorkerChoice } from '../src/contracts.ts';
import { git } from '../src/git.ts';
import { LocalRuntime, workerHome } from '../src/runtime.ts';
import type { Job } from '../src/runtime.ts';
import { Store, json, sha } from '../src/store.ts';
import { PauseGate } from '../src/process.ts';
import { projectRun } from '../src/dashboard.ts';

const handoffClaim = { schemaVersion: 1, goal: 'Fix the behavior', done: ['Created src/app.txt'],
    remaining: ['Finish and commit'], evidence: ['src/app.txt'], risks: [] };


const model = { provider: 'codex' as const, model: 'fixture-model', effort: 'low' as const };
const task = (role: 'developer' | 'reviewer', model: WorkerChoice) => ({ role, objective: 'Implement and inspect the approved behavior',
    inputs: ['brief'], writableScope: role === 'developer' ? ['src'] : [], expectedOutput: 'Candidate or review', doneWhen: ['Behavior works'],
    model, limits: { maxAttempts: 1, maxSeconds: 30, maxTokens: 1000 } });
const proposal = (role: 'developer' | 'reviewer', selected: WorkerChoice) => JSON.stringify({ schemaVersion: 1, tasks: [task(role, selected)] });
const codexOutput = (text: string, usage = { input_tokens: 10, output_tokens: 10, cached_input_tokens: 0 }) =>
    JSON.stringify({ type: 'item.completed', item: { type: 'agent_message', text } }) + '\n' +
    JSON.stringify({ type: 'turn.completed', usage }) + '\n';

class FakeRuntime extends LocalRuntime {
    readonly calls: string[] = [];
    readonly schemas = new Map<string, string>();
    proposedReviewerModel?: WorkerChoice;
    override async preflight() { return []; }
    override async execute(job: Job): Promise<Awaited<ReturnType<LocalRuntime['execute']>>> {
        this.calls.push(job.id);
        const schemaFlag = job.argv.indexOf('--output-schema');
        if (schemaFlag >= 0) this.schemas.set(job.id, await readFile(job.argv[schemaFlag + 1], 'utf8'));
        if (job.provider === 'codex') assert.equal(job.env?.OPENAI_BASE_URL, 'http://127.0.0.1:8791/v1');
        if (job.provider === 'claude') {
            assert.equal(job.env?.OPENAI_BASE_URL, undefined);
            assert.equal(job.env?.ANTHROPIC_BASE_URL, job.project.headroom?.baseUrl.slice(0, -'/v1'.length));
        }
        const start = new Date().toISOString();
        let text = '';
        if (job.id === 'plan-architect') text = proposal('developer', job.project.models.developer);
        else if (job.id === 'plan-tester') text = proposal('reviewer', this.proposedReviewerModel ?? job.project.models.reviewer);
        else if (job.id.startsWith('developer-')) {
            await mkdir(path.join(job.workspace, 'src'));
            await writeFile(path.join(job.workspace, 'src', 'app.txt'), 'fixed\n');
            await git(job.workspace, ['add', '.']);
            await git(job.workspace, ['commit', '-m', 'Fix approved behavior']);
            text = 'Implemented and committed';
        } else if (job.id.startsWith('review-')) {
            const candidate = await git(job.workspace, ['rev-parse', 'HEAD']);
            text = JSON.stringify({ schemaVersion: 1, candidate, specDigest: sha('Fix the behavior'), requirements: ['behavior'], verdict: 'pass', findings: [] });
        }
        await writeFile(path.join(job.captureDir, 'stdout.log'), job.id.startsWith('check-') ? 'check passed\n' : codexOutput(text));
        await writeFile(path.join(job.captureDir, 'stderr.log'), '');
        return { exitCode: 0, signal: null, reason: 'completed' as const, startedAt: start, endedAt: new Date().toISOString(), pausedMs: 0 };
    }
}

class StallFixtureRuntime extends FakeRuntime {
    readonly startEvent: boolean;
    readonly provider: 'codex' | 'claude';
    readonly growth: 'none' | 'stdout' | 'stderr' | 'rollout';
    readonly ready: Promise<void>;
    readonly aborted: Promise<void>;
    private resolveReady!: () => void;
    private resolveAborted!: () => void;
    constructor(startEvent: boolean, provider: 'codex' | 'claude' = 'codex', growth: 'none' | 'stdout' | 'stderr' | 'rollout' = 'none') {
        super(); this.startEvent = startEvent; this.provider = provider; this.growth = growth;
        this.ready = new Promise(resolve => { this.resolveReady = resolve; });
        this.aborted = new Promise(resolve => { this.resolveAborted = resolve; });
    }
    override async execute(job: Job) {
        this.calls.push(job.id);
        try { await job.onSpawn?.(999999); } catch (error) { assert.ok((error as NodeJS.ErrnoException).code === 'EPERM' || (error as NodeJS.ErrnoException).code === undefined); }
        const start = this.provider === 'codex' ? await readFile(new URL('./fixtures/codex-thread-started.jsonl', import.meta.url), 'utf8') : await readFile(new URL('./fixtures/claude-init.json', import.meta.url), 'utf8');
        await writeFile(path.join(job.captureDir, 'stdout.log'), this.startEvent ? start : 'waiting for additional input\n');
        await writeFile(path.join(job.captureDir, 'stderr.log'), this.growth === 'stderr' ? 'diagnostic\n' : '');
        if (this.growth === 'rollout') await writeRollout(job, await readFile(new URL('./fixtures/codex-token-count.jsonl', import.meta.url), 'utf8'));
        const stopped = new Promise<void>(resolve => { job.limit?.addEventListener('abort', () => resolve(), { once: true }); job.signal.addEventListener('abort', () => resolve(), { once: true }); });
        this.resolveReady();
        await stopped; this.resolveAborted();
        const reason = job.limit?.reason === 'stall_start' || job.limit?.reason === 'stall_idle' ? job.limit.reason : 'cancelled';
        return { exitCode: null, signal: 'SIGTERM', reason, startedAt: new Date().toISOString(), endedAt: new Date().toISOString(), pausedMs: 0 };
    }
}

class HandoffRuntime extends FakeRuntime {
    readonly jobs: Job[] = [];
    readonly tick: () => void;
    readonly resumeExit: number;
    constructor(tick: () => void, resumeExit = 0) { super(); this.tick = tick; this.resumeExit = resumeExit; }
    override async execute(job: Job): Promise<Awaited<ReturnType<LocalRuntime['execute']>>> {
        if (!job.id.startsWith('developer-')) {
            const result = await super.execute(job);
            if (job.id === 'plan-architect') {
                const proposed = task('developer', model);
                proposed.limits.maxTokens = 800000;
                await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput(JSON.stringify({ schemaVersion: 1, tasks: [proposed] })));
            }
            return result;
        }
        this.calls.push(job.id); this.jobs.push(job);
        const startedAt = new Date().toISOString();
        await writeFile(path.join(job.captureDir, 'stderr.log'), '');
        if (this.jobs.length === 1) {
            await mkdir(path.join(job.workspace, 'src'));
            await writeFile(path.join(job.workspace, 'src/app.txt'), 'partial\n');
            await writeRollout(job, await readFile(new URL('./fixtures/codex-token-count.jsonl', import.meta.url), 'utf8'));
            await writeFile(path.join(job.captureDir, 'stdout.log'), await readFile(new URL('./fixtures/codex-thread-started.jsonl', import.meta.url), 'utf8'));
            const stopped = new Promise<void>(resolve => job.limit?.addEventListener('abort', () => resolve(), { once: true }));
            this.tick();
            await stopped;
            assert.equal(job.limit?.reason, 'context_limit');
            return { exitCode: null, signal: 'SIGTERM', reason: 'context_limit', startedAt, endedAt: new Date().toISOString(), pausedMs: 0 };
        }
        if (this.jobs.length === 2) {
            assert.equal(job.argv[job.argv.indexOf('resume') + 1], '00000000-0000-4000-8000-000000000002');
            assert.equal(job.resumeHome, workerHome(this.jobs[0].scratchDir));
            await cp(job.resumeHome, workerHome(job.scratchDir), { recursive: true });
            const next = JSON.parse(await readFile(new URL('./fixtures/codex-token-count.jsonl', import.meta.url), 'utf8'));
            next.payload.info.total_token_usage.input_tokens += 10;
            next.payload.info.total_token_usage.output_tokens += 10;
            next.payload.info.total_token_usage.total_tokens += 20;
            await appendFile(path.join(workerHome(job.scratchDir), '.codex/sessions/rollout-test.jsonl'), JSON.stringify(next) + '\n');
            await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput(JSON.stringify(handoffClaim)));
            this.tick();
            return { exitCode: this.resumeExit, signal: null, reason: this.resumeExit ? 'exit_error' : 'completed',
                startedAt, endedAt: new Date().toISOString(), pausedMs: 0 };
        }
        assert.equal(job.resumeHome, undefined);
        assert.equal(job.argv.includes('resume'), false);
        assert.ok(job.stdin?.includes(JSON.stringify(handoffClaim)));
        assert.equal(await readFile(path.join(job.workspace, 'src/app.txt'), 'utf8'), 'partial\n');
        await writeFile(path.join(job.workspace, 'src/app.txt'), 'fixed\n');
        await git(job.workspace, ['add', '.']);
        await git(job.workspace, ['commit', '-m', 'Finish handoff fixture']);
        await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput('Implemented and committed'));
        this.tick();
        return { exitCode: 0, signal: null, reason: 'completed', startedAt, endedAt: new Date().toISOString(), pausedMs: 0 };
    }
}

async function fixture(visual = false) {
    const root = await mkdtemp(path.join(tmpdir(), 'factory-coordinator-'));
    const source = path.join(root, 'source');
    await mkdir(source);
    await git(source, ['init']);
    await git(source, ['config', 'user.name', 'Fixture']);
    await git(source, ['config', 'user.email', 'fixture@localhost']);
    await writeFile(path.join(source, 'README.md'), 'base\n');
    await git(source, ['add', '.']);
    await git(source, ['commit', '-m', 'Base']);
    const base = await git(source, ['rev-parse', 'HEAD']);
    const check = { id: 'unit', argv: ['node', '--version'], cwd: '.', timeoutMs: 1000, requirements: ['behavior'], outputPaths: [] };
    const project: Project = { schemaVersion: 2, name: 'fixture', repository: source, base,
        recipient: 'operator', brief: 'Fix the behavior', policies: ['No arbitrary subprocesses'], requirements: ['behavior'],
        checks: [check], artifact: 'result.txt', artifactCheck: { ...check, id: 'artifact' }, allowedPaths: ['src'],
        ...(visual ? { visualReview: { caseIds: ['case-1'] } } : {}),
        runtime: { kind: 'macos-sandbox', toolPaths: ['/usr/bin'], network: 'none', authHomes: { codex: null, claude: null } },
        models: { coordinator: model, developer: model, reviewer: model, inspector: model },
        limits: { maxAttempts: 6, maxReworks: 1, attemptTimeoutMs: 30000, maxWallMs: 300000,
            maxReportedTokens: 10000, verificationReserveAttempts: 2, maxLogBytes: 16384 }, billing: 'subscription-only', retentionDays: 30 };
    return { root, project, store: new Store(path.join(root, 'state')) };
}

async function runStall(t: { mock: { timers: { enable: Function; tick: Function; reset: Function } } }, mode: StallFixtureRuntime, pause?: PauseGate) {
    const f = await fixture();
    f.project.limits.workerStartTimeoutMs = 2000; f.project.limits.workerIdleTimeoutMs = 2000;
    await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
    const pending = pause ? stepRun(f.store, 'run', mode, { signal: new AbortController().signal, pause }) : stepRun(f.store, 'run', mode);
    await mode.ready;
    t.mock.timers.tick(2000);
    await waitForMeter(f, meter => meter.providerStartedAt === null);
    await Promise.race([mode.aborted, new Promise((_, reject) => setTimeout(() => reject(Error('stall test abort timeout')), 1000))]);
    return { f, state: await pending };
}

async function waitForMeter(f: Awaited<ReturnType<typeof fixture>>, predicate: (meter: { providerStartedAt?: string | null; at?: string }) => boolean) {
    const file = path.join(f.store.dir('run'), 'attempts', 'plan-architect', 'segment-1', 'meter.json');
    const deadline = performance.now() + 1000;
    for (;;) {
        try {
            const meter = JSON.parse(await readFile(file, 'utf8')) as { providerStartedAt?: string | null; at?: string };
            if (predicate(meter)) return meter;
        } catch { /* poll has not written the sidecar yet */ }
        if (performance.now() >= deadline) throw Error(`meter state wait timed out: ${await readFile(file, 'utf8').catch(() => 'no meter')}`);
        await new Promise(resolve => setImmediate(resolve));
    }
}

test('stall_start rejects non-start capture growth and accepts a start on the threshold poll', { timeout: 10000 }, async t => {
    t.mock.timers.enable({ apis: ['setInterval', 'Date'], now: Date.now() });
    try {
        const { f, state } = await runStall(t, new StallFixtureRuntime(false));
        assert.equal(state.attempts[0].segments?.[0].reason, 'stall_start');
        assert.equal((state.history.find(event => event.type === 'job_finished')?.detail as { reason?: string } | undefined)?.reason, 'stall_start');
        await rm(f.root, { recursive: true, force: true });
    } finally { t.mock.timers.reset(); }
});

for (const provider of ['codex', 'claude'] as const)
test(`stall_idle after ${provider} start with no new activity`, { timeout: 10000 }, async t => {
    t.mock.timers.enable({ apis: ['setInterval', 'Date'], now: Date.now() });
    try {
        const f = await fixture();
        if (provider === 'claude') f.project.models.inspector = { provider, model: 'claude-sonnet-5', effort: 'low' };
        f.project.limits.workerIdleTimeoutMs = 4000;
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        const runtime = new StallFixtureRuntime(true, provider);
        const pending = stepRun(f.store, 'run', runtime);
        await runtime.ready;
        t.mock.timers.tick(2000);
        const startedMs = Date.parse((await waitForMeter(f, meter => meter.providerStartedAt !== null)).providerStartedAt!);
        // A poll still in flight skips the next interval, so step until the abort instead of counting polls.
        let aborted = false;
        runtime.aborted.then(() => { aborted = true; });
        for (let step = 0; !aborted && step < 10; step++) {
            t.mock.timers.tick(2000);
            const settle = performance.now() + 50;
            while (!aborted && performance.now() < settle) await new Promise(resolve => setImmediate(resolve));
            if (aborted) assert.ok(Date.now() - startedMs >= 4000, 'stall_idle fired before the idle limit');
        }
        assert.ok(aborted, 'stall_idle never fired');
        const state = await pending;
        assert.equal(state.attempts[0].segments?.[0].reason, 'stall_idle');
        await rm(f.root, { recursive: true, force: true });
    } finally { t.mock.timers.reset(); }
});

test('pause before start and after start consumes no stall allowance', { timeout: 10000 }, async t => {
    t.mock.timers.enable({ apis: ['setInterval', 'Date'], now: Date.now() });
    try {
        const gate = new PauseGate(); gate.pause();
        const f = await fixture(); f.project.limits.workerStartTimeoutMs = 2000; f.project.limits.workerIdleTimeoutMs = 2000;
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        const abort = new AbortController(); const runtime = new StallFixtureRuntime(true);
        const pending = stepRun(f.store, 'run', runtime, { signal: abort.signal, pause: gate });
        await runtime.ready; t.mock.timers.tick(10000); await new Promise(resolve => setImmediate(resolve));
        assert.equal(abort.signal.aborted, false);
        gate.resume();
        t.mock.timers.tick(2000); await new Promise(resolve => setImmediate(resolve));
        gate.pause(); t.mock.timers.tick(10000); await new Promise(resolve => setImmediate(resolve));
        assert.equal(abort.signal.aborted, false);
        gate.resume(); abort.abort(); const state = await pending;
        assert.equal(state.attempts[0].segments?.[0].reason, 'cancelled');
        assert.equal(state.attempts[0].result?.reason, 'cancelled');
        await rm(f.root, { recursive: true, force: true });
    } finally { t.mock.timers.reset(); }
});

test('stall reason persists through meter, history, result, segment and reopened dashboard row', { timeout: 10000 }, async t => {
    t.mock.timers.enable({ apis: ['setInterval', 'Date'], now: Date.now() });
    try {
        const { f, state } = await runStall(t, new StallFixtureRuntime(false));
        const reopened = await f.store.read('run');
        const attempt = reopened.attempts[0];
        assert.equal(attempt.result?.reason, 'stall_start'); assert.equal(attempt.segments?.[0].reason, 'stall_start');
        assert.equal((reopened.history.find(event => event.type === 'job_finished')?.detail as { reason?: string } | undefined)?.reason, 'stall_start');
        const meter = JSON.parse(await readFile(path.join(f.store.dir('run'), 'attempts', attempt.id, 'segment-1', 'meter.json'), 'utf8'));
        assert.equal(meter.reason, 'stall_start');
        const row = projectRun(reopened).usageByRole[0].agents[0]; assert.equal(row.reason, 'stall_start'); assert.equal(row.status, 'failed'); assert.equal(row.live, false);
        assert.equal(state.status, 'awaiting_input');
        await rm(f.root, { recursive: true, force: true });
    } finally { t.mock.timers.reset(); }
});

for (const claudePlanner of [false, true]) test(`an older run without Headroom cannot admit Codex with ${claudePlanner ? 'a Claude planner' : 'all Codex roles'}`, async () => {
    const f = await fixture();
    try {
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        await f.store.update('run', 'legacy_profile_fixture', {}, state => {
            delete state.project.headroom;
            if (claudePlanner) state.project.models.inspector = { provider: 'claude', model: 'fixture-claude', effort: 'low' };
            state.profileDigest = sha(json(state.project));
        });
        const before = await f.store.read('run');
        const runtime = new FakeRuntime();
        await assert.rejects(stepRun(f.store, 'run', runtime), /older run has no Headroom route/);
        assert.deepEqual(runtime.calls, []);
        assert.deepEqual(await f.store.read('run'), before);
        assert.equal(before.attempts.length, 0);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('dispatches bounded planner tasks and accepts only candidate-bound runner checks', async () => {
    const f = await fixture();
    try {
        const created = await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        assert.equal(created.project.headroom?.baseUrl, 'http://127.0.0.1:8791/v1');
        const runtime = new FakeRuntime();
        const final = await runRun(f.store, 'run', runtime);
        assert.equal(final.status, 'handoff_ready');
        assert.deepEqual(runtime.calls, ['plan-architect', 'plan-tester', 'developer-1', 'review-1', 'check-unit-1', 'check-artifact-1']);
        assert.deepEqual([...runtime.schemas.keys()], ['plan-architect', 'plan-tester', 'review-1']);
        assert.deepEqual(JSON.parse(runtime.schemas.get('plan-architect')!).required, ['schemaVersion', 'tasks']);
        assert.ok(JSON.parse(runtime.schemas.get('review-1')!).required.includes('verdict'));
        assert.doesNotMatch(runtime.schemas.get('plan-architect')!, /minLength|minItems|maximum|\$schema/);
        assert.equal(final.attempts.length, 4);
        assert.equal(final.checks.length, 2);
        assert.equal(final.reportedTokens, 80);
        assert.equal(final.review?.record.candidate, final.candidate);
        assert.equal(await git(path.join(f.store.dir('run'), 'candidates.git'), ['rev-parse', `refs/candidates/${final.candidate}`]), final.candidate);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('requires exact visual evidence and an explicit owner answer before handoff', async () => {
    const f = await fixture(true);
    try {
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        const runtime = new FakeRuntime();
        const waiting = await runRun(f.store, 'run', runtime);
        assert.equal(waiting.status, 'awaiting_input');
        assert.equal(waiting.priorStatus, 'verified');
        const captures = path.join(f.root, 'captures');
        await mkdir(captures);
        const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg==', 'base64');
        await writeFile(path.join(captures, 'case-1.png'), png);
        const manifest = { schemaVersion: 1, candidate: waiting.candidate!, specDigest: waiting.specDigest,
            images: [{ id: 'case-1', path: 'case-1.png', sha256: sha(png), mimeType: 'image/png' }] };
        await assert.rejects(registerVisualReview(f.store, 'run', captures, { ...manifest, images: [] }));
        const registered = await registerVisualReview(f.store, 'run', captures, manifest);
        assert.equal(registered.questionBatches?.[0]?.questions[0]?.visualEvidence?.length, 1);
        assert.equal((await runRun(f.store, 'run', runtime)).status, 'awaiting_input');
        await f.store.update('run', 'question_answered', {}, state => {
            state.questionBatches![0]!.answer = { owner: 'operator', source: 'operator', answeredAt: new Date().toISOString(),
                answers: [{ questionId: 'approve-renders', value: 'approve' }] };
        });
        const approved = await runRun(f.store, 'run', runtime);
        assert.equal(approved.status, 'handoff_ready');
        assert.equal(approved.candidate, manifest.candidate);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('unknown worker usage persists as a failed attempt and stops dispatch', async () => {
    const f = await fixture();
    try {
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        class UnknownRuntime extends FakeRuntime {
            override async execute(job: Job) {
                const result = await super.execute(job);
                const file = path.join(job.captureDir, 'stdout.log');
                await writeFile(file, (await readFile(file, 'utf8')).replace(/,"usage":\{[^}]+\}/, ''));
                return result;
            }
        }
        const runtime = new UnknownRuntime();
        const state = await stepRun(f.store, 'run', runtime);
        assert.equal(state.status, 'awaiting_input');
        assert.equal(state.unknownUsage, true);
        assert.equal(state.attempts.length, 1);
        assert.equal(state.attempts[0]?.status, 'failed');
        const meter = meterReadingSchema.parse(JSON.parse(await readFile(path.join(f.store.dir('run'), 'attempts', 'plan-architect', 'segment-1', 'meter.json'), 'utf8')));
        assert.equal(meter.metered, null);
        assert.equal(meter.usage, null);
        assert.equal(meter.raw, null);
        assert.equal(state.attempts[0].segments?.[0].metered, null);
        assert.deepEqual(runtime.calls, ['plan-architect']);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

for (const proposed of ['configured', 'provider', 'model', 'effort'] as const) {
    test(`planner admission preserves configured Claude choice: ${proposed}`, async () => {
        const f = await fixture();
        try {
            const selected: WorkerChoice = { provider: 'claude', model: 'sonnet', effort: 'medium' };
            f.project.models.reviewer = selected;
            await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
            const runtime = new FakeRuntime();
            runtime.proposedReviewerModel = proposed === 'provider' ? { ...selected, provider: 'codex' }
                : proposed === 'model' ? { ...selected, model: 'opus' }
                : proposed === 'effort' ? { ...selected, effort: 'high' } : selected;
            assert.equal((await stepRun(f.store, 'run', runtime)).status, 'ready');
            const planner = await stepRun(f.store, 'run', runtime);
            assert.equal(planner.status, proposed === 'configured' ? 'ready' : 'awaiting_input');
            if (proposed !== 'configured')
                assert.match(JSON.stringify(planner.lastEvent), /Proposed worker model differs from the approved profile/);
            assert.deepEqual(runtime.calls, ['plan-architect', 'plan-tester']);
        } finally { await rm(f.root, { recursive: true, force: true }); }
    });
}

test('createRun rejects a Claude developer', async () => {
    const f = await fixture();
    try {
        f.project.models.developer = { provider: 'claude', model: 'sonnet', effort: 'medium' };
        await assert.rejects(createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' }), /requires Codex for implementation/);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

for (const inputTokens of [150000, 350000]) {
    test(`developer allocation meters ${inputTokens} cached input tokens against the reserved share`, async () => {
        const f = await fixture();
        try {
            f.project.limits.maxReportedTokens = 900000;
            await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
            class BudgetRuntime extends FakeRuntime {
                override async execute(job: Job) {
                    const result = await super.execute(job);
                    if (job.id.startsWith('plan-')) {
                        const role = job.id === 'plan-architect' ? 'developer' : 'reviewer';
                        const proposed = task(role, model);
                        proposed.limits.maxTokens = 600000;
                        await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput(JSON.stringify({ schemaVersion: 1, tasks: [proposed] })));
                    } else if (job.id.startsWith('developer-')) {
                        await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput('Implemented and committed', {
                            input_tokens: inputTokens, cached_input_tokens: inputTokens - 1000, output_tokens: 10,
                        }));
                    }
                    return result;
                }
            }
            const runtime = new BudgetRuntime();
            assert.equal((await stepRun(f.store, 'run', runtime)).status, 'ready');
            assert.equal((await stepRun(f.store, 'run', runtime)).status, 'ready');
            const state = await stepRun(f.store, 'run', runtime);
            const cached = inputTokens - 1000;
            const metered = (inputTokens - cached) + 10 + Math.ceil(cached * 0.1);
            assert.equal(state.status, 'candidate');
            assert.equal(state.attempts[2].status, 'completed');
            assert.equal(state.reportedTokens, 40 + metered);
            assert.equal(state.attempts[2].cachedInputTokens, cached);
            const handoff = state.attempts[0].handoff!;
            const recorded = JSON.parse(await readFile(path.join(f.store.dir('run'), handoff.path), 'utf8'));
            assert.equal(recorded.tasks[0].limits.maxTokens, 600000);
        } finally { await rm(f.root, { recursive: true, force: true }); }
    });
}

test('an uncached developer overrun is metered gross and fails with token_limit', async () => {
    const f = await fixture();
    try {
        f.project.limits.maxReportedTokens = 900000;
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        class BudgetRuntime extends FakeRuntime {
            override async execute(job: Job) {
                const result = await super.execute(job);
                if (job.id.startsWith('plan-')) {
                    const role = job.id === 'plan-architect' ? 'developer' : 'reviewer';
                    const proposed = task(role, model);
                    proposed.limits.maxTokens = 600000;
                    await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput(JSON.stringify({ schemaVersion: 1, tasks: [proposed] })));
                } else if (job.id.startsWith('developer-')) {
                    await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput('Implemented and committed', {
                        input_tokens: 700000, cached_input_tokens: 0, output_tokens: 10,
                    }));
                }
                return result;
            }
        }
        const runtime = new BudgetRuntime();
        assert.equal((await stepRun(f.store, 'run', runtime)).status, 'ready');
        assert.equal((await stepRun(f.store, 'run', runtime)).status, 'ready');
        const state = await stepRun(f.store, 'run', runtime);
        assert.equal(state.status, 'changes_requested');
        assert.equal(state.attempts[2].status, 'failed');
        assert.match(state.reason ?? '', /token_limit/);
        assert.ok(state.history.some(event => event.type === 'job_finished' && (event.detail as { reason?: string }).reason === 'token_limit'));
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('planner usage is metered with cache reads at one tenth', async () => {
    const f = await fixture();
    try {
        f.project.limits.maxReportedTokens = 3000;
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        class PlannerBudgetRuntime extends FakeRuntime {
            override async execute(job: Job) {
                const result = await super.execute(job);
                await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput(proposal('developer', model), {
                    input_tokens: 1100, cached_input_tokens: 1000, output_tokens: 10,
                }));
                return result;
            }
        }
        const state = await stepRun(f.store, 'run', new PlannerBudgetRuntime());
        assert.equal(state.attempts[0].status, 'completed');
        assert.equal(state.reportedTokens, 210);
        assert.equal(state.attempts[0].cachedInputTokens, 1000);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('planner usage over its metered share fails', async () => {
    const f = await fixture();
    try {
        f.project.limits.maxReportedTokens = 3000;
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        class PlannerBudgetRuntime extends FakeRuntime {
            override async execute(job: Job) {
                const result = await super.execute(job);
                await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput(proposal('developer', model), {
                    input_tokens: 1100, cached_input_tokens: 0, output_tokens: 10,
                }));
                return result;
            }
        }
        const state = await stepRun(f.store, 'run', new PlannerBudgetRuntime());
        assert.equal(state.status, 'awaiting_input');
        assert.equal(state.attempts[0].status, 'failed');
        assert.equal(state.reportedTokens, 1110);
        assert.equal(state.attempts[0].cachedInputTokens, 0);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('a normal completion still writes a final meter.json with source final', async () => {
    const f = await fixture();
    try {
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        const runtime = new FakeRuntime();
        const final = await runRun(f.store, 'run', runtime);
        assert.equal(final.status, 'handoff_ready');
        const meterPath = path.join(f.store.dir('run'), 'attempts', 'developer-1', 'segment-1', 'meter.json');
        const meterRecord = JSON.parse(await readFile(meterPath, 'utf8'));
        assert.equal(meterRecord.source, 'final');
        assert.deepEqual(meterRecord.raw, { input_tokens: 10, cached_input_tokens: 0, output_tokens: 10 });
        const segment = final.attempts.find(a => a.id === 'developer-1')?.segments?.[0];
        assert.ok(segment);
        assert.equal(segment?.source, 'final');
        assert.deepEqual(meterRecord.raw, segment?.raw);
        assert.equal(meterRecord.metered, segment?.metered);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('a developer over its token allowance is stopped with token_limit and the meter file matches the segment', async () => {
    const f = await fixture();
    try {
        f.project.limits.maxReportedTokens = 900000;
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        class MeterKillRuntime extends FakeRuntime {
            override async execute(job: Job) {
                if (!job.id.startsWith('developer-')) return super.execute(job);
                this.calls.push(job.id);
                const start = new Date().toISOString();
                const sessionsDir = path.join(workerHome(job.scratchDir), '.codex', 'sessions');
                await mkdir(sessionsDir, { recursive: true });
                const rollout = JSON.stringify({ type: 'event_msg', payload: { type: 'token_count', info: {
                    total_token_usage: { input_tokens: 5000, cached_input_tokens: 0, output_tokens: 0 },
                    last_token_usage: { input_tokens: 5000, output_tokens: 0 }, model_context_window: 258400,
                } } });
                await writeFile(path.join(sessionsDir, 'rollout-1.jsonl'), rollout + '\n');
                await writeFile(path.join(job.captureDir, 'stdout.log'),
                    JSON.stringify({ type: 'item.completed', item: { type: 'agent_message', text: 'partial' } }) + '\n');
                await writeFile(path.join(job.captureDir, 'stderr.log'), '');
                await new Promise<void>(resolve => job.limit?.addEventListener('abort', () => resolve(), { once: true }));
                const reason = (typeof job.limit?.reason === 'string' ? job.limit.reason : 'token_limit') as 'token_limit';
                return { exitCode: null, signal: 'SIGTERM', reason, startedAt: start, endedAt: new Date().toISOString(), pausedMs: 0 };
            }
        }
        class BudgetRuntime extends MeterKillRuntime {
            override async execute(job: Job) {
                const result = await super.execute(job);
                if (job.id.startsWith('plan-')) {
                    const role = job.id === 'plan-architect' ? 'developer' : 'reviewer';
                    const proposed = task(role, model);
                    proposed.limits.maxTokens = 1000;
                    await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput(JSON.stringify({ schemaVersion: 1, tasks: [proposed] })));
                }
                return result;
            }
        }
        const budgetRuntime = new BudgetRuntime();
        assert.equal((await stepRun(f.store, 'run', budgetRuntime)).status, 'ready');
        assert.equal((await stepRun(f.store, 'run', budgetRuntime)).status, 'ready');
        const state = await stepRun(f.store, 'run', budgetRuntime);
        assert.equal(state.status, 'changes_requested');
        const attempt = state.attempts[2];
        assert.equal(attempt.status, 'failed');
        assert.equal(attempt.result?.reason, 'token_limit');
        assert.equal(state.unknownUsage, false);
        const segment = attempt.segments?.[0];
        assert.ok(segment);
        assert.equal(segment?.source, 'meter');
        assert.deepEqual(segment?.raw, { input_tokens: 5000, cached_input_tokens: 0, output_tokens: 0 });
        assert.equal(segment?.metered, 5000);
        assert.equal(segment?.peakContext, 5000);
        assert.equal(state.reportedTokens, 40 + 5000);
        const meterPath = path.join(f.store.dir('run'), 'attempts', 'developer-1', 'segment-1', 'meter.json');
        const meterRecord = JSON.parse(await readFile(meterPath, 'utf8'));
        assert.equal(meterRecord.source, 'meter');
        assert.deepEqual(meterRecord.raw, segment?.raw);
        assert.equal(meterRecord.metered, segment?.metered);
        assert.equal(meterRecord.contextTokens, segment?.peakContext);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});


const rolloutLine = (context: number, total = 100) => JSON.stringify({ type: 'event_msg', payload: { type: 'token_count', info: {
    total_token_usage: { input_tokens: total, cached_input_tokens: 0, output_tokens: 0 },
    last_token_usage: { input_tokens: context, output_tokens: 0 }, model_context_window: 258400,
} } }) + '\n';
async function writeRollout(job: Job, text: string) {
    const dir = path.join(workerHome(job.scratchDir), '.codex', 'sessions');
    await mkdir(dir, { recursive: true });
    await writeFile(path.join(dir, 'rollout-test.jsonl'), text);
}

test('a context_limit crossing produces one attempt with three segments and a stored handoff', async t => {
    const f = await fixture();
    t.mock.timers.enable({ apis: ['setInterval', 'Date'], now: Date.now() });
    try {
        f.project.limits.maxReportedTokens = 6000000;
        f.project.limits.handoffContextRatio = 0.4;
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        const runtime = new HandoffRuntime(() => t.mock.timers.tick(2000));
        await stepRun(f.store, 'run', runtime);
        await stepRun(f.store, 'run', runtime);
        const state = await stepRun(f.store, 'run', runtime);
        assert.equal(state.status, 'candidate');
        const attempts = state.attempts.filter(a => a.role === 'developer');
        assert.equal(attempts.length, 1);
        const attempt = attempts[0];
        assert.equal(attempt.status, 'completed');
        assert.deepEqual(attempt.segments?.map(s => [s.index, s.kind, s.reason]),
            [[1, 'work', 'context_limit'], [2, 'handoff', 'completed'], [3, 'work', 'completed']]);
        const root = path.join(f.store.dir('run'), 'attempts', 'developer-1');
        assert.deepEqual(JSON.parse(await readFile(path.join(root, 'handoff-1.json'), 'utf8')), handoffClaim);
        assert.deepEqual(runtime.jobs.map(j => j.workspace), [path.join(root, 'source'), path.join(root, 'source'), path.join(root, 'source')]);
        assert.deepEqual(runtime.jobs.map(j => j.timeoutMs), [30000, 28000, 26000]);
        assert.deepEqual(runtime.jobs.map(j => j.captureDir), [path.join(root, 'capture'), path.join(root, 'segment-2/capture'), path.join(root, 'segment-3/capture')]);
        assert.equal(attempt.inputTokens, 4148653);
        assert.equal(attempt.cachedInputTokens, 3927168);
        assert.equal(attempt.outputTokens, 15123);
        assert.equal(state.reportedTokens, 629365);
        assert.equal(state.elapsedMs, 6000);
        assert.equal(state.unknownUsage, false);
        assert.equal(state.reworks, 0);
        for (const [index, spentBefore] of [[1, 0], [2, 629285], [3, 629305]]) {
            const meter = meterReadingSchema.parse(JSON.parse(await readFile(path.join(root, `segment-${index}/meter.json`), 'utf8')));
            assert.equal(meter.spentBefore, spentBefore);
            assert.equal(meter.allowance, 800000);
            if (index === 2) {
                assert.equal(meter.peakContext, 122449);
                assert.equal(meter.trigger, 103360);
                assert.equal(meter.metered, 20, 'the copied work usage must not be charged again');
            }
        }
        assert.equal(await git(path.join(f.store.dir('run'), 'candidates.git'), ['show', `${state.candidate}:src/app.txt`]), 'fixed');
    } finally { t.mock.timers.reset(); await rm(f.root, { recursive: true, force: true }); }
});

test('a non-zero resume exit fails the attempt with handoff_failed', async t => {
    const f = await fixture();
    t.mock.timers.enable({ apis: ['setInterval', 'Date'], now: Date.now() });
    try {
        f.project.limits.maxReportedTokens = 6000000;
        f.project.limits.handoffContextRatio = 0.4;
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        const runtime = new HandoffRuntime(() => t.mock.timers.tick(2000), 1);
        await stepRun(f.store, 'run', runtime); await stepRun(f.store, 'run', runtime);
        const state = await stepRun(f.store, 'run', runtime);
        const attempt = state.attempts[2];
        assert.equal(attempt.result?.reason, 'handoff_failed');
        assert.equal(attempt.status, 'failed');
        assert.equal(attempt.segments?.length, 2);
        assert.equal(runtime.jobs.length, 2);
        assert.equal(state.candidate, undefined);
        assert.equal(state.reportedTokens, 629345);
        assert.match(await readFile(path.join(f.store.dir('run'), attempt.handoff!.path), 'utf8'), /resume.*exit.*1/i);
    } finally { t.mock.timers.reset(); await rm(f.root, { recursive: true, force: true }); }
});

test('Claude contextWindowMismatch is set only when the reported window differs', async () => {
    for (const window of [200000, 250000]) {
        const f = await fixture();
        try {
            f.project.models.inspector = { provider: 'claude', model: 'claude-sonnet-5', effort: 'low' };
            f.project.limits.maxReportedTokens = 6000000;
            await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
            class ClaudeResultRuntime extends FakeRuntime {
                override async execute(job: Job) {
                    const result = await super.execute(job);
                    const fixture = JSON.parse(await readFile(new URL('./fixtures/claude-result.json', import.meta.url), 'utf8'));
                    fixture.modelUsage['claude-sonnet-5'].contextWindow = window;
                    fixture.result = proposal('developer', model);
                    await writeFile(path.join(job.captureDir, 'stdout.log'), JSON.stringify(fixture));
                    return result;
                }
            }
            const state = await stepRun(f.store, 'run', new ClaudeResultRuntime());
            assert.equal(state.attempts[0].segments?.[0].contextWindowMismatch, window === 200000 ? undefined : true);
        } finally { await rm(f.root, { recursive: true, force: true }); }
    }
});

for (const failure of ['invalid', 'absent', 'token_limit', 'compacted', 'timeout'] as const)
test(`a handoff ${failure} stops the chain before fresh work`, async t => {
    const f = await fixture();
    t.mock.timers.enable({ apis: ['setInterval', 'Date'], now: Date.now() });
    try {
        f.project.limits.maxReportedTokens = 6000000;
        f.project.limits.handoffContextRatio = 0.4;
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        class FailedHandoffRuntime extends HandoffRuntime {
            override async execute(job: Job) {
                const result = await super.execute(job);
                if (job.argv.includes('resume')) {
                    if (failure === 'invalid') await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput('{"schemaVersion":99}'));
                    if (failure === 'absent') await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput(''));
                    if (failure === 'token_limit') await writeFile(path.join(job.captureDir, 'stdout.log'),
                        codexOutput(JSON.stringify(handoffClaim), { input_tokens: 200000, cached_input_tokens: 0, output_tokens: 10 }));
                    if (failure === 'compacted') await appendFile(path.join(workerHome(job.scratchDir), '.codex/sessions/rollout-test.jsonl'), '{"type":"compacted"}\n');
                }
                return result;
            }
        }
        const runtime = new FailedHandoffRuntime(() => t.mock.timers.tick(failure === 'timeout' ? 16000 : 2000));
        await stepRun(f.store, 'run', runtime); await stepRun(f.store, 'run', runtime);
        const state = await stepRun(f.store, 'run', runtime);
        const attempt = state.attempts[2];
        assert.equal(attempt.result?.reason, failure === 'invalid' || failure === 'absent' ? 'handoff_failed' : failure);
        assert.equal(attempt.status, 'failed');
        assert.equal(runtime.jobs.length, 2);
        assert.equal(attempt.segments?.length, 2);
        assert.equal(state.candidate, undefined);
    } finally { t.mock.timers.reset(); await rm(f.root, { recursive: true, force: true }); }
});

for (const reason of ['timeout', 'cancelled'] as const) test(`${reason} without final usage preserves an unknown lower bound`, async () => {
    const f = await fixture();
    try {
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        class InterruptedRuntime extends FakeRuntime {
            override async execute(job: Job) {
                const result = await super.execute(job);
                await writeRollout(job, rolloutLine(100));
                await writeFile(path.join(job.captureDir, 'stdout.log'), '');
                return { ...result, exitCode: null, reason };
            }
        }
        const runtime = new InterruptedRuntime();
        const state = await stepRun(f.store, 'run', runtime);
        assert.equal(state.unknownUsage, true);
        assert.equal(state.reportedTokens, 100);
        assert.equal(state.attempts[0].segments?.[0].metered, 100);
        assert.equal(state.attempts[0].result?.reason, reason);
        assert.deepEqual(state.history.find(e => e.type === 'job_finished')?.detail, { jobId: 'plan-architect', passed: false, reason });
        await stepRun(f.store, 'run', runtime);
        assert.deepEqual(runtime.calls, ['plan-architect']);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

for (const reason of ['compacted', 'context_limit', 'token_limit'] as const) test(`final polling preserves ${reason} on the segment`, async () => {
    const f = await fixture();
    try {
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        class FinalLimitRuntime extends FakeRuntime {
            override async execute(job: Job) {
                const result = await super.execute(job);
                await writeRollout(job, rolloutLine(reason === 'context_limit' ? 160000 : 100) + rolloutLine(1000) +
                    (reason === 'compacted' ? JSON.stringify({ type: 'compacted' }) + '\n' : ''));
                if (reason === 'token_limit') await writeFile(path.join(job.captureDir, 'stdout.log'),
                    codexOutput(proposal('developer', job.project.models.developer), { input_tokens: 5000, cached_input_tokens: 0, output_tokens: 0 }));
                return result;
            }
        }
        const state = await stepRun(f.store, 'run', new FinalLimitRuntime());
        const attempt = state.attempts[0];
        assert.equal(attempt.status, 'failed');
        const attemptReason = reason === 'context_limit' ? 'handoff_failed' : reason;
        assert.equal(attempt.result?.reason, attemptReason);
        assert.equal(attempt.segments?.[0].reason, reason);
        assert.deepEqual(state.history.find(e => e.type === 'job_finished')?.detail, { jobId: 'plan-architect', passed: false, reason: attemptReason });
        if (reason === 'context_limit') {
            assert.equal(attempt.segments?.length, 1, 'a missing session id must not launch a replacement');
            assert.match(await readFile(path.join(f.store.dir('run'), attempt.handoff!.path), 'utf8'), /session id is missing/);
            assert.equal(attempt.segments?.[0].peakContext, 160000);
            const meter = meterReadingSchema.parse(JSON.parse(await readFile(path.join(f.store.dir('run'), 'attempts', 'plan-architect', 'segment-1', 'meter.json'), 'utf8')));
            assert.equal(meter.contextTokens, 1000);
            assert.equal(meter.peakContext, 160000);
        }
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('unknown Claude model is refused before recording or executing an attempt', async () => {
    const f = await fixture();
    try {
        f.project.models.inspector = { provider: 'claude', model: 'claude-unknown-test', effort: 'low' };
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        const runtime = new FakeRuntime();
        await assert.rejects(stepRun(f.store, 'run', runtime), /No verified context window/);
        assert.deepEqual(runtime.calls, []);
        assert.deepEqual((await f.store.read('run')).attempts, []);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('Claude context crossing stops a live segment and retains its limit reason', async () => {
    const f = await fixture();
    try {
        f.project.models.inspector = { provider: 'claude', model: 'claude-sonnet-5', effort: 'low' };
        f.project.limits.maxReportedTokens = 1000000;
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        class ClaudeRuntime extends FakeRuntime {
            override async execute(job: Job) {
                const start = new Date().toISOString();
                await writeFile(path.join(job.captureDir, 'stderr.log'), '');
                await writeFile(path.join(job.captureDir, 'stdout.log'), JSON.stringify({ type: 'assistant', message: { id: 'one', usage: {
                    input_tokens: 10000, cache_creation_input_tokens: 10000, cache_read_input_tokens: 110000, output_tokens: 100,
                } } }) + '\n');
                await new Promise<void>(resolve => {
                    const timer = setTimeout(resolve, 3500);
                    job.limit?.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
                });
                assert.equal(job.limit?.reason, 'context_limit');
                return { exitCode: null, signal: 'SIGTERM', reason: 'context_limit' as const, startedAt: start, endedAt: new Date().toISOString(), pausedMs: 0 };
            }
        }
        const state = await stepRun(f.store, 'run', new ClaudeRuntime());
        assert.equal(state.unknownUsage, false);
        assert.equal(state.attempts[0].segments?.[0].peakContext, 130100);
        assert.equal(state.attempts[0].segments?.[0].reason, 'context_limit');
        assert.equal(state.attempts[0].result?.reason, 'handoff_failed');
        assert.deepEqual(state.history.find(e => e.type === 'job_finished')?.detail, { jobId: 'plan-architect', passed: false, reason: 'handoff_failed' });
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('runSegment waits for an in-flight poll before finalizing the sidecar', async t => {
    const f = await fixture();
    const originalOpen = fsPromises.open;
    let release!: () => void, entered!: () => void;
    const blocked = new Promise<void>(resolve => { release = resolve; });
    const enteredPoll = new Promise<void>(resolve => { entered = resolve; });
    let delayed = false;
    t.mock.method(fsPromises, 'open', async (...args: Parameters<typeof originalOpen>) => {
        const handle = await originalOpen(...args);
        if (!delayed && String(args[0]).includes('segment-1/meter.json.') && args[1] === 'wx') {
            delayed = true;
            const sync = handle.sync.bind(handle);
            t.mock.method(handle, 'sync', async () => { entered(); await blocked; await sync(); });
        }
        return handle;
    });
    syncBuiltinESMExports();
    let pending: ReturnType<typeof stepRun> | undefined;
    try {
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        let exited!: () => void;
        const runtimeExited = new Promise<void>(resolve => { exited = resolve; });
        class RaceRuntime extends FakeRuntime {
            override async execute(job: Job) {
                const result = await super.execute(job);
                await writeRollout(job, rolloutLine(100));
                await enteredPoll;
                await writeRollout(job, rolloutLine(100) + rolloutLine(200, 200));
                await writeFile(path.join(job.captureDir, 'stdout.log'), codexOutput(proposal('developer', job.project.models.developer),
                    { input_tokens: 200, cached_input_tokens: 0, output_tokens: 0 }));
                exited();
                return result;
            }
        }
        let finished = false;
        pending = stepRun(f.store, 'run', new RaceRuntime()).then(state => { finished = true; return state; });
        await runtimeExited;
        await new Promise(resolve => setTimeout(resolve, 100));
        assert.equal(finished, false, 'finalization must wait for the existing poll');
        release();
        const state = await pending;
        const meter = meterReadingSchema.parse(JSON.parse(await readFile(path.join(f.store.dir('run'), 'attempts', 'plan-architect', 'segment-1', 'meter.json'), 'utf8')));
        assert.equal(meter.source, 'final');
        assert.equal(meter.metered, 200);
        assert.equal(meter.metered, state.attempts[0].segments?.[0].metered);
    } finally {
        release();
        await pending;
        t.mock.restoreAll(); syncBuiltinESMExports();
        await rm(f.root, { recursive: true, force: true });
    }
});

for (const failure of ['write', 'usage'] as const) test(`a meter ${failure} failure cancels the worker and blocks dispatch`, async t => {
    const f = await fixture();
    const originalOpen = fsPromises.open;
    if (failure === 'write') t.mock.method(fsPromises, 'open', async (...args: Parameters<typeof originalOpen>) => {
        if (String(args[0]).includes('segment-1/meter.json.')) throw Error('fixture meter write failure');
        return originalOpen(...args);
    });
    syncBuiltinESMExports();
    try {
        await createRun(f.store, 'run', f.project, { owner: 'operator', statement: 'Approved brief' });
        let cancelled = false;
        class BrokenMeterRuntime extends FakeRuntime {
            override async execute(job: Job) {
                const result = await super.execute(job);
                await writeRollout(job, failure === 'usage' ? rolloutLine(100).replace(',"cached_input_tokens":0', '') : rolloutLine(100));
                await new Promise<void>(resolve => {
                    const timer = setTimeout(resolve, 3500);
                    job.signal.addEventListener('abort', () => { cancelled = true; clearTimeout(timer); resolve(); }, { once: true });
                });
                return { ...result, reason: 'cancelled' as const };
            }
        }
        const runtime = new BrokenMeterRuntime();
        await assert.rejects(stepRun(f.store, 'run', runtime));
        assert.equal(cancelled, true);
        const state = await f.store.read('run');
        assert.equal(state.unknownUsage, true);
        assert.equal(state.attempts[0].status, 'interrupted');
        await stepRun(f.store, 'run', runtime);
        assert.deepEqual(runtime.calls, ['plan-architect']);
    } finally {
        t.mock.restoreAll(); syncBuiltinESMExports();
        await rm(f.root, { recursive: true, force: true });
    }
});
