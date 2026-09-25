import { mkdir, readFile, rm } from 'node:fs/promises';
import path from 'node:path';
import { z } from 'zod';
import { bundle } from './bundle.ts';
import { check as checkSchema, choice, id, projectSchema, reviewSchema } from './contracts.ts';
import type { Attempt, Check, Project, Result, Role, State, WorkerChoice } from './contracts.ts';
import { checkout, cleanRepository, createStore, importCandidate, treeDigest } from './git.ts';
import { PauseGate, groupStopped, identify, isOwnedAlive } from './process.ts';
import { LocalRuntime } from './runtime.ts';
import { Store, durable, immutable, json, move, record, sha, verifyFile, within } from './store.ts';
import { parseWorkerOutput, workerCommand } from './workers.ts';

const proposalSchema = z.object({
    schemaVersion: z.literal(1),
    tasks: z.array(z.object({
        role: z.enum(['developer', 'tester', 'reviewer']), objective: z.string().min(1),
        inputs: z.array(z.string()), writableScope: z.array(z.string()),
        expectedOutput: z.string().min(1), doneWhen: z.array(z.string()).min(1),
        model: choice,
        limits: z.object({ maxAttempts: z.literal(1), maxSeconds: z.number().int().positive().max(1800), maxTokens: z.number().int().safe().positive() }).strict(),
    }).strict()).min(1).max(2),
}).strict();
const visualManifestSchema = z.object({ schemaVersion: z.literal(1), candidate: z.string().regex(/^[a-f0-9]{40}$/),
    specDigest: z.string().regex(/^[a-f0-9]{64}$/),
    images: z.array(z.object({ id, path: z.string().min(1), sha256: z.string().regex(/^[a-f0-9]{64}$/),
        mimeType: z.enum(['image/png', 'image/jpeg']) }).strict()).min(1).max(200),
}).strict();
export type VisualManifest = z.infer<typeof visualManifestSchema>;
type Proposal = z.infer<typeof proposalSchema>;
type JobOutcome = { attempt: Attempt; text: string; passed: boolean; result: Result };
/** Operator controls for one supervised run. Abort means stop permanently; the gate freezes running work. */
export type RunControl = { signal: AbortSignal; pause: PauseGate };
export class RunCancelled extends Error {}
export class SupervisorConflict extends Error {}
const terminal = (status: State['status']) => status === 'failed' || status === 'cancelled' || status === 'handoff_ready';
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
const now = () => new Date().toISOString();
const candidateStore = (store: Store, run: string) => path.join(store.dir(run), 'candidates.git');
const evidenceDir = (store: Store, run: string, job: string) => path.join(store.dir(run), 'attempts', job);
const used = (s: State) => s.attempts.length;
const remaining = (s: State) => s.project.limits.maxAttempts - used(s);
const choiceFor = (s: State, role: Role): WorkerChoice => role === 'developer' ? s.project.models.developer : role === 'reviewer' ? s.project.models.reviewer : s.project.models.inspector;
const visualBatchId = (candidate: string) => `visual-${candidate.slice(0, 12)}`;
const visualQuestionId = 'approve-renders';

export async function registerVisualReview(store: Store, run: string, sourceRoot: string, value: unknown): Promise<State> {
    const s = await store.read(run);
    if (!s.project.visualReview || !s.candidate || !(['verified', 'awaiting_input'] as string[]).includes(s.status) ||
        (s.status === 'awaiting_input' && s.priorStatus !== 'verified')) throw Error('Run is not waiting for verified visual evidence');
    const manifest = visualManifestSchema.parse(value);
    if (manifest.candidate !== s.candidate || manifest.specDigest !== s.specDigest) throw Error('Visual manifest identity is stale');
    const expected = s.project.visualReview.caseIds;
    if (new Set(expected).size !== expected.length || manifest.images.length !== expected.length ||
        new Set(manifest.images.map(image => image.id)).size !== expected.length ||
        manifest.images.some(image => !expected.includes(image.id))) throw Error('Visual manifest does not cover every approved case exactly once');
    const batchId = visualBatchId(s.candidate);
    const entries = manifest.images.map(image => ({ ...image, sourceId: image.id, label: `Case ${image.id}`,
        candidate: s.candidate!, specDigest: s.specDigest }));
    const imported = await store.importVisualEvidence(run, batchId, visualQuestionId, entries, sourceRoot);
    const location = `visual/${batchId}/manifest.json`;
    const bytes = json(manifest);
    try { await immutable(path.join(store.dir(run), location), bytes); }
    catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
        if (await readFile(path.join(store.dir(run), location), 'utf8') !== bytes) throw Error('Visual manifest was already registered with different evidence');
    }
    const file = await record(store.dir(run), location);
    return store.update(run, 'visual_review_registered', { batchId, candidate: s.candidate }, state => {
        if (state.revision !== s.revision || state.candidate !== s.candidate || state.specDigest !== s.specDigest)
            throw Error('Run changed while visual evidence was imported');
        if (state.questionBatches?.some(batch => batch.id === batchId)) throw Error('Visual review already registered');
        state.artifact = file;
        state.questionBatches ??= [];
        state.questionBatches.push({ id: batchId, title: `Review ${imported.length} rendered cases`, createdAt: now(), questions: [{
            id: visualQuestionId, prompt: `Inspect all ${imported.length} rendered cases for candidate ${s.candidate} and approve or request changes.`,
            owner: state.approval.owner, impact: 'Visual acceptance and handoff', recommendation: 'Approve only after inspecting every render',
            options: ['approve', 'request_changes'], affectedTicketIds: [], visualEvidence: imported,
        }] });
    });
}

export async function ensureVisualApproval(store: Store, run: string): Promise<'pending' | 'approved' | 'rejected'> {
    const s = await store.read(run);
    if (!s.project.visualReview) return 'approved';
    if (!s.candidate) throw Error('Missing candidate for visual review');
    const batchId = visualBatchId(s.candidate);
    const batch = s.questionBatches?.find(value => value.id === batchId);
    if (!batch || !s.artifact) return 'pending';
    await verifyFile(store.dir(run), s.artifact);
    const stored = visualManifestSchema.parse(JSON.parse(await readFile(await within(store.dir(run), s.artifact.path), 'utf8')));
    if (stored.candidate !== s.candidate || stored.specDigest !== s.specDigest) throw Error('Registered visual manifest is stale');
    const images = batch.questions.find(question => question.id === visualQuestionId)?.visualEvidence;
    if (!images || images.length !== s.project.visualReview.caseIds.length || stored.images.length !== images.length ||
        images.some(image => !s.project.visualReview!.caseIds.includes(image.id) || image.candidate !== s.candidate || image.specDigest !== s.specDigest ||
            !stored.images.some(original => original.id === image.id && original.sha256 === image.sha256 && original.mimeType === image.mimeType)))
        throw Error('Visual approval lacks exact case coverage');
    for (const image of images) await store.readVisualEvidence(run, batchId, visualQuestionId, image);
    if (!batch.answer) return 'pending';
    if (batch.answer.owner !== s.approval.owner) throw Error('Visual answer owner does not match approval owner');
    const answer = batch.answer.answers.find(item => item.questionId === visualQuestionId)?.value;
    return answer === 'approve' ? 'approved' : answer === 'request_changes' ? 'rejected' : 'pending';
}

function tokenAllowance(s: State, role: Role): number {
    const remainingTokens = s.project.limits.maxReportedTokens - s.reportedTokens;
    const reserve = role === 'architect' || role === 'tester' || role === 'developer' ? s.project.limits.verificationReserveAttempts : 0;
    return Math.floor(remainingTokens / (reserve + 1));
}

function budget(s: State, role: Role) {
    if (s.control || s.suspended || s.unknownUsage) throw Error('Run control or usage is unresolved');
    if (s.elapsedMs >= s.project.limits.maxWallMs || s.reportedTokens >= s.project.limits.maxReportedTokens) throw Error('Run budget exhausted');
    const reserve = role === 'architect' || role === 'tester' || role === 'developer' ? s.project.limits.verificationReserveAttempts : 0;
    if (remaining(s) <= reserve) throw Error('Verification attempt reserve reached');
}

function parseJsonText(text: string): unknown {
    const trimmed = text.trim().replace(/^```(?:json)?\s*/, '').replace(/\s*```$/, '');
    return JSON.parse(trimmed);
}

function admitProposal(value: unknown, s: State, source: 'architect' | 'tester'): Proposal {
    const proposal = proposalSchema.parse(value);
    const allowedRoles = source === 'architect' ? new Set(['developer']) : new Set(['tester', 'reviewer']);
    for (const task of proposal.tasks) {
        if (!allowedRoles.has(task.role)) throw Error(`${source} proposed an unsupported ${task.role} task`);
        const pinned = choiceFor(s, task.role);
        if (JSON.stringify(task.model) !== JSON.stringify(pinned)) throw Error('Proposed worker model differs from the approved profile');
        if (task.limits.maxSeconds * 1000 > s.project.limits.attemptTimeoutMs || task.limits.maxTokens > s.project.limits.maxReportedTokens)
            throw Error('Proposed task exceeds approved limits');
        task.limits.maxTokens = Math.min(task.limits.maxTokens, tokenAllowance(s, task.role));
        if (task.limits.maxTokens < 1) throw Error('No task token allowance remains after verification reserve');
        if (task.writableScope.some(scope => !s.project.allowedPaths.some(allowed => allowed === '.' || scope === allowed || scope.startsWith(allowed + '/'))))
            throw Error('Proposed writable scope exceeds approved paths');
    }
    if (source === 'architect' && proposal.tasks.length !== 1) throw Error('The architect must propose exactly one developer task');
    if (source === 'tester' && (proposal.tasks.length !== 1 || proposal.tasks[0].role !== 'reviewer'))
        throw Error('The first delivery supports one read-only review task from the test planner');
    return proposal;
}

async function loadedProposal(store: Store, s: State, source: 'architect' | 'tester'): Promise<Proposal> {
    const attempt = s.attempts.find(a => a.role === source && a.status === 'completed');
    if (!attempt?.handoff) throw Error(`Missing ${source} proposal`);
    return admitProposal(parseJsonText(await readFile(await within(store.dir(s.id), attempt.handoff.path), 'utf8')), s, source);
}

function prompt(s: State, task: string) {
    return `Approved brief:\n${s.project.brief}\n\nRequirements: ${s.project.requirements.join(', ')}\nPolicies:\n${s.project.policies.join('\n')}\n\nCandidate base: ${s.candidate ?? s.sourceBase}\n\n${task}`;
}

function plannerPrompt(s: State, role: 'developer' | 'reviewer') {
    const selected = choiceFor(s, role);
    const maxTokens = tokenAllowance(s, role);
    if (maxTokens < 1) throw Error('No task token allowance remains after verification reserve');
    const maxSeconds = Math.min(1800, Math.floor((s.project.limits.maxWallMs - s.elapsedMs) / 1000), Math.floor(s.project.limits.attemptTimeoutMs / 1000));
    const example = { schemaVersion: 1, tasks: [{ role, objective: 'One bounded task grounded in the approved requirements',
        inputs: ['approved brief', 'candidate source'], writableScope: role === 'developer' ? s.project.allowedPaths : [],
        expectedOutput: role === 'developer' ? 'Clean candidate commit' : 'Independent JSON review', doneWhen: ['Named approved behavior has evidence'],
        model: selected, limits: { maxAttempts: 1, maxSeconds, maxTokens } }] };
    return `Return only JSON with this exact shape and field names. Replace the objective, inputs, expectedOutput, and doneWhen with a concrete bounded task. Keep role, model, limits, and writableScope exactly as supplied. No nested delegation or subprocess spawning.\n${JSON.stringify(example)}`;
}

async function roleJob(store: Store, s: State, runtime: LocalRuntime, role: Role, jobId: string, objective: string, source: string, readOnlySource: boolean,
    limits: { maxSeconds: number; maxTokens: number } | undefined, control: RunControl | undefined): Promise<JobOutcome> {
    if (control?.signal.aborted) throw new RunCancelled();
    budget(s, role);
    const maxTokens = Math.min(tokenAllowance(s, role), limits?.maxTokens ?? Infinity);
    if (maxTokens < 1) throw Error('No task token allowance remains after verification reserve');
    const run = s.id, root = evidenceDir(store, run, jobId), workspace = path.join(root, 'source');
    const policyDir = path.join(root, 'policy'), outputDir = path.join(root, 'proposed'), captureDir = path.join(root, 'capture'), scratchDir = path.join(root, 'scratch');
    // A job refused before job_started (for example by a pause) leaves an unrecorded checkout. It holds no evidence.
    if (!s.attempts.some(a => a.id === jobId)) await rm(root, { recursive: true, force: true });
    for (const dir of [policyDir, outputDir, captureDir, scratchDir]) await mkdir(dir, { recursive: true, mode: 0o700 });
    await checkout(candidateStore(store, run), workspace, source);
    const b = await bundle();
    const policy = b.content[role];
    const worker = workerCommand(choiceFor(s, role), role, workspace, prompt(s, objective), policy, s.project.headroom);
    const startedAt = now();
    await store.update(run, 'job_started', { jobId, role }, state => {
        budget(state, role);
        state.activeJob = { id: jobId, runtime: 'macos-sandbox', kind: role, startedAt };
        state.attempts.push({ id: jobId, role, startedAt, model: choiceFor(state, role), candidate: source, status: 'running' });
    });
    const sourceBefore = readOnlySource ? await treeDigest(workspace) : null;
    let completed;
    try {
        completed = await runtime.execute({ id: jobId, project: s.project, workspace, policyDir, outputDir, captureDir, scratchDir,
            argv: [worker.executable, ...worker.args], stdin: worker.stdin, env: worker.env, provider: choiceFor(s, role).provider,
            readOnlySource, network: s.project.runtime.network,
            timeoutMs: Math.min(s.project.limits.attemptTimeoutMs, s.project.limits.maxWallMs - s.elapsedMs, limits ? limits.maxSeconds * 1000 : Infinity),
            maxLogBytes: s.project.limits.maxLogBytes, signal: control?.signal ?? new AbortController().signal, pause: control?.pause,
            onSpawn: pid => recordProcess(store, run, jobId, pid) });
    } catch (error) {
        await store.update(run, 'job_launch_uncertain', { jobId, error: String(error) }, state => {
            const attempt = state.attempts.find(a => a.id === jobId)!;
            attempt.endedAt = now(); attempt.status = 'interrupted';
            state.activeJob = undefined; state.unknownUsage = true;
        });
        throw error;
    }
    const stdout = await readFile(path.join(captureDir, 'stdout.log'), 'utf8');
    const parsed = parseWorkerOutput(choiceFor(s, role).provider, stdout);
    const sourceUnchanged = sourceBefore !== null && sourceBefore === await treeDigest(workspace);
    const totalTokens = parsed.inputTokens !== null && parsed.outputTokens !== null ? parsed.inputTokens + parsed.outputTokens : null;
    const passed = completed.reason === 'completed' && completed.exitCode === 0 && !parsed.failed && totalTokens !== null &&
        totalTokens <= maxTokens && (!readOnlySource || sourceUnchanged);
    const current = await store.read(run);
    const result: Result = { id: jobId, kind: 'worker', candidate: source, specDigest: current.specDigest,
        definitionDigest: sha(json({ role, objective, model: choiceFor(s, role) })), runtime: runtime.identity,
        startedAt: completed.startedAt, endedAt: completed.endedAt, exitCode: completed.exitCode, signal: completed.signal,
        reason: completed.reason, stdout: await record(store.dir(run), path.relative(store.dir(run), path.join(captureDir, 'stdout.log'))),
        stderr: await record(store.dir(run), path.relative(store.dir(run), path.join(captureDir, 'stderr.log'))),
        passed: passed && sourceUnchanged, sourceUnchanged,
        argv: [worker.executable, ...worker.args], cwd: workspace };
    // The untrusted text is retained as a claim. Only runner checks establish acceptance.
    const handoffPath = path.join(root, 'handoff.json');
    await mkdir(root, { recursive: true });
    await durable(handoffPath, parsed.text);
    const handoff = await record(store.dir(run), path.relative(store.dir(run), handoffPath));
    const latest = await store.update(run, 'job_finished', { jobId, passed, reason: parsed.reason ?? completed.reason }, state => {
        const attempt = state.attempts.find(a => a.id === jobId)!;
        attempt.endedAt = completed.endedAt; attempt.status = passed ? 'completed' : 'failed'; attempt.result = result;
        attempt.inputTokens = parsed.inputTokens; attempt.outputTokens = parsed.outputTokens; attempt.cachedInputTokens = parsed.cachedInputTokens;
        attempt.handoff = handoff; state.activeJob = undefined;
        if (parsed.inputTokens === null || parsed.outputTokens === null) state.unknownUsage = true;
        else state.reportedTokens += parsed.inputTokens + parsed.outputTokens;
        state.elapsedMs += runningMs(completed);
    });
    if (completed.reason === 'cancelled') throw new RunCancelled();
    return { attempt: latest.attempts.find(a => a.id === jobId)!, text: parsed.text, passed, result };
}

async function checkJob(store: Store, s: State, runtime: LocalRuntime, definition: Check, jobId: string, control: RunControl | undefined): Promise<Result> {
    if (control?.signal.aborted) throw new RunCancelled();
    if (!s.candidate) throw Error('Missing candidate');
    if (s.control || s.suspended || s.unknownUsage || s.elapsedMs >= s.project.limits.maxWallMs)
        throw Error('Run control, usage, or wall budget blocks checks');
    const root = evidenceDir(store, s.id, jobId), workspace = path.join(root, 'build');
    const policyDir = path.join(root, 'policy'), outputDir = path.join(root, 'proposed'), captureDir = path.join(root, 'capture'), scratchDir = path.join(root, 'scratch');
    if (!s.checks.some(r => r.stdout.path === path.relative(store.dir(s.id), path.join(captureDir, 'stdout.log')))) await rm(root, { recursive: true, force: true });
    for (const dir of [policyDir, outputDir, captureDir, scratchDir]) await mkdir(dir, { recursive: true, mode: 0o700 });
    await checkout(candidateStore(store, s.id), workspace, s.candidate);
    await store.update(s.id, 'check_started', { jobId, check: definition.id }, state => { state.activeJob = { id: jobId, runtime: 'macos-sandbox', kind: 'check', startedAt: now() }; });
    const sourceBefore = await treeDigest(workspace, definition.outputPaths);
    const command = definition.argv;
    const cwd = path.resolve(workspace, definition.cwd);
    if (cwd !== workspace && !cwd.startsWith(workspace + path.sep)) throw Error('Check cwd escapes candidate');
    const completed = await runtime.execute({ id: jobId, project: s.project, workspace: cwd, policyDir, outputDir, captureDir, scratchDir,
        argv: command, env: { FACTORY_OUTPUT_DIR: outputDir }, readOnlySource: false, network: s.project.runtime.network,
        timeoutMs: Math.min(definition.timeoutMs, s.project.limits.attemptTimeoutMs, s.project.limits.maxWallMs - s.elapsedMs),
        maxLogBytes: s.project.limits.maxLogBytes, signal: control?.signal ?? new AbortController().signal, pause: control?.pause,
        onSpawn: pid => recordProcess(store, s.id, jobId, pid) });
    const sourceUnchanged = sourceBefore === await treeDigest(workspace, definition.outputPaths);
    const result: Result = { id: definition.id, kind: definition.id === s.project.artifactCheck.id ? 'artifact' : 'check', candidate: s.candidate,
        specDigest: s.specDigest, definitionDigest: sha(json(definition)), runtime: runtime.identity,
        startedAt: completed.startedAt, endedAt: completed.endedAt, exitCode: completed.exitCode, signal: completed.signal,
        reason: completed.reason, stdout: await record(store.dir(s.id), path.relative(store.dir(s.id), path.join(captureDir, 'stdout.log'))),
        stderr: await record(store.dir(s.id), path.relative(store.dir(s.id), path.join(captureDir, 'stderr.log'))),
        passed: completed.reason === 'completed' && completed.exitCode === 0 && sourceUnchanged, sourceUnchanged, argv: command, cwd };
    await store.update(s.id, 'check_finished', { jobId, check: definition.id, passed: result.passed }, state => {
        state.activeJob = undefined; state.checks.push(result);
        state.elapsedMs += runningMs(completed);
    });
    if (completed.reason === 'cancelled') throw new RunCancelled();
    return result;
}

const runningMs = (completed: { startedAt: string; endedAt: string; pausedMs: number }) =>
    Math.max(0, Date.parse(completed.endedAt) - Date.parse(completed.startedAt) - completed.pausedMs);

/** Records the worker group before its timeout is armed, so a stop or restart can prove ownership. */
async function recordProcess(store: Store, run: string, jobId: string, pid: number) {
    const owned = await identify(pid);
    // The leader already exited; runProcess kills the rest of its group, so nothing outlives the record.
    if (!owned) return;
    await store.update(run, 'job_process_recorded', { jobId, pid: owned.pid }, state => {
        if (state.activeJob?.id !== jobId) throw Error('Recorded process belongs to no active job');
        state.activeJob.process = owned;
    });
}

function cancelRun(store: Store, run: string) {
    return store.update(run, 'run_cancelled', {}, state => {
        state.control = undefined;
        state.suspended = false;
        if (!terminal(state.status)) move(state, 'cancelled', 'Operator stopped the factory');
    });
}

export async function createRun(store: Store, run: string, input: unknown, approval: { owner: string; statement: string }): Promise<State> {
    id.parse(run);
    const project = projectSchema.parse(input);
    project.headroom ??= { baseUrl: 'http://127.0.0.1:8791/v1' };
    if (!approval.owner.trim() || !approval.statement.trim()) throw Error('Named approval is required');
    await cleanRepository(project.repository, project.base);
    const b = await bundle();
    const at = now();
    const event = { sequence: 1, at, type: 'created', detail: { owner: approval.owner } };
    const s: State = { schemaVersion: 1, id: run, revision: 1, status: 'ready', project,
        specDigest: sha(project.brief), profileDigest: sha(json(project)), bundleDigest: b.digest, policyDigest: sha(json(project.policies)),
        sourceBase: project.base, createdAt: at, updatedAt: at, approval, attempts: [], checks: [], reworks: 0,
        reportedTokens: 0, unknownUsage: false, elapsedMs: 0, suspended: false, lastEvent: event, history: [event] };
    await createStore(project.repository, candidateStore(store, run), project.base);
    await store.create(s);
    return s;
}

async function stepUnlocked(store: Store, run: string, runtime: LocalRuntime, control: RunControl | undefined): Promise<State> {
    let s = await store.read(run);
    await store.reconcile(run, s);
    if ((s.control === 'cancel' || control?.signal.aborted) && !terminal(s.status)) return cancelRun(store, run);
    if (s.activeJob) return store.transition(run, 'awaiting_input', `Uncertain prior job ${s.activeJob.id}; reconcile its process before resume`);
    if (s.control || s.suspended || s.unknownUsage) return s;
    if (s.status === 'awaiting_input' && s.priorStatus === 'verified' && s.project.visualReview) {
        const decision = await ensureVisualApproval(store, run);
        if (decision === 'pending') return s;
        await store.transition(run, 'verified', 'Visual decision recorded for exact candidate');
        return decision === 'approved'
            ? store.transition(run, 'handoff_ready', 'Operator approved all visual evidence')
            : store.transition(run, 'changes_requested', 'Operator requested visual changes');
    }
    if (['failed', 'cancelled', 'handoff_ready', 'awaiting_input'].includes(s.status)) return s;
    if (!s.project.headroom)
        throw Error('This older run has no Headroom route. Create a new routed run before starting another attempt.');
    if ((await bundle()).digest !== s.bundleDigest) throw Error('Worker skill bundle changed after run approval');
    const errors = await runtime.preflight(s.project);
    if (errors.length) throw Error(errors.join('; '));
    if (s.status === 'ready') {
        if (!s.attempts.some(a => a.role === 'architect')) {
            const objective = plannerPrompt(s, 'developer');
            const outcome = await roleJob(store, s, runtime, 'architect', 'plan-architect', objective, s.sourceBase, true, undefined, control);
            if (!outcome.passed) return store.transition(run, 'awaiting_input', 'Architect plan failed or usage unknown');
            s = await store.read(run);
            try { admitProposal(parseJsonText(outcome.text), s, 'architect'); }
            catch (error) { return store.transition(run, 'awaiting_input', `Architect proposal rejected: ${String(error)}`); }
            return s;
        }
        if (!s.attempts.some(a => a.role === 'tester')) {
            const objective = plannerPrompt(s, 'reviewer');
            const outcome = await roleJob(store, s, runtime, 'tester', 'plan-tester', objective, s.sourceBase, true, undefined, control);
            if (!outcome.passed) return store.transition(run, 'awaiting_input', 'Test plan failed or usage unknown');
            s = await store.read(run);
            try { admitProposal(parseJsonText(outcome.text), s, 'tester'); }
            catch (error) { return store.transition(run, 'awaiting_input', `Test planner proposal rejected: ${String(error)}`); }
            return s;
        }
        const task = (await loadedProposal(store, s, 'architect')).tasks[0];
        await store.transition(run, 'running', 'Approved developer task dispatched');
        s = await store.read(run);
        const outcome = await roleJob(store, s, runtime, 'developer', 'developer-' + (s.reworks + 1), `${task.objective}\nDone when: ${task.doneWhen.join('; ')}\nCommit your changes and leave the checkout clean.`, s.candidate ?? s.sourceBase, false, task.limits, control);
        if (!outcome.passed) return store.transition(run, 'changes_requested', 'Developer attempt failed');
        let imported;
        try { imported = await importCandidate(candidateStore(store, run), path.join(evidenceDir(store, run, outcome.attempt.id), 'source'), s.candidate ?? s.sourceBase, s.project.allowedPaths); }
        catch (error) { return store.transition(run, 'changes_requested', `Candidate import rejected: ${String(error)}`); }
        return store.update(run, 'candidate_imported', imported, state => { state.candidate = imported.candidate; state.review = undefined; state.checks = []; move(state, 'candidate', 'Developer candidate imported'); });
    }
    if (s.status === 'candidate') return store.transition(run, 'verifying', 'Independent review and mandatory checks');
    if (s.status === 'verifying') {
        if (!s.candidate) throw Error('Missing candidate');
        if (!s.review) {
            const task = (await loadedProposal(store, s, 'tester')).tasks[0];
            const outcome = await roleJob(store, s, runtime, 'reviewer', 'review-' + (s.reworks + 1),
                `${task.objective}\nDone when: ${task.doneWhen.join('; ')}\nReturn only JSON: {"schemaVersion":1,"candidate":"${s.candidate}","specDigest":"${s.specDigest}","requirements":${JSON.stringify(s.project.requirements)},"verdict":"pass|changes_requested","findings":[]}.`, s.candidate, true, task.limits, control);
            if (!outcome.passed) return store.transition(run, 'changes_requested', 'Independent review failed');
            const review = reviewSchema.parse(parseJsonText(outcome.text));
            if (review.candidate !== s.candidate || review.specDigest !== s.specDigest || s.project.requirements.some(r => !review.requirements.includes(r)))
                return store.transition(run, 'changes_requested', 'Review identity or requirement coverage mismatch');
            s = await store.update(run, 'review_recorded', { verdict: review.verdict }, state => { state.review = { attemptId: outcome.attempt.id, record: review, file: outcome.attempt.handoff! }; });
            if (review.verdict !== 'pass' || review.findings.some(f => f.severity === 'blocking')) return store.transition(run, 'changes_requested', 'Review requested changes');
            return s;
        }
        const definition = [...s.project.checks, s.project.artifactCheck].find(c => !s.checks.some(r => r.id === c.id && r.candidate === s.candidate));
        if (definition) {
            const result = await checkJob(store, s, runtime, checkSchema.parse(definition), `check-${definition.id}-${s.reworks + 1}`, control);
            if (!result.passed) return store.transition(run, 'changes_requested', `Mandatory check ${definition.id} failed`);
            return store.read(run);
        }
        if (s.checks.some(c => !c.passed) || !s.review || s.review.record.verdict !== 'pass') return store.transition(run, 'changes_requested', 'Acceptance evidence is incomplete');
        return store.transition(run, 'verified', 'All approved checks and independent review passed');
    }
    if (s.status === 'verified') {
        if (s.project.visualReview) return store.transition(run, 'awaiting_input', 'Operator must inspect and decide on every visual case');
        return store.transition(run, 'handoff_ready', 'Verified candidate ready for operator handoff');
    }
    if (s.status === 'changes_requested') {
        if (s.reworks >= s.project.limits.maxReworks || remaining(s) <= s.project.limits.verificationReserveAttempts + 1)
            return store.transition(run, 'failed', 'Rework budget exhausted');
        return store.update(run, 'rework_started', {}, state => { state.reworks++; move(state, 'ready', 'Rework attempt'); });
    }
    return s;
}

export async function stepRun(store: Store, run: string, runtime = new LocalRuntime(), control?: RunControl): Promise<State> {
    let state: State;
    try { state = await store.execution(run, () => stepUnlocked(store, run, runtime, control)); }
    catch (error) {
        if (!(error instanceof RunCancelled)) throw error;
        state = await cancelRun(store, run);
    }
    if (terminal(state.status)) await store.release(run);
    return state;
}
export async function runRun(store: Store, run: string, runtime = new LocalRuntime()): Promise<State> {
    for (;;) {
        const before = await store.read(run);
        const after = await stepRun(store, run, runtime);
        if (after.revision === before.revision || terminal(after.status) || after.status === 'awaiting_input') return after;
    }
}
export const resumeRun = runRun;

/** A registered supervisor whose recorded identity matches a live process. */
export async function liveSupervisor(state: State): Promise<boolean> {
    return state.supervisor !== undefined && await isOwnedAlive(state.supervisor.process);
}

/**
 * Runs one run to a stopping point as its only supervisor. The operator's stored control
 * drives the pause gate and cancellation; only this process writes `suspended` while it lives.
 */
export async function superviseRun(store: Store, run: string, launchId: string, runtime: LocalRuntime, control: RunControl): Promise<State> {
    id.parse(launchId);
    const self = await identify(process.pid);
    if (!self) throw Error('Supervisor process identity is unavailable');
    await store.update(run, 'supervisor_started', { launchId, pid: self.pid }, async state => {
        if (state.supervisor && await isOwnedAlive(state.supervisor.process)) throw new SupervisorConflict(`Supervisor ${state.supervisor.process.pid} already runs ${run}`);
        state.supervisor = { launchId, process: self, launchedAt: now() };
    });
    const stop = new AbortController();
    const signal = AbortSignal.any([control.signal, stop.signal]);
    const step: RunControl = { signal, pause: control.pause };
    const sync = async () => {
        let s = await store.read(run);
        if (s.control === 'cancel') stop.abort();
        if (s.control === 'suspend') {
            control.pause.pause();
            const deadline = Date.now() + 5000;
            while (s.control === 'suspend' && !signal.aborted && s.activeJob) {
                if (s.activeJob.process && await groupStopped(s.activeJob.process.pgid)) break;
                if (s.activeJob.process && Date.now() >= deadline) throw Error('Worker process group did not stop after pause');
                await sleep(100);
                s = await store.read(run);
                if (s.control === 'cancel') stop.abort();
            }
        }
        if (s.control !== 'suspend' || signal.aborted) control.pause.resume();
        const suspended = s.control === 'suspend' && !signal.aborted;
        if (s.suspended !== suspended)
            await store.update(run, suspended ? 'factory_paused' : 'factory_resumed', { launchId }, state => { state.suspended = state.control === 'suspend' && !signal.aborted; });
    };
    let watching = true;
    let watcher: Promise<void> | undefined;
    let watcherError: string | undefined;
    let outcome: 'waiting' | 'finished' | 'cancelled' | 'error' = 'waiting';
    let message = '';
    try {
        await sync();
        watcher = (async () => {
            while (watching) {
                try { await sync(); }
                catch (error) {
                    watcherError = (error instanceof Error ? error.message : String(error)).slice(0, 300);
                    process.stderr.write(`Supervisor watcher: ${watcherError}\n`);
                    stop.abort();
                    break;
                }
                await sleep(1000);
            }
        })();
        let state = await store.read(run);
        for (;;) {
            while (control.pause.paused && !signal.aborted) await sleep(100);
            const before = await store.read(run);
            try { state = await stepRun(store, run, runtime, step); }
            catch (error) {
                // An operator control that lands between a step's first read and its job start refuses the job.
                // That is not a fault: the next pass pauses or cancels.
                const current = await store.read(run);
                if (signal.aborted || current.control === 'cancel') {
                    state = await cancelRun(store, run);
                    if (terminal(state.status)) await store.release(run);
                    break;
                }
                if (current.control === 'suspend' || current.suspended) { await sleep(100); continue; }
                throw error;
            }
            if (terminal(state.status) || state.status === 'awaiting_input') break;
            if (state.revision === before.revision) {
                if (!signal.aborted && (control.pause.paused || state.control || state.suspended)) { await sleep(100); continue; }
                break;
            }
        }
        outcome = state.status === 'cancelled' ? 'cancelled' : terminal(state.status) ? 'finished' : 'waiting';
        message = state.reason ?? state.status;
        return state;
    } catch (error) {
        outcome = 'error';
        message = error instanceof Error ? error.message : String(error);
        throw error;
    } finally {
        watching = false;
        if (watcher) await watcher;
        if (watcherError) {
            outcome = 'error';
            message = `Supervisor control failed: ${watcherError}`;
            await store.update(run, 'supervisor_control_failed', { launchId, message }, state => {
                if (!terminal(state.status) || state.status === 'cancelled') state.reason = message;
            });
        }
        await store.update(run, 'supervisor_exited', { launchId, outcome, message: message.slice(0, 300) }, state => {
            if (state.supervisor?.launchId === launchId) state.supervisor = undefined;
        });
    }
}
