import { z } from 'zod';
export const id = z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/);
export const digest = z.string().regex(/^[a-f0-9]{64}$/);
export const commit = z.string().regex(/^[a-f0-9]{40}$/);
export const role = z.enum(['business', 'domain', 'architect', 'developer', 'reviewer', 'tester', 'coordinator']);
export const choice = z.object({ provider: z.enum(['codex', 'claude']), model: z.string().min(1), effort: z.enum(['low', 'medium', 'high', 'xhigh']) }).strict();
export const check = z.object({
    id, argv: z.array(z.string()).min(1), cwd: z.string().default('.'), timeoutMs: z.number().int().positive().max(1800000),
    requirements: z.array(id).min(1), outputPaths: z.array(z.string()).default([]),
}).strict();
export const headroomSchema = z.object({
    baseUrl: z.string().url().refine(value => {
        const match = /^http:\/\/127\.0\.0\.1:([1-9]\d{0,4})\/(?:p\/[a-z0-9][a-z0-9-]{0,63}\/)?v1$/.exec(value);
        return match !== null && Number(match[1]) <= 65535;
    }, 'Headroom must use an explicit loopback port and a /v1 endpoint'),
}).strict();
export type Headroom = z.infer<typeof headroomSchema>;
// handoffContextRatio is optional; callers default to 0.6 of the effective max context where they read it.
export const limitsSchema = z.object({
    maxAttempts: z.number().int().min(1).max(100), maxReworks: z.number().int().min(0).max(10),
    attemptTimeoutMs: z.number().int().positive().max(1800000), maxWallMs: z.number().int().positive(),
    maxReportedTokens: z.number().int().positive(), verificationReserveAttempts: z.number().int().min(1),
    maxLogBytes: z.number().int().min(1024).max(100000000), handoffContextRatio: z.number().gt(0).max(0.95).optional(),
    workerStartTimeoutMs: z.number().int().positive().max(1800000).optional(), workerIdleTimeoutMs: z.number().int().positive().max(1800000).optional(),
}).strict();
export const projectSchema = z.object({
    schemaVersion: z.literal(2), name: id, repository: z.string().min(1), base: commit,
    recipient: z.string().min(1), brief: z.string().min(1), policies: z.array(z.string()).min(1),
    requirements: z.array(id).min(1), checks: z.array(check).min(1),
    artifact: z.string().min(1), artifactCheck: check,
    visualReview: z.object({ caseIds: z.array(id).min(1).max(200) }).strict().optional(),
    allowedPaths: z.array(z.string()).min(1),
    runtime: z.object({ kind: z.literal('macos-sandbox'), toolPaths: z.array(z.string().min(1)).min(1), network: z.enum(['none', 'loopback', 'outbound']), authHomes: z.object({ codex: z.string().nullable(), claude: z.string().nullable() }).strict() }).strict(),
    models: z.object({ coordinator: choice, developer: choice, reviewer: choice, inspector: choice }).strict(),
    limits: limitsSchema,
    headroom: headroomSchema.optional(),
    billing: z.literal('subscription-only'), retentionDays: z.number().int().min(30),
}).strict().superRefine((v, c) => {
    if (v.limits.verificationReserveAttempts >= v.limits.maxAttempts)
        c.addIssue({ code: 'custom', message: 'Verification reserve must be smaller than total attempts' });
    const ids = new Set<string>();
    for (const x of [...v.checks, v.artifactCheck]) {
        if (ids.has(x.id))
            c.addIssue({ code: 'custom', message: `Duplicate check ${x.id}` });
        ids.add(x.id);
        for (const r of x.requirements)
            if (!v.requirements.includes(r))
                c.addIssue({ code: 'custom', message: `Unknown requirement ${r}` });
    }
    for (const r of v.requirements)
        if (!v.checks.some(x => x.requirements.includes(r)))
            c.addIssue({ code: 'custom', message: `No mandatory check covers ${r}` });
    if (v.models.developer.provider !== 'codex')
        c.addIssue({ code: 'custom', message: 'The first subscription profile requires Codex for implementation and Git commits' });
});
export type Project = z.infer<typeof projectSchema>;
export type Check = z.infer<typeof check>;
export type Role = z.infer<typeof role>;
export const states = ['draft', 'ready', 'running', 'candidate', 'verifying', 'verified', 'changes_requested', 'awaiting_input', 'handoff_ready', 'failed', 'cancelled'] as const;
export type Status = typeof states[number];
export const transitions: Record<Status, Status[]> = {
    draft: ['ready', 'awaiting_input', 'cancelled'], ready: ['running', 'awaiting_input', 'cancelled'], running: ['candidate', 'changes_requested', 'awaiting_input', 'failed', 'cancelled'],
    candidate: ['verifying', 'changes_requested', 'awaiting_input', 'cancelled'], verifying: ['verified', 'changes_requested', 'awaiting_input', 'failed', 'cancelled'],
    verified: ['handoff_ready', 'changes_requested', 'awaiting_input', 'cancelled'], changes_requested: ['ready', 'awaiting_input', 'failed', 'cancelled'],
    awaiting_input: ['draft', 'ready', 'running', 'candidate', 'verifying', 'verified', 'changes_requested', 'handoff_ready', 'cancelled'],
    handoff_ready: ['changes_requested', 'awaiting_input', 'cancelled'], failed: ['ready'], cancelled: ['ready'],
};
export const reviewSchema = z.object({ schemaVersion: z.literal(1), candidate: commit, specDigest: digest, requirements: z.array(id), verdict: z.enum(['pass', 'changes_requested']), findings: z.array(z.object({ severity: z.enum(['blocking', 'minor']), requirement: id, message: z.string().min(1), evidence: z.string().min(1) }).strict()) }).strict();
export type Review = z.infer<typeof reviewSchema>;
const timestamp = z.string().datetime();
// `started` is the exact `ps -o lstart= -p <pid>` text. PID plus start time proves identity; a PID alone can be reused.
export const ownedProcess = z.object({ pid: z.number().int().positive(), pgid: z.number().int().positive(), started: z.string().min(1) }).strict();
export type OwnedProcess = z.infer<typeof ownedProcess>;
const count = z.number().int().nonnegative();
const fileRecord = z.object({ path: z.string().min(1), sha256: digest }).strict();
const eventSchema = z.object({ sequence: z.number().int().positive(), at: timestamp, type: z.string().min(1), detail: z.unknown() }).strict();
const resultSchema = z.object({
    id, kind: z.enum(['check', 'worker', 'artifact']), candidate: commit, specDigest: digest, definitionDigest: digest,
    runtime: z.string().min(1), startedAt: timestamp, endedAt: timestamp, exitCode: z.number().int().nullable(),
    signal: z.string().nullable(), reason: z.string().min(1), stdout: fileRecord, stderr: fileRecord,
    passed: z.boolean(), sourceUnchanged: z.boolean(), argv: z.array(z.string()).min(1), cwd: z.string().min(1),
}).strict().superRefine((v, c) => {
    if (v.passed && (v.exitCode !== 0 || v.signal !== null || v.reason !== 'completed' || !v.sourceUnchanged))
        c.addIssue({ code: 'custom', message: 'Passing evidence requires successful execution and unchanged source' });
    if (Date.parse(v.endedAt) < Date.parse(v.startedAt))
        c.addIssue({ code: 'custom', message: 'Result ends before it starts' });
});
export const usageSchema = z.object({ input: count.safe(), cached: count.safe(), output: count.safe() }).strict()
    .refine(usage => usage.cached <= usage.input, 'Cached input exceeds gross input');
// Written to attempts/<job>/segment-<n>/meter.json on every poll, and once more with the
// final numbers when the segment ends. The dashboard reads the same shape the enforcer compares.
export const meterReadingSchema = z.object({
    segment: z.number().int().positive(), kind: z.enum(['work', 'handoff']), model: z.string().min(1),
    raw: z.record(count.safe()).nullable(), usage: usageSchema.nullable(), metered: count.safe().nullable(), spentBefore: count,
    allowance: z.number().int().positive(), contextTokens: count.nullable(), peakContext: count.nullable(), contextMax: count.nullable(),
    trigger: count.nullable(), at: timestamp, source: z.enum(['final', 'meter']), providerStartedAt: timestamp.nullable().optional(), lastActivityAt: timestamp.nullable().optional(), reason: z.string().optional(),
}).strict();
export type MeterReading = z.infer<typeof meterReadingSchema>;
export const segmentSchema = z.object({
    index: z.number().int().positive(), kind: z.enum(['work', 'handoff']), model: z.string().min(1),
    startedAt: timestamp, endedAt: timestamp.optional(), reason: z.string().optional(),
    raw: z.record(z.number()).nullable(), input: count.nullable(), cached: count.nullable(), output: count.nullable(),
    metered: count.nullable(), contextMax: count.nullable(), trigger: count.nullable(), peakContext: count.nullable(),
    source: z.enum(['final', 'meter']), contextWindowMismatch: z.boolean().optional(), providerStartedAt: timestamp.nullable().optional(), lastActivityAt: timestamp.nullable().optional(),
}).strict();
export type Segment = z.infer<typeof segmentSchema>;
export const handoffSchema = z.object({
    schemaVersion: z.literal(1), goal: z.string().min(1), done: z.array(z.string()),
    remaining: z.array(z.string()), evidence: z.array(z.string()), risks: z.array(z.string()),
}).strict();
const attemptSchema = z.object({
    id, role, startedAt: timestamp, endedAt: timestamp.optional(), model: choice, candidate: commit,
    status: z.enum(['running', 'completed', 'failed', 'interrupted']), result: resultSchema.optional(),
    admitted: z.boolean().optional(),
    inputTokens: count.nullable().optional(), outputTokens: count.nullable().optional(), cachedInputTokens: count.nullable().optional(), handoff: fileRecord.optional(),
    segments: z.array(segmentSchema).optional(),
}).strict();
export const specificationSchema = z.object({
    id, kind: z.enum(['business', 'domain']), title: z.string().min(1), revision: z.number().int().positive(),
    content: z.string().min(1), digest, requirements: z.array(id).min(1),
    approval: z.object({ owner: z.string().min(1), statement: z.string().min(1), approvedAt: timestamp }).strict(),
}).strict();
export const ticketSchema = z.object({
    id, title: z.string().min(1), requirements: z.array(id).min(1), specificationIds: z.array(id).min(1),
    status: z.enum(['todo', 'in_progress', 'blocked', 'done']), architectNote: z.string().min(1),
    updatedAt: timestamp, attemptIds: z.array(id),
    github: z.object({ url: z.string().regex(/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/issues\/[1-9][0-9]*$/), number: z.number().int().positive(), syncedAt: timestamp }).strict().optional(),
}).strict();
export const visualEvidenceSchema = z.object({
    id, label: z.string().min(1).max(300), sourceId: id, path: z.string().min(1), sha256: digest,
    mimeType: z.enum(['image/png', 'image/jpeg']), candidate: commit, specDigest: digest,
}).strict();
export type VisualEvidence = z.infer<typeof visualEvidenceSchema>;
export const questionBatchSchema = z.object({
    id, title: z.string().min(1), createdAt: timestamp,
    questions: z.array(z.object({ id, prompt: z.string().min(1), owner: z.string().min(1), impact: z.string().min(1),
        recommendation: z.string().min(1), options: z.array(z.string().min(1)), affectedTicketIds: z.array(id),
        visualEvidence: z.array(visualEvidenceSchema).min(1).optional() }).strict()).min(1),
    answer: z.object({ owner: z.string().min(1), source: z.literal('operator'), answeredAt: timestamp,
        answers: z.array(z.object({ questionId: id, value: z.string().trim().min(1).max(10000) }).strict()).min(1) }).strict().optional(),
}).strict();
const baseStateSchema = z.object({
    schemaVersion: z.literal(1), id, revision: z.number().int().positive(), status: z.enum(states),
    priorStatus: z.enum(states).optional(), reason: z.string().optional(), project: projectSchema,
    specDigest: digest, profileDigest: digest, bundleDigest: digest, policyDigest: digest,
    sourceBase: commit, candidate: commit.optional(), createdAt: timestamp, updatedAt: timestamp,
    approval: z.object({ owner: z.string().min(1), statement: z.string().min(1) }).strict(),
    attempts: z.array(attemptSchema), checks: z.array(resultSchema),
    review: z.object({ attemptId: id, record: reviewSchema, file: fileRecord }).strict().optional(),
    artifact: fileRecord.optional(), handoff: fileRecord.optional(), reworks: count, reportedTokens: count,
    unknownUsage: z.boolean(), elapsedMs: count, control: z.enum(['cancel', 'suspend']).optional(), suspended: z.boolean(),
    activeJob: z.object({ id, runtime: z.literal('macos-sandbox'), kind: z.string().min(1), startedAt: timestamp, process: ownedProcess.optional() }).strict().optional(),
    supervisor: z.object({ launchId: id, process: ownedProcess, launchedAt: timestamp }).strict().optional(),
    specifications: z.array(specificationSchema).optional(), tickets: z.array(ticketSchema).optional(), questionBatches: z.array(questionBatchSchema).optional(),
    lastEvent: eventSchema, history: z.array(eventSchema).min(1),
}).strict();
export type State = z.infer<typeof baseStateSchema>;
export const stateSchema = baseStateSchema.superRefine((v, c) => {
    for (const records of [v.specifications ?? [], v.tickets ?? [], v.questionBatches ?? []])
        if (new Set(records.map(record => record.id)).size !== records.length)
            c.addIssue({ code: 'custom', message: 'Duplicate collaboration identity' });
    for (const spec of v.specifications ?? [])
        if (spec.requirements.some(requirement => !v.project.requirements.includes(requirement)))
            c.addIssue({ code: 'custom', message: 'Unknown specification requirement' });
    for (const ticket of v.tickets ?? []) {
        if (ticket.requirements.some(requirement => !v.project.requirements.includes(requirement)) ||
            ticket.specificationIds.some(spec => !v.specifications?.some(record => record.id === spec)) ||
            ticket.attemptIds.some(attempt => !v.attempts.some(record => record.id === attempt)))
            c.addIssue({ code: 'custom', message: 'Unknown ticket reference' });
        if (ticket.github && !ticket.github.url.endsWith('/issues/' + ticket.github.number))
            c.addIssue({ code: 'custom', message: 'GitHub ticket identity mismatch' });
        if (ticket.status === 'done' && ticketProgress(v, ticket) !== 'done')
            c.addIssue({ code: 'custom', message: 'Done ticket requires accepted candidate evidence' });
    }
    for (const batch of v.questionBatches ?? []) {
        const questionIds = new Set(batch.questions.map(question => question.id));
        if (questionIds.size !== batch.questions.length || batch.questions.some(question => question.affectedTicketIds.some(ticket => !v.tickets?.some(record => record.id === ticket))))
            c.addIssue({ code: 'custom', message: 'Invalid question references' });
        for (const question of batch.questions) {
            const images = question.visualEvidence ?? [];
            if (new Set(images.map(image => image.id)).size !== images.length ||
                images.some(image => image.path !== `visual/${batch.id}/${question.id}/${image.id}.${image.mimeType === 'image/png' ? 'png' : 'jpg'}`))
                c.addIssue({ code: 'custom', message: 'Invalid visual evidence reference' });
        }
        if (batch.answer && (batch.answer.answers.length !== batch.questions.length || new Set(batch.answer.answers.map(answer => answer.questionId)).size !== questionIds.size ||
            batch.answer.answers.some(answer => !questionIds.has(answer.questionId) || batch.questions.some(question => question.id === answer.questionId && question.options.length > 0 && !question.options.includes(answer.value)))))
            c.addIssue({ code: 'custom', message: 'Answers must cover the entire batch with allowed values' });
    }
    if (v.history.length !== v.revision || v.history.some((e, i) => e.sequence !== i + 1) || JSON.stringify(v.lastEvent) !== JSON.stringify(v.history.at(-1)))
        c.addIssue({ code: 'custom', message: 'State event history conflict' });
    if (v.status === 'awaiting_input' && !v.priorStatus)
        c.addIssue({ code: 'custom', message: 'Awaiting input requires a prior state' });
    if (['candidate', 'verifying', 'verified', 'handoff_ready'].includes(v.status) && !v.candidate)
        c.addIssue({ code: 'custom', message: 'State requires a candidate' });
    if (new Set(v.attempts.map(a => a.id)).size !== v.attempts.length)
        c.addIssue({ code: 'custom', message: 'Duplicate attempt identity' });
});
export type WorkerChoice = z.infer<typeof choice>;
export type FileRecord = z.infer<typeof fileRecord>;
export type Result = z.infer<typeof resultSchema>;
export type Attempt = z.infer<typeof attemptSchema>;
export type Event = z.infer<typeof eventSchema>;
export function ticketProgress(state: State, ticket: z.infer<typeof ticketSchema>): z.infer<typeof ticketSchema>['status'] {
    const waiting = state.questionBatches?.some(batch => (!batch.answer || state.status === 'awaiting_input') &&
        batch.questions.some(question => question.affectedTicketIds.includes(ticket.id)));
    if (waiting || ['failed', 'cancelled', 'changes_requested'].includes(state.status)) return 'blocked';
    const accepted = ['verified', 'handoff_ready'].includes(state.status) && state.candidate &&
        state.review?.record.verdict === 'pass' && state.review.record.candidate === state.candidate && state.review.record.specDigest === state.specDigest &&
        ticket.requirements.every(requirement => state.review?.record.requirements.includes(requirement) &&
            state.project.checks.filter(check => check.requirements.includes(requirement)).every(check =>
                state.checks.some(result => result.id === check.id && result.passed && result.candidate === state.candidate && result.specDigest === state.specDigest)));
    if (accepted) return 'done';
    const attempts = state.attempts.filter(attempt => ticket.attemptIds.includes(attempt.id));
    if (attempts.some(attempt => attempt.status === 'running')) return 'in_progress';
    const latest = attempts.toSorted((a, b) => b.startedAt.localeCompare(a.startedAt)).at(0);
    if (latest?.status === 'failed' || latest?.status === 'interrupted') return 'blocked';
    return latest ? 'in_progress' : 'todo';
}
