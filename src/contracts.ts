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
export const projectSchema = z.object({
    schemaVersion: z.literal(2), name: id, repository: z.string().min(1), base: commit,
    recipient: z.string().min(1), brief: z.string().min(1), policies: z.array(z.string()).min(1),
    requirements: z.array(id).min(1), checks: z.array(check).min(1),
    artifact: z.string().min(1), artifactCheck: check,
    allowedPaths: z.array(z.string()).min(1),
    runtime: z.object({ kind: z.literal('macos-sandbox'), toolPaths: z.array(z.string().min(1)).min(1), network: z.enum(['none', 'loopback', 'outbound']), authHomes: z.object({ codex: z.string().nullable(), claude: z.string().nullable() }).strict() }).strict(),
    models: z.object({ coordinator: choice, developer: choice, reviewer: choice, inspector: choice }).strict(),
    limits: z.object({ maxAttempts: z.number().int().min(1).max(100), maxReworks: z.number().int().min(0).max(10), attemptTimeoutMs: z.number().int().positive().max(1800000), maxWallMs: z.number().int().positive(), maxReportedTokens: z.number().int().positive(), verificationReserveAttempts: z.number().int().min(1), maxLogBytes: z.number().int().min(1024).max(100000000) }).strict(),
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
    handoff_ready: ['changes_requested', 'awaiting_input', 'cancelled'], failed: [], cancelled: [],
};
export const reviewSchema = z.object({ schemaVersion: z.literal(1), candidate: commit, specDigest: digest, requirements: z.array(id), verdict: z.enum(['pass', 'changes_requested']), findings: z.array(z.object({ severity: z.enum(['blocking', 'minor']), requirement: id, message: z.string().min(1), evidence: z.string().min(1) }).strict()) }).strict();
export type Review = z.infer<typeof reviewSchema>;
const timestamp = z.string().datetime();
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
const attemptSchema = z.object({
    id, role, startedAt: timestamp, endedAt: timestamp.optional(), model: choice, candidate: commit,
    status: z.enum(['running', 'completed', 'failed', 'interrupted']), result: resultSchema.optional(),
    inputTokens: count.nullable().optional(), outputTokens: count.nullable().optional(), cachedInputTokens: count.nullable().optional(), handoff: fileRecord.optional(),
}).strict();
export const stateSchema = z.object({
    schemaVersion: z.literal(1), id, revision: z.number().int().positive(), status: z.enum(states),
    priorStatus: z.enum(states).optional(), reason: z.string().optional(), project: projectSchema,
    specDigest: digest, profileDigest: digest, bundleDigest: digest, policyDigest: digest,
    sourceBase: commit, candidate: commit.optional(), createdAt: timestamp, updatedAt: timestamp,
    approval: z.object({ owner: z.string().min(1), statement: z.string().min(1) }).strict(),
    attempts: z.array(attemptSchema), checks: z.array(resultSchema),
    review: z.object({ attemptId: id, record: reviewSchema, file: fileRecord }).strict().optional(),
    artifact: fileRecord.optional(), handoff: fileRecord.optional(), reworks: count, reportedTokens: count,
    unknownUsage: z.boolean(), elapsedMs: count, control: z.enum(['cancel', 'suspend']).optional(), suspended: z.boolean(),
    activeJob: z.object({ id, runtime: z.literal('macos-sandbox'), kind: z.string().min(1), startedAt: timestamp }).strict().optional(),
    lastEvent: eventSchema, history: z.array(eventSchema).min(1),
}).strict().superRefine((v, c) => {
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
export type State = z.infer<typeof stateSchema>;
