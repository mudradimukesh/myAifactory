import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createRun, registerVisualReview, runRun, stepRun } from '../src/coordinator.ts';
import type { Project, WorkerChoice } from '../src/contracts.ts';
import { git } from '../src/git.ts';
import { LocalRuntime } from '../src/runtime.ts';
import type { Job } from '../src/runtime.ts';
import { Store, json, sha } from '../src/store.ts';

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
    proposedReviewerModel?: WorkerChoice;
    override async preflight() { return []; }
    override async execute(job: Job) {
        this.calls.push(job.id);
        if (job.provider === 'codex') assert.equal(job.env?.OPENAI_BASE_URL, 'http://127.0.0.1:8791/v1');
        if (job.provider === 'claude') {
            assert.equal(job.env?.OPENAI_BASE_URL, undefined);
            assert.equal(job.env?.ANTHROPIC_BASE_URL, undefined);
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
    test(`developer allocation counts ${inputTokens} gross input tokens against the reserved share`, async () => {
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
            assert.equal(state.status, inputTokens === 150000 ? 'candidate' : 'changes_requested');
            assert.equal(state.attempts[2].status, inputTokens === 150000 ? 'completed' : 'failed');
            assert.equal(state.reportedTokens, inputTokens + 50);
            assert.equal(state.attempts[2].cachedInputTokens, inputTokens - 1000);
            const handoff = state.attempts[0].handoff!;
            const recorded = JSON.parse(await readFile(path.join(f.store.dir('run'), handoff.path), 'utf8'));
            assert.equal(recorded.tasks[0].limits.maxTokens, 600000);
        } finally { await rm(f.root, { recursive: true, force: true }); }
    });
}

test('planner usage exceeding its reserved share fails without discounting cached input', async () => {
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
        assert.equal(state.status, 'awaiting_input');
        assert.equal(state.attempts[0].status, 'failed');
        assert.equal(state.reportedTokens, 1110);
        assert.equal(state.attempts[0].cachedInputTokens, 1000);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});
