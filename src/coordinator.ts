import { mkdir, readdir, readFile, rm, stat } from 'node:fs/promises';
import path from 'node:path';
import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { bundle } from './bundle.ts';
import { check as checkSchema, choice, id, projectSchema, reviewSchema, meterReadingSchema, handoffSchema } from './contracts.ts';
import type { Attempt, Check, MeterReading, Project, Result, Role, Segment, State, WorkerChoice } from './contracts.ts';
import { checkout, cleanRepository, createStore, importCandidate, treeDigest } from './git.ts';
import { contextMax, handoffTrigger } from './model-context.ts';
import { PauseGate, groupStopped, identify, isOwnedAlive } from './process.ts';
import { LocalRuntime, workerHome } from './runtime.ts';
import type { Job } from './runtime.ts';
import { Store, durable, immutable, json, move, record, sha, verifyFile, within } from './store.ts';
import { meteredTokens, parseWorkerOutput, workerCommand, UsageMeter } from './workers.ts';
import type { MeterReading as LiveReading, WorkerOutput } from './workers.ts';

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
type JobOutcome = { attempt: Attempt; text: string; passed: boolean; result: Result; reason: string };
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

async function nextAttemptId(store: Store, s: State, base: string): Promise<string> {
    const recorded = new Set(s.attempts.map(attempt => attempt.id));
    for (let suffix = 1; ; suffix++) {
        const candidate = suffix === 1 ? base : `${base}-${suffix}`;
        if (recorded.has(candidate)) continue;
        try { await stat(evidenceDir(store, s.id, candidate)); }
        catch (error) {
            if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return candidate;
            throw error;
        }
    }
}

function successfulPlanner(s: State, role: 'architect' | 'tester') {
    return s.attempts.some(attempt => attempt.role === role && attempt.status === 'completed' && attempt.admitted === true);
}

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

// CLI structured-output engines reject bound keywords; zod still enforces every bound when the result is parsed.
const unenforced = new Set(['$schema', 'minLength', 'maxLength', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'minItems', 'maxItems']);
function outputSchema(schema: z.ZodTypeAny): string {
    return JSON.stringify(zodToJsonSchema(schema, { $refStrategy: 'none' }), (key, value) => unenforced.has(key) ? undefined : value);
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
    const attempt = s.attempts.find(a => a.role === source && a.status === 'completed' && a.admitted === true);
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

/** Finds the newest Codex rollout under `<home>/.codex/sessions`, or the Claude capture log. Null while neither exists yet. */
async function meterSourcePath(provider: WorkerChoice['provider'], home: string, captureDir: string): Promise<string | null> {
    if (provider === 'claude') return path.join(captureDir, 'stdout.log');
    const root = path.join(home, '.codex', 'sessions');
    const found: { file: string; mtime: number }[] = [];
    async function walk(dir: string) {
        let entries;
        try { entries = await readdir(dir, { withFileTypes: true }); } catch (error) {
            if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return;
            throw error;
        }
        for (const entry of entries) {
            const full = path.join(dir, entry.name);
            if (entry.isDirectory()) await walk(full);
            else if (entry.isFile() && entry.name.startsWith('rollout-') && entry.name.endsWith('.jsonl'))
                found.push({ file: full, mtime: (await stat(full)).mtimeMs });
        }
    }
    await walk(root);
    if (!found.length) return null;
    return found.toSorted((a, b) => b.mtime - a.mtime)[0].file;
}

/**
 * Runs one worker process, polling its usage every 2s so a token or context overrun kills it through
 * `job.limit` instead of waiting for the timeout. Always writes a final meter.json: `source: 'final'`
 * when the worker's own completion line carries usage, `'meter'` when only the live tail does (for
 * example after a limit kill truncates the process before it can report).
 */
async function runSegment(store: Store, s: State, runtime: LocalRuntime, role: Role, index: number, kind: 'work' | 'handoff',
    job: Omit<Job, 'limit'>, maxTokens: number, spentBefore: number, segmentDir: string, modelWindow: number | null,
    resumeCursor?: NonNullable<ReturnType<UsageMeter['cursor']>>):
    Promise<{ completed: Awaited<ReturnType<LocalRuntime['execute']>>; parsed: WorkerOutput; segment: Segment; cursor: ReturnType<UsageMeter['cursor']> }> {
    const provider = choiceFor(s, role).provider, model = choiceFor(s, role).model;
    const ratio = s.project.limits.handoffContextRatio ?? 0.6;
    const meterFile = path.join(segmentDir, 'meter.json');
    const controller = new AbortController();
    const cancellation = new AbortController();
    let meter: UsageMeter | null = null;
    let activityMeter: UsageMeter | null = null;
    let spawnedAtMs: number | null = null;
    let pausedAtMs: number | null = job.pause?.paused ? Date.now() : null;
    let pausedMs = 0;
    let enforcing = true;
    let providerStartedAt: string | null = null;
    let lastActivityAt: string | null = null;
    let activityBytes = 0;
    const startLimit = s.project.limits.workerStartTimeoutMs ?? 90000;
    const idleLimit = s.project.limits.workerIdleTimeoutMs ?? 600000;
    let lastActivityMs = 0;
    const runningNow = () => {
        const current = Date.now();
        return current - (spawnedAtMs ?? current) - pausedMs - (pausedAtMs === null ? 0 : current - pausedAtMs);
    };
    const onPause = () => { if (pausedAtMs === null) pausedAtMs = Date.now(); };
    const onResume = () => { if (pausedAtMs !== null) { pausedMs += Date.now() - pausedAtMs; pausedAtMs = null; } };
    job.pause?.addEventListener('pause', onPause);
    job.pause?.addEventListener('resume', onResume);
    const cursor = () => meter?.cursor() ?? null;
    const tracked: { lastReading: LiveReading | null; failed: boolean; error?: unknown } = { lastReading: null, failed: false };
    const poll = async () => {
        const activityPath = path.join(job.captureDir, 'stdout.log');
        if (!activityMeter) activityMeter = new UsageMeter(provider, activityPath);
        activityMeter.read();
        const activity = activityMeter.activity();
        let stderrBytes = 0;
        try { stderrBytes = (await stat(path.join(job.captureDir, 'stderr.log'))).size; } catch { /* capture may not exist yet */ }
        const recordActivity = (rolloutBytes: number) => {
            const totalActivityBytes = activity.bytes + stderrBytes + rolloutBytes;
            if (totalActivityBytes > activityBytes) { activityBytes = totalActivityBytes; lastActivityMs = runningNow(); lastActivityAt = now(); }
            if (activity.started && providerStartedAt === null) { providerStartedAt = now(); lastActivityMs = runningNow(); }
            if (enforcing && pausedAtMs === null && spawnedAtMs !== null) {
                const elapsed = runningNow();
                if (!providerStartedAt && elapsed >= startLimit) controller.abort('stall_start');
                else if (providerStartedAt && elapsed - lastActivityMs >= idleLimit) controller.abort('stall_idle');
            }
        };
        if (!meter) {
            const src = resumeCursor && job.resumeHome
                ? path.join(workerHome(job.scratchDir), path.relative(job.resumeHome, resumeCursor.path))
                : await meterSourcePath(provider, workerHome(job.scratchDir), job.captureDir);
            if (!src) {
                recordActivity(0);
                const empty = { segment: index, kind, model, raw: null, usage: null, metered: null, spentBefore, allowance: maxTokens,
                    contextTokens: null, peakContext: null, contextMax: provider === 'claude' ? modelWindow : null, trigger: null,
                    at: now(), source: 'meter', providerStartedAt, lastActivityAt, ...(controller.signal.aborted ? { reason: controller.signal.reason } : {}) };
                await durable(meterFile, json(meterReadingSchema.parse(empty)));
                return;
            }
            if (resumeCursor) {
                try { await stat(src); } catch (error) {
                    if (error instanceof Error && 'code' in error && error.code === 'ENOENT') { recordActivity(0); return; }
                    throw error;
                }
            }
            meter = new UsageMeter(provider, src, resumeCursor);
        }
        const reading = meter.read();
        recordActivity(meter.activity().bytes);
        if (!reading) {
            const empty = { segment: index, kind, model, raw: null, usage: null, metered: null, spentBefore, allowance: maxTokens,
                contextTokens: null, peakContext: null, contextMax: provider === 'claude' ? modelWindow : null, trigger: null,
                at: now(), source: 'meter', providerStartedAt, lastActivityAt, ...(controller.signal.aborted ? { reason: controller.signal.reason } : {}) };
            await durable(meterFile, json(meterReadingSchema.parse(empty)));
            return;
        }
        tracked.lastReading = reading;
        const metered = meteredTokens(reading.usage);
        const max = provider === 'codex' ? reading.contextMax : modelWindow;
        const trigger = max !== null ? handoffTrigger(ratio, max, model) : null;
        const live = { segment: index, kind, model, raw: reading.raw, usage: reading.usage, metered,
            spentBefore, allowance: maxTokens, contextTokens: reading.contextTokens, peakContext: reading.peakContext, contextMax: max, trigger, at: now(), source: 'meter', providerStartedAt, lastActivityAt, ...(controller.signal.aborted ? { reason: controller.signal.reason } : {}) } satisfies MeterReading;
        await durable(meterFile, json(meterReadingSchema.parse(live)));
        if (!controller.signal.aborted) {
            if (live.spentBefore + live.metered > live.allowance) controller.abort('token_limit');
            else if (reading.compacted) controller.abort('compacted');
            else if (kind === 'work' && live.peakContext !== null && live.trigger !== null && live.peakContext >= live.trigger) controller.abort('context_limit');
        }
    };
    let inFlight: Promise<void> | null = null;
    const wrappedOnSpawn = async (pid: number) => {
        spawnedAtMs = Date.now();
        lastActivityMs = 0;
        await job.onSpawn?.(pid);
    };
    const interval = setInterval(() => {
        if (!inFlight && !tracked.failed) inFlight = poll().catch(error => {
            tracked.failed = true; tracked.error = error; cancellation.abort(error);
        }).finally(() => { inFlight = null; });
    }, 2000);
    let completed: Awaited<ReturnType<LocalRuntime['execute']>>;
    try {
        completed = await runtime.execute({ ...job, onSpawn: wrappedOnSpawn, signal: AbortSignal.any([job.signal, cancellation.signal]), limit: controller.signal });
    } finally {
        enforcing = false;
        if (pausedAtMs !== null) { pausedMs += Date.now() - pausedAtMs; pausedAtMs = null; }
        clearInterval(interval);
        await inFlight;
        job.pause?.removeEventListener('pause', onPause);
        job.pause?.removeEventListener('resume', onResume);
    }
    if (tracked.failed) throw tracked.error;
    await poll();
    const stdout = await readFile(path.join(job.captureDir, 'stdout.log'), 'utf8');
    const parsed = parseWorkerOutput(provider, stdout);
    const usageKnown = parsed.inputTokens !== null && parsed.cachedInputTokens !== null && parsed.outputTokens !== null;
    const usage = usageKnown ? { input: parsed.inputTokens!, cached: parsed.cachedInputTokens!, output: parsed.outputTokens! } : tracked.lastReading?.usage ?? null;
    const raw = usageKnown ? parsed.raw : tracked.lastReading?.raw ?? null;
    const metered = usage ? meteredTokens(usage) : null;
    const source: Segment['source'] = usageKnown ? 'final' : 'meter';
    const contextMaxValue = provider === 'codex' ? tracked.lastReading?.contextMax ?? null : modelWindow;
    const trigger = contextMaxValue !== null ? handoffTrigger(ratio, contextMaxValue, model) : null;
    const peakContext = tracked.lastReading?.peakContext ?? null;
    const final: MeterReading = { segment: index, kind, model, raw, usage,
        metered, spentBefore, allowance: maxTokens, contextTokens: tracked.lastReading?.contextTokens ?? null,
        contextMax: contextMaxValue, peakContext, trigger, at: now(), source, providerStartedAt, lastActivityAt,
        ...(completed.reason === 'stall_start' || completed.reason === 'stall_idle' ? { reason: completed.reason } : {}) };
    await durable(meterFile, json(meterReadingSchema.parse(final)));
    const reason = completed.reason !== 'completed' ? completed.reason
        : metered !== null && spentBefore + metered > maxTokens ? 'token_limit'
        : tracked.lastReading?.compacted ? 'compacted'
        : kind === 'work' && peakContext !== null && trigger !== null && peakContext >= trigger ? 'context_limit'
        : completed.reason === 'completed' ? parsed.reason ?? completed.reason : completed.reason;
    const segment: Segment = { index, kind, model, startedAt: completed.startedAt, endedAt: completed.endedAt, reason,
        raw, input: usage?.input ?? null, cached: usage?.cached ?? null, output: usage?.output ?? null, metered,
        contextMax: contextMaxValue, trigger, peakContext, source, providerStartedAt, lastActivityAt };
    const reportedWindow = parsed.contextWindows?.[model];
    if (provider === 'claude' && reportedWindow !== undefined && reportedWindow !== modelWindow) segment.contextWindowMismatch = true;
    return { completed, parsed, segment, cursor: cursor() };
}

async function roleJob(store: Store, s: State, runtime: LocalRuntime, role: Role, jobId: string, objective: string, source: string, readOnlySource: boolean,
    limits: { maxSeconds: number; maxTokens: number } | undefined, control: RunControl | undefined, output?: z.ZodTypeAny): Promise<JobOutcome> {
    if (control?.signal.aborted) throw new RunCancelled();
    budget(s, role);
    const selected = choiceFor(s, role);
    const modelWindow = selected.provider === 'claude' ? contextMax('claude', selected.model, null) : null;
    const maxTokens = Math.min(tokenAllowance(s, role), limits?.maxTokens ?? Infinity);
    if (maxTokens < 1) throw Error('No task token allowance remains after verification reserve');
    const run = s.id, root = evidenceDir(store, run, jobId), workspace = path.join(root, 'source');
    const policyDir = path.join(root, 'policy'), outputDir = path.join(root, 'proposed');
    let captureDir = path.join(root, 'capture'), scratchDir = path.join(root, 'scratch');
    // A job refused before job_started (for example by a pause) leaves an unrecorded checkout. It holds no evidence.
    if (!s.attempts.some(a => a.id === jobId)) await rm(root, { recursive: true, force: true });
    for (const dir of [policyDir, outputDir, captureDir, scratchDir]) await mkdir(dir, { recursive: true, mode: 0o700 });
    await checkout(candidateStore(store, run), workspace, source);
    const b = await bundle();
    const policy = b.content[role];
    const schema = output && { json: outputSchema(output), file: path.join(policyDir, 'output-schema.json') };
    if (schema) await durable(schema.file, schema.json);
    let worker = workerCommand(selected, role, workspace, prompt(s, objective), policy, s.project.headroom, schema);
    const startedAt = now();
    await store.update(run, 'job_started', { jobId, role }, state => {
        budget(state, role);
        state.activeJob = { id: jobId, runtime: 'macos-sandbox', kind: role, startedAt };
        state.attempts.push({ id: jobId, role, startedAt, model: choiceFor(state, role), candidate: source, status: 'running' });
    });
    const sourceBefore = readOnlySource ? await treeDigest(workspace) : null;
    let segmentDir = path.join(root, 'segment-1');
    let deadline = Date.now() + Math.min(s.project.limits.attemptTimeoutMs, s.project.limits.maxWallMs - s.elapsedMs, limits ? limits.maxSeconds * 1000 : Infinity);
    const segments: Segment[] = [];
    let spent = 0, elapsed = 0, usageKnown = true;
    let kind: Segment['kind'] = 'work';
    let resumeHome: string | undefined, resumeCursor: NonNullable<ReturnType<UsageMeter['cursor']>> | undefined;
    let completed, parsed, segment;
    let failure: string | undefined;
    let executedWorker = worker;
    try {
        for (;;) {
            executedWorker = worker;
            const outcome = await runSegment(store, s, runtime, role, segments.length + 1, kind,
                { id: jobId, project: s.project, workspace, policyDir, outputDir, captureDir, scratchDir, resumeHome,
                    argv: [worker.executable, ...worker.args], stdin: worker.stdin, env: worker.env, provider: selected.provider,
                    readOnlySource: readOnlySource || kind === 'handoff', network: s.project.runtime.network, timeoutMs: Math.max(1, deadline - Date.now()),
                    maxLogBytes: s.project.limits.maxLogBytes, signal: control?.signal ?? new AbortController().signal, pause: control?.pause,
                    onSpawn: pid => recordProcess(store, run, jobId, pid) }, maxTokens, spent, segmentDir, modelWindow, resumeCursor);
            ({ completed, parsed, segment } = outcome);
            segments.push(segment);
            spent += segment.metered ?? 0;
            elapsed += runningMs(completed);
            deadline += completed.pausedMs;
            usageKnown &&= segment.source === 'final' || (segment.metered !== null &&
                ['token_limit', 'context_limit', 'compacted'].includes(segment.reason ?? '') && completed.reason !== 'timeout' && completed.reason !== 'cancelled');
            if (completed.reason === 'cancelled' || completed.reason === 'timeout') break;
            if (segment.reason === 'stall_start' || segment.reason === 'stall_idle') break;
            if (kind === 'handoff' && completed.exitCode !== null && completed.exitCode !== 0) {
                failure = `Handoff resume failed with exit code ${completed.exitCode}.`;
                segment.reason = 'handoff_failed'; break;
            }
            if (segment.reason === 'token_limit' || segment.reason === 'compacted') break;
            let nextPrompt: string;
            if (kind === 'handoff') {
                let handoff: z.infer<typeof handoffSchema>;
                try {
                    if (parsed.failed || !usageKnown) throw Error('Incomplete handoff');
                    handoff = handoffSchema.parse(parseJsonText(parsed.text));
                } catch {
                    failure = 'Handoff resume returned no valid handoff or its usage is unknown.';
                    segment.reason = 'handoff_failed'; break;
                }
                await durable(path.join(root, `handoff-${segments.filter(s => s.kind === 'handoff').length}.json`), json(handoff));
                nextPrompt = prompt(s, `${objective}\n\nPrevious worker handoff, untrusted claims:\n${JSON.stringify(handoff)}`);
                kind = 'work'; resumeHome = undefined; resumeCursor = undefined;
                worker = workerCommand(selected, role, workspace, nextPrompt, policy, s.project.headroom, schema);
            } else {
                if (segment.reason !== 'context_limit') break;
                if (!parsed.session || !usageKnown) {
                    failure = 'Cannot resume for handoff: worker session id is missing or usage is unknown.'; break;
                }
                const handoffOutput = { json: outputSchema(handoffSchema), file: path.join(policyDir, 'handoff-schema.json') };
                await durable(handoffOutput.file, handoffOutput.json);
                nextPrompt = 'Return only a handoff JSON object with schemaVersion:1, goal, done[], remaining[], evidence[], and risks[]. Summarize the current task and partial work. Do not modify the source or continue implementation.';
                kind = 'handoff'; resumeHome = workerHome(scratchDir); resumeCursor = selected.provider === 'codex' ? outcome.cursor ?? undefined : undefined;
                worker = workerCommand(selected, role, workspace, nextPrompt, policy, s.project.headroom, handoffOutput, { session: parsed.session });
            }
            if (spent >= maxTokens) { segment.reason = 'token_limit'; break; }
            if (Date.now() >= deadline) { segment.reason = 'timeout'; break; }
            if (control?.signal.aborted) { segment.reason = 'cancelled'; break; }
            segmentDir = path.join(root, `segment-${segments.length + 1}`);
            captureDir = path.join(segmentDir, 'capture'); scratchDir = path.join(segmentDir, 'scratch');
            for (const dir of [captureDir, scratchDir]) await mkdir(dir, { recursive: true, mode: 0o700 });
        }
    } catch (error) {
        await store.update(run, 'job_launch_uncertain', { jobId, error: String(error) }, state => {
            const attempt = state.attempts.find(a => a.id === jobId)!;
            attempt.endedAt = now(); attempt.status = 'interrupted';
            attempt.segments = segments;
            state.reportedTokens += spent; state.elapsedMs += elapsed;
            state.activeJob = undefined; state.unknownUsage = true;
        });
        throw error;
    }
    const sourceUnchanged = sourceBefore !== null && sourceBefore === await treeDigest(workspace);
    const jobReason = failure ? 'handoff_failed' : segment.reason ?? completed.reason;
    const passed = jobReason === 'completed' && completed.exitCode === 0 && !parsed.failed && usageKnown && (!readOnlySource || sourceUnchanged);
    const current = await store.read(run);
    const result: Result = { id: jobId, kind: 'worker', candidate: source, specDigest: current.specDigest,
        definitionDigest: sha(json({ role, objective, model: choiceFor(s, role) })), runtime: runtime.identity,
        startedAt, endedAt: completed.endedAt, exitCode: completed.exitCode, signal: completed.signal,
        reason: jobReason, stdout: await record(store.dir(run), path.relative(store.dir(run), path.join(captureDir, 'stdout.log'))),
        stderr: await record(store.dir(run), path.relative(store.dir(run), path.join(captureDir, 'stderr.log'))),
        passed: passed && sourceUnchanged, sourceUnchanged,
        argv: [executedWorker.executable, ...executedWorker.args], cwd: workspace };
    // The untrusted text is retained as a claim. Only runner checks establish acceptance.
    const handoffPath = path.join(root, 'handoff.json');
    await mkdir(root, { recursive: true });
    await durable(handoffPath, failure ?? parsed.text);
    const handoff = await record(store.dir(run), path.relative(store.dir(run), handoffPath));
    const latest = await store.update(run, 'job_finished', { jobId, passed, reason: jobReason }, state => {
        const attempt = state.attempts.find(a => a.id === jobId)!;
        attempt.endedAt = completed.endedAt; attempt.status = passed ? 'completed' : 'failed'; attempt.result = result;
        const sum = (key: 'input' | 'output' | 'cached') => segments.every(s => s[key] !== null) ? segments.reduce((total, s) => total + (s[key] ?? 0), 0) : null;
        attempt.inputTokens = sum('input'); attempt.outputTokens = sum('output'); attempt.cachedInputTokens = sum('cached');
        attempt.segments = segments;
        attempt.handoff = handoff; state.activeJob = undefined;
        if (!usageKnown) state.unknownUsage = true;
        state.reportedTokens += spent;
        state.elapsedMs += elapsed;
    });
    if (completed.reason === 'cancelled' || jobReason === 'cancelled') throw new RunCancelled();
    return { attempt: latest.attempts.find(a => a.id === jobId)!, text: parsed.text, passed, result, reason: jobReason };
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
        if (!successfulPlanner(s, 'architect')) {
            const objective = plannerPrompt(s, 'developer');
            const outcome = await roleJob(store, s, runtime, 'architect', await nextAttemptId(store, s, 'plan-architect'), objective, s.sourceBase, true, undefined, control, proposalSchema);
            if (!outcome.passed) return store.transition(run, 'awaiting_input', outcome.reason === 'stall_start' || outcome.reason === 'stall_idle' ? outcome.reason : 'Architect plan failed or usage unknown');
            s = await store.read(run);
            try {
                admitProposal(parseJsonText(outcome.text), s, 'architect');
                await store.update(run, 'proposal_admitted', { jobId: outcome.attempt.id, role: 'architect' }, state => {
                    const attempt = state.attempts.find(item => item.id === outcome.attempt.id)!;
                    attempt.admitted = true;
                });
            }
            catch (error) { return store.transition(run, 'awaiting_input', `Architect proposal rejected: ${String(error)}`); }
            return s;
        }
        if (!successfulPlanner(s, 'tester')) {
            const objective = plannerPrompt(s, 'reviewer');
            const outcome = await roleJob(store, s, runtime, 'tester', await nextAttemptId(store, s, 'plan-tester'), objective, s.sourceBase, true, undefined, control, proposalSchema);
            if (!outcome.passed) return store.transition(run, 'awaiting_input', outcome.reason === 'stall_start' || outcome.reason === 'stall_idle' ? outcome.reason : 'Test plan failed or usage unknown');
            s = await store.read(run);
            try {
                admitProposal(parseJsonText(outcome.text), s, 'tester');
                await store.update(run, 'proposal_admitted', { jobId: outcome.attempt.id, role: 'tester' }, state => {
                    const attempt = state.attempts.find(item => item.id === outcome.attempt.id)!;
                    attempt.admitted = true;
                });
            }
            catch (error) { return store.transition(run, 'awaiting_input', `Test planner proposal rejected: ${String(error)}`); }
            return s;
        }
        const task = (await loadedProposal(store, s, 'architect')).tasks[0];
        if (s.attempts.filter(attempt => attempt.role === 'developer').length >= s.project.limits.maxReworks + 1)
            return store.transition(run, 'awaiting_input', 'Implementation attempt ceiling exhausted');
        await store.transition(run, 'running', 'Approved developer task dispatched');
        s = await store.read(run);
        const outcome = await roleJob(store, s, runtime, 'developer', await nextAttemptId(store, s, 'developer-' + (s.reworks + 1)), `${task.objective}\nDone when: ${task.doneWhen.join('; ')}\nCommit your changes and leave the checkout clean.`, s.candidate ?? s.sourceBase, false, task.limits, control);
        if (!outcome.passed) return store.transition(run, outcome.reason === 'stall_start' || outcome.reason === 'stall_idle' ? 'awaiting_input' : 'changes_requested',
            outcome.reason === 'stall_start' || outcome.reason === 'stall_idle' ? outcome.reason : `Developer attempt failed: ${outcome.reason}`);
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
            const outcome = await roleJob(store, s, runtime, 'reviewer', await nextAttemptId(store, s, 'review-' + (s.reworks + 1)),
                `${task.objective}\nDone when: ${task.doneWhen.join('; ')}\nReturn only JSON: {"schemaVersion":1,"candidate":"${s.candidate}","specDigest":"${s.specDigest}","requirements":${JSON.stringify(s.project.requirements)},"verdict":"pass|changes_requested","findings":[]}.`, s.candidate, true, task.limits, control, reviewSchema);
            if (!outcome.passed) return store.transition(run, outcome.reason === 'stall_start' || outcome.reason === 'stall_idle' ? 'awaiting_input' : 'changes_requested',
                outcome.reason === 'stall_start' || outcome.reason === 'stall_idle' ? outcome.reason : `Independent review failed: ${outcome.reason}`);
            const review = reviewSchema.parse(parseJsonText(outcome.text));
            if (review.candidate !== s.candidate || review.specDigest !== s.specDigest || s.project.requirements.some(r => !review.requirements.includes(r)))
                return store.transition(run, 'changes_requested', 'Review identity or requirement coverage mismatch');
            s = await store.update(run, 'review_recorded', { verdict: review.verdict }, state => { state.review = { attemptId: outcome.attempt.id, record: review, file: outcome.attempt.handoff! }; });
            if (review.verdict !== 'pass' || review.findings.some(f => f.severity === 'blocking')) return store.transition(run, 'changes_requested', 'Review requested changes');
            return s;
        }
        const definition = [...s.project.checks, s.project.artifactCheck].find(c => !s.checks.some(r => r.id === c.id && r.candidate === s.candidate));
        if (definition) {
            const result = await checkJob(store, s, runtime, checkSchema.parse(definition), await nextAttemptId(store, s, `check-${definition.id}-${s.reworks + 1}`), control);
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
