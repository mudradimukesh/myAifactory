import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm, symlink } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import test from 'node:test';
import { FactoryProfiles, FactoryRevisionConflict, runFactory, workerPolicy, skillLibrary } from '../src/factory-profiles.ts';
import { Store, json, sha } from '../src/store.ts';
import { createRun } from '../src/coordinator.ts';
import { git, importCandidate } from '../src/git.ts';
import { Dashboard, factoryView } from '../src/dashboard.ts';
import { createDashboardServer } from '../src/dashboard-server.ts';
import type { Project } from '../src/contracts.ts';

async function fixture() {
    const root = await mkdtemp(path.join(tmpdir(), 'factory-profiles-'));
    const source = path.join(root, 'source'); await mkdir(source);
    await git(source, ['init']); await git(source, ['config', 'user.name', 'Fixture']); await git(source, ['config', 'user.email', 'fixture@localhost']);
    await writeFile(path.join(source, 'README.md'), 'base\n'); await git(source, ['add', '.']); await git(source, ['commit', '-m', 'Base']);
    const base = await git(source, ['rev-parse', 'HEAD']);
    const model = { provider: 'codex', model: 'fixture', effort: 'low' } as const;
    const check = { id: 'unit', argv: ['node', '--version'], cwd: '.', timeoutMs: 1000, requirements: ['behavior'], outputPaths: [] };
    const project: Project = { schemaVersion: 2, name: 'fixture', repository: source, base, recipient: 'operator', brief: 'Fix behavior', policies: ['No arbitrary subprocesses'], requirements: ['behavior'], checks: [check], artifact: 'result.txt', artifactCheck: { ...check, id: 'artifact' }, allowedPaths: ['src'], runtime: { kind: 'macos-sandbox', toolPaths: ['/usr/bin'], network: 'none', authHomes: { codex: null, claude: null } }, models: { coordinator: model, developer: model, reviewer: model, inspector: model }, limits: { maxAttempts: 7, maxReworks: 1, attemptTimeoutMs: 30000, maxWallMs: 300000, maxReportedTokens: 10000, verificationReserveAttempts: 2, maxLogBytes: 16384 }, billing: 'subscription-only', retentionDays: 30 };
    const dashboard = new Dashboard(path.join(root, 'state'));
    return { root, project, dashboard, store: dashboard.store, profiles: new FactoryProfiles(dashboard.store) };
}
const approval = { owner: 'Operator', statement: 'Approved test run' };

test('coordinator maintains factual handoff at creation and factory inherits immutable context only', async () => {
    const f = await fixture();
    try {
        const source = await createRun(f.store, 'handoff-source', f.project, approval);
        const initial = JSON.parse(await readFile(path.join(f.store.dir(source.id), 'handoff.json'), 'utf8'));
        assert.equal(initial.runId, source.id);
        assert.equal(initial.revision, source.revision);
        assert.equal(initial.objective, 'Fix behavior');
        assert.deepEqual(initial.changedFiles, []);
        assert.deepEqual(initial.completed, []);
        const factory = await f.profiles.create({ id: 'successor', name: 'Successor', handoffRunId: source.id });
        assert.equal(factory.inheritedHandoff?.runId, source.id);
        assert.equal(factory.inheritedHandoff?.revision, source.revision);
        assert.equal(factory.inheritedHandoff?.digest, sha(factory.inheritedHandoff!.markdown));
        const next = await createRun(f.store, 'handoff-next', { ...f.project, brief: 'New independent objective', factoryId: factory.id }, approval);
        assert.equal(next.candidate, undefined);
        assert.equal(next.reportedTokens, 0);
        assert.deepEqual(next.checks, []);
        const policyDir = path.join(f.root, 'inherited-policy');
        const prompt = await workerPolicy(f.store, next, 'developer', policyDir);
        assert.ok(prompt.includes(path.join(policyDir, 'context/inherited-handoff.md')));
        assert.equal(await readFile(path.join(policyDir, 'context/inherited-handoff.md'), 'utf8'), factory.inheritedHandoff!.markdown);
        await f.store.update(source.id, 'paused', {}, state => { state.reason = 'Later source change'; });
        assert.equal((await f.profiles.read(factory.id)).inheritedHandoff!.markdown, factory.inheritedHandoff!.markdown);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('handoff reports stalled work and exposes evidence without treating worker prose as completed changes', async () => {
    const f = await fixture(), server = createDashboardServer(f.store.root, f.dashboard);
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    const address = server.address(); assert.ok(address && typeof address !== 'string');
    const base = `http://127.0.0.1:${address.port}`;
    try {
        const state = await createRun(f.store, 'stalled', f.project, approval);
        await f.store.update(state.id, 'job_finished', {}, state => {
            state.status = 'awaiting_input'; state.priorStatus = 'running'; state.reason = 'Worker idle timeout';
            state.attempts.push({ id: 'developer-1', role: 'developer', status: 'failed', candidate: state.sourceBase, startedAt: state.createdAt, model: state.project.models.developer });
        });
        const response = await fetch(base + '/api/runs/stalled/handoff'); assert.equal(response.status, 200);
        const { handoff } = await response.json();
        assert.equal(handoff.revision, 2);
        assert.ok(handoff.blocked.some((item: { text: string }) => item.text.includes('Worker idle timeout')));
        assert.ok(handoff.difficulties.some((item: { text: string; evidence: string[] }) => item.text.includes('failed') && item.evidence.includes('attempts/developer-1')));
        assert.deepEqual(handoff.completed, []);
        assert.match(handoff.markdown, /Historical context|approved task/);
        const download = await fetch(base + '/api/runs/stalled/handoff.md'); assert.equal(download.status, 200);
        assert.equal(await download.text(), handoff.markdown);
    } finally { await new Promise<void>(resolve => server.close(() => resolve())); await rm(f.root, { recursive: true, force: true }); }
});

test('handoff records candidate file changes and active work from persisted evidence', async () => {
    const f = await fixture();
    try {
        const state = await createRun(f.store, 'changes-handoff', f.project, approval);
        await mkdir(path.join(f.project.repository, 'src'));
        await writeFile(path.join(f.project.repository, 'src/behavior.txt'), 'Changed behavior\n');
        await git(f.project.repository, ['add', 'src/behavior.txt']);
        await git(f.project.repository, ['commit', '-m', 'Behavior change']);
        const imported = await importCandidate(path.join(f.store.dir(state.id), 'candidates.git'), f.project.repository, state.sourceBase, ['src']);
        await f.store.update(state.id, 'job_started', {}, state => {
            state.candidate = imported.candidate;
            state.attempts.push({ id: 'tester-1', role: 'tester', status: 'running', candidate: imported.candidate, startedAt: state.updatedAt, model: state.project.models.developer });
        });
        const handoff = await f.dashboard.runHandoff(state.id);
        assert.deepEqual(handoff.changedFiles, ['src/behavior.txt']);
        assert.deepEqual(handoff.current, [{ text: 'tester is working on tester-1.', evidence: ['attempts/tester-1'] }]);
        assert.deepEqual(JSON.parse(await readFile(path.join(f.store.dir(state.id), 'handoff.json'), 'utf8')), handoff);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('handoff credential validation failure leaves the authoritative revision unchanged', async () => {
    const f = await fixture();
    try {
        const state = await createRun(f.store, 'atomic-handoff', f.project, approval);
        await mkdir(path.join(f.store.root, '.dashboard'), { recursive: true, mode: 0o700 });
        await writeFile(path.join(f.store.root, '.dashboard/credentials.json'), '{invalid', { mode: 0o600 });
        await assert.rejects(f.store.update(state.id, 'paused', {}, state => { state.reason = 'Must not commit'; }));
        assert.equal((await f.store.read(state.id)).revision, state.revision);
        assert.equal((await f.store.read(state.id)).reason, state.reason);
        await assert.rejects(createRun(f.store, 'retry-handoff', f.project, approval));
        await writeFile(path.join(f.store.root, '.dashboard/credentials.json'), json({ application: {} }));
        assert.equal((await createRun(f.store, 'retry-handoff', f.project, approval)).revision, 1);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('handoff redacts saved credentials before persistence and inheritance', async () => {
    const f = await fixture();
    try {
        await mkdir(path.join(f.store.root, '.dashboard'), { recursive: true, mode: 0o700 });
        await writeFile(path.join(f.store.root, '.dashboard/credentials.json'), json({ application: { fixture: 'private-fixture-token' } }), { mode: 0o600 });
        const state = await createRun(f.store, 'redacted', { ...f.project, brief: 'Fix private-fixture-token handling' }, approval);
        await f.store.update(state.id, 'failure', {}, state => { state.reason = 'private-fixture-token rejected'; state.status = 'failed'; });
        for (const file of ['handoff.json', 'HANDOFF.md']) {
            const text = await readFile(path.join(f.store.dir(state.id), file), 'utf8');
            assert.ok(!text.includes('private-fixture-token'));
            assert.ok(text.includes('[redacted]'));
        }
        const factory = await f.profiles.create({ id: 'redacted-copy', name: 'Copy', handoffRunId: state.id });
        assert.ok(!factory.inheritedHandoff!.markdown.includes('private-fixture-token'));
        assert.match(factory.inheritedHandoff!.markdown, /\[redacted\] rejected/);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('clarification display accepts admitted fenced JSON and refuses modified evidence', async () => {
    const f = await fixture();
    try {
        const state = await createRun(f.store, 'clarifications', f.project, approval);
        const questions = '```json\n' + JSON.stringify({ schemaVersion: 1, questions: [{ id: 'q1', prompt: 'Which rule?', assumption: 'Use the approved brief' }] }) + '\n```';
        const answers = '```json\n' + JSON.stringify({ schemaVersion: 1, answers: [{ id: 'q1', verdict: 'correct' }] }) + '\n```';
        await writeFile(path.join(f.store.dir(state.id), 'questions.txt'), questions);
        await writeFile(path.join(f.store.dir(state.id), 'answers.txt'), answers);
        state.attempts.push({ id: 'clarify-developer', role: 'developer', status: 'completed', candidate: state.sourceBase, startedAt: state.createdAt, model: state.project.models.developer, handoff: { path: 'questions.txt', sha256: sha(questions) } });
        state.attempts.push({ id: 'answer-architect', role: 'architect', status: 'completed', candidate: state.sourceBase, startedAt: state.createdAt, model: state.project.models.inspector, handoff: { path: 'answers.txt', sha256: sha(answers) } });
        state.clarification = { developer: { round: 1, admitted: true, clarifyAttemptId: 'clarify-developer', answerAttemptId: 'answer-architect' } };
        const view = await factoryView(state, value => value, f.store.dir(state.id));
        assert.deepEqual(view.clarification.developer?.items, [{ id: 'q1', question: 'Which rule?', assumption: 'Use the approved brief', verdict: 'correct', correction: undefined }]);
        await writeFile(path.join(f.store.dir(state.id), 'questions.txt'), questions.replace('Which rule?', 'Tampered rule'));
        assert.deepEqual((await factoryView(state, value => value, f.store.dir(state.id))).clarification.developer?.items, []);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('active job exposes successful skill reads from its snapshot and excludes assigned but unread files', async () => {
    const f = await fixture();
    try {
        const initial = await createRun(f.store, 'activity', f.project, approval);
        const snapshot = (await runFactory(f.store, initial))!;
        const skill = snapshot.agents.find(agent => agent.role === 'developer')!.skills[0];
        const attempt = 'developer-1', root = path.join(f.store.dir('activity'), 'attempts', attempt);
        await f.store.update('activity', 'job_started', {}, state => {
            state.activeJob = { id: attempt, runtime: 'macos-sandbox', kind: 'developer', startedAt: state.createdAt };
            state.attempts.push({ id: attempt, role: 'developer', status: 'running', candidate: state.sourceBase, startedAt: state.createdAt, model: state.project.models.developer });
        });
        const target = path.join(root, 'policy/skills/developer', skill.path);
        await mkdir(path.join(root, 'capture'), { recursive: true });
        await writeFile(path.join(root, 'capture/stdout.log'), JSON.stringify({ type: 'item.completed', item: { type: 'command_execution', command: `cat '${target}'`, exit_code: 1 } }) + '\n');
        assert.deepEqual((await f.dashboard.runSkills('activity')).observed, []);
        await writeFile(path.join(root, 'capture/stdout.log'), JSON.stringify({ type: 'item.completed', item: { type: 'command_execution', command: `cat '${target}'`, exit_code: 0 } }) + '\n');
        assert.deepEqual((await f.dashboard.runSkills('activity')).observed, [{ role: 'developer', skills: [{ name: skill.name, path: skill.path }] }]);
        const view = (await f.dashboard.snapshot()).runs[0];
        assert.ok(view);
        assert.deepEqual(Reflect.get(view.factory!, 'skillReads'), [{ name: skill.name, path: skill.path }]);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('agent edits persist independently across factories and existing run snapshots', async () => {
    const f = await fixture();
    try {
        const first = await f.profiles.create({ id: 'rendering', name: 'Rendering', description: '3D' });
        const second = await f.profiles.create({ id: 'rules', name: 'Rules', description: 'Rule engine' });
        const developer = first.agents.find(agent => agent.role === 'developer')!;
        const common = developer.skills.find(skill => skill.name === 'unslop')!;
        const initial = await createRun(f.store, 'before', { ...f.project, factoryId: first.id }, approval);
        const changed = developer.skills.map(skill => skill.name === common.name ? { ...skill, content: 'Factory-local developer instructions' } : skill);
        await f.profiles.saveAgent(first.id, 'developer', { revision: first.revision, skills: changed });
        const persisted = await new FactoryProfiles(new Store(f.store.root)).read(first.id);
        assert.equal(persisted.agents.find(agent => agent.role === 'developer')!.skills.find(skill => skill.name === common.name)!.content, 'Factory-local developer instructions');
        assert.equal(persisted.agents.find(agent => agent.role === 'reviewer')!.skills.find(skill => skill.name === common.name)!.content, common.content);
        assert.equal((await f.profiles.read(second.id)).agents.find(agent => agent.role === 'developer')!.skills.find(skill => skill.name === common.name)!.content, common.content);
        assert.equal((await skillLibrary()).find(skill => skill.name === common.name)!.content, common.content);
        await assert.rejects(f.profiles.saveAgent(first.id, 'developer', { revision: first.revision, skills: changed }), FactoryRevisionConflict);
        const after = await createRun(f.store, 'after', { ...f.project, factoryId: first.id }, approval);
        assert.notEqual(initial.bundleDigest, after.bundleDigest);
        const beforePolicy = path.join(f.root, 'before-policy'), afterPolicy = path.join(f.root, 'after-policy');
        const prompt = await workerPolicy(f.store, initial, 'developer', beforePolicy);
        await workerPolicy(f.store, after, 'developer', afterPolicy);
        assert.equal(await readFile(path.join(beforePolicy, 'skills/developer', common.path), 'utf8'), common.content);
        assert.equal(await readFile(path.join(afterPolicy, 'skills/developer', common.path), 'utf8'), 'Factory-local developer instructions');
        assert.ok(prompt.includes(path.join(beforePolicy, 'skills/developer', common.path)));
        assert.ok(!prompt.includes(common.content));
        await assert.rejects(createRun(f.store, 'before', f.project, approval), /already exists/);
        assert.equal((await runFactory(f.store, initial))!.revision, 1);
        await writeFile(path.join(f.store.dir('before'), 'skills.json'), json(persisted));
        await assert.rejects(workerPolicy(f.store, initial, 'developer', beforePolicy), /snapshot changed/);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('profile validation rejects traversal, duplicate roles, stale writes and symlink reads', async () => {
    const f = await fixture();
    try {
        const factory = await f.profiles.create({ id: 'test', name: 'Test' });
        const skills = factory.agents[0].skills;
        await assert.rejects(f.profiles.saveAgent('test', 'business', { revision: 1, skills: [{ ...skills[0], path: '../secret' }] }));
        await assert.rejects(f.profiles.saveAgent('test', 'business', { revision: 1, skills: [] }));
        await assert.rejects(f.profiles.saveAgent('test', 'business', { revision: 1, skills: [skills[0], skills[0]] }));
        await symlink(path.join(f.store.root, '.factories/test.json'), path.join(f.store.root, '.factories/alias.json'));
        await assert.rejects(f.profiles.read('alias'), /Symlink/);
    } finally { await rm(f.root, { recursive: true, force: true }); }
});

test('HTTP factory editor persists agent copies, enforces CSRF and creates selected run snapshots', async () => {
    const f = await fixture(), server = createDashboardServer(f.store.root, f.dashboard);
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    const address = server.address(); assert.ok(address && typeof address !== 'string');
    const base = `http://127.0.0.1:${address.port}`;
    try {
        const session = await (await fetch(base + '/api/session')).json();
        const request = (route: string, data: unknown, method = 'POST') => fetch(base + route, { method, headers: { 'content-type': 'application/json', origin: base, 'x-csrf-token': session.csrfToken }, body: JSON.stringify(data) });
        assert.equal((await fetch(base + '/api/factories', { method: 'POST', body: '{}' })).status, 403);
        const created = await request('/api/factories', { id: 'ui-design', name: 'UI design', description: '' }); assert.equal(created.status, 201);
        const { factory } = await created.json();
        const agent = factory.agents.find((agent: { role: string }) => agent.role === 'developer');
        agent.skills[0].content = 'Edited in the browser';
        assert.equal((await request('/api/factories/ui-design/agents/developer', { revision: 1, skills: agent.skills }, 'PUT')).status, 200);
        assert.equal((await request('/api/factories/ui-design/agents/developer', { revision: 1, skills: agent.skills }, 'PUT')).status, 409);
        assert.equal((await request('/api/settings', { ...await f.dashboard.settings(), factoryId: 'ui-design' }, 'PUT')).status, 200);
        await writeFile(path.join(f.project.repository, 'stray.txt'), 'x');
        const dirty = await request('/api/runs', { id: 'dirty', project: f.project, approval });
        assert.equal(dirty.status, 409);
        assert.match((await dirty.json()).message, /uncommitted changes/);
        await rm(path.join(f.project.repository, 'stray.txt'));
        const run = await request('/api/runs', { id: 'from-ui', project: f.project, approval }); assert.equal(run.status, 201);
        const state = await f.store.read('from-ui'); assert.equal(state.project.factoryId, 'ui-design');
        const saved = await (await fetch(base + '/api/runs/from-ui/skills')).json();
        assert.equal(saved.factory.agents.find((agent: { role: string }) => agent.role === 'developer').skills[0].content, 'Edited in the browser');
        assert.deepEqual(saved.observed, []);
        assert.equal((await fetch(base + '/api/skill-library')).status, 200);
        assert.equal((await request('/api/skill-library', {}, 'PUT')).status, 404);
        assert.equal((await fetch(base + '/factories.js')).status, 200);
    } finally { await new Promise<void>(resolve => server.close(() => resolve())); await rm(f.root, { recursive: true, force: true }); }
});
