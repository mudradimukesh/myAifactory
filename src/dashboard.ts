import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdir, rename, open, chmod, lstat, readdir, readFile } from 'node:fs/promises';
import { constants, mkdirSync, realpathSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { z } from 'zod';
import { choice, id, ticketProgress, meterReadingSchema } from './contracts.ts';
import type { OwnedProcess, State, Segment, MeterReading } from './contracts.ts';
import { groupAlive, groupMembers, isOwnedAlive, terminateOwned } from './process.ts';
import { Store, move, reset, resettable } from './store.ts';
import { validateAuthHome } from './runtime.ts';

const finite = z.number().finite().nonnegative();
const positive = z.number().int().safe().positive();
const githubUrl = z.string().refine(value => /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\.git)?$/.test(value) && !value.includes('..'), 'Use an HTTPS GitHub owner/repository URL without credentials');
export const settingsSchema = z.object({
  repositoryUrl: z.union([z.literal(''), githubUrl]), brief: z.string().max(10000), recipient: z.string().max(300),
  authHomes: z.object({ codex: z.string().max(1000), claude: z.string().max(1000) }).strict(),
  models: z.object({ coordinator: choice, developer: choice, reviewer: choice, inspector: choice }).strict(),
  budget: z.object({ maxReportedTokens: positive, maxAttempts: positive.max(100), verificationReserveAttempts: positive.max(100),
    maxWallMinutes: positive.max(Math.floor(Number.MAX_SAFE_INTEGER / 60000)), attemptTimeoutMinutes: positive.max(30), applicationBudgetUsd: finite }).strict(),
  recovery: z.object({ staleMinutes: positive, maxFailures: positive, contextTokenThreshold: positive }).strict(),
}).strict().superRefine((value, ctx) => {
  if (value.budget.verificationReserveAttempts >= value.budget.maxAttempts) ctx.addIssue({ code: 'custom', path: ['budget', 'verificationReserveAttempts'], message: 'Verification reserve must be smaller than total attempts' });
  if (value.budget.attemptTimeoutMinutes > value.budget.maxWallMinutes) ctx.addIssue({ code: 'custom', path: ['budget', 'attemptTimeoutMinutes'], message: 'Attempt timeout exceeds wall budget' });
});
export type Settings = z.infer<typeof settingsSchema>;
export type Issue = { code: string; severity: 'warning' | 'error' | 'info'; message: string; recommendation: string; runId?: string };
export const defaultSettings: Settings = {
  repositoryUrl: '', brief: '', recipient: '', authHomes: { codex: '', claude: '' },
  models: {
    coordinator: { provider: 'codex', model: 'gpt-6-astra', effort: 'high' },
    developer: { provider: 'codex', model: 'gpt-6-sol', effort: 'high' },
    reviewer: { provider: 'codex', model: 'gpt-6-sol', effort: 'high' },
    inspector: { provider: 'codex', model: 'gpt-6-luna', effort: 'medium' },
  },
  budget: { maxReportedTokens: 200000, maxAttempts: 6, verificationReserveAttempts: 2, maxWallMinutes: 240, attemptTimeoutMinutes: 30, applicationBudgetUsd: 0 },
  recovery: { staleMinutes: 15, maxFailures: 3, contextTokenThreshold: 160000 },
};

async function privateDirectory(directory: string) {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const info = await lstat(directory);
  if (!info.isDirectory() || info.mode & 0o077) throw new Error('Private directory is unsafe');
}
async function privateRead(file: string) {
  await privateDirectory(path.dirname(file));
  const handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.mode & 0o077) throw new Error('Private file is unsafe');
    return await handle.readFile({ encoding: 'utf8' });
  } finally { await handle.close(); }
}
async function privateJson(file: string, data: unknown) {
  await privateDirectory(path.dirname(file));
  try { const info = await lstat(file); if (!info.isFile()) throw new Error('Private file is unsafe'); }
  catch (error) { if (!isMissing(error)) throw error; }
  const temporary = `${file}.${randomUUID()}.tmp`;
  const handle = await open(temporary, 'wx', 0o600);
  try { await handle.writeFile(JSON.stringify(data)); await handle.sync(); } finally { await handle.close(); }
  await rename(temporary, file);
  await chmod(file, 0o600);
}

export type FactoryState = 'idle' | 'running' | 'pausing' | 'paused' | 'resuming' | 'stopping' | 'exited' | 'terminal';
export type FactoryView = {
  state: FactoryState; supervisor: { pid: number; launchedAt: string } | null;
  activeJob: { id: string; kind: string; startedAt: string; frozen: boolean } | null;
  canStart: boolean; canPause: boolean; canStop: boolean; canReset: boolean; startLabel: 'Start' | 'Resume';
  reason: string | null; orphans: number[];
  activity: { job: string; summary: string; ageSeconds: number | null }[];
};
const factoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const terminal = (status: State['status']) => status === 'failed' || status === 'cancelled' || status === 'handoff_ready';
const roles = ['business', 'domain', 'architect', 'developer', 'reviewer', 'tester', 'coordinator'] as const;
const choiceFor = (state: State, role: typeof roles[number]) => role === 'developer' ? state.project.models.developer
  : role === 'reviewer' ? state.project.models.reviewer : role === 'coordinator' ? state.project.models.coordinator : state.project.models.inspector;
const exitDetail = z.object({ outcome: z.enum(['waiting', 'finished', 'cancelled', 'error']), message: z.string() }).passthrough();

/** One line per Codex or Claude stream-json event: what ran, never its text, reasoning, tool results, or prompts. */
function summarizeEvent(event: Record<string, unknown>): string[] {
  if (event.type === 'item.started' || event.type === 'item.completed') {
    const item = event.item as Record<string, unknown> | undefined;
    if (!item || typeof item !== 'object') return [];
    if (item.type === 'command_execution') {
      const command = Array.isArray(item.command) ? item.command.join(' ') : String(item.command ?? '');
      return [event.type === 'item.started' ? `running ${command}` : `ran ${command} (exit ${item.exit_code})`];
    }
    if (item.type === 'agent_message') return ['message'];
    return [String(item.type ?? 'event')];
  }
  if (event.type === 'turn.completed') return ['turn finished'];
  if (event.type === 'assistant') {
    const content = (event.message as Record<string, unknown> | undefined)?.content;
    if (!Array.isArray(content)) return [];
    return content.filter((block): block is Record<string, unknown> => Boolean(block) && typeof block === 'object' && block.type === 'tool_use')
      .map(block => {
        const input = block.input as Record<string, unknown> | undefined;
        const detail = [input?.file_path, input?.pattern, input?.command].find(value => typeof value === 'string');
        return detail !== undefined ? `${block.name} ${detail}` : String(block.name ?? 'tool');
      });
  }
  if (event.type === 'result') return ['finished'];
  return [];
}
const collapse = (value: string) => value.replace(/\s+/g, ' ').trim();
const clip = (value: string) => value.length > 160 ? `${value.slice(0, 160)}...` : value;
/** Reads only the tail of a possibly large capture log and returns up to the last 5 event summaries. */
async function tailEvents(file: string, maxBytes = 65536): Promise<string[]> {
  let handle;
  try { handle = await open(file, 'r'); }
  catch { return []; }
  try {
    const size = (await handle.stat()).size;
    const n = Math.min(size, maxBytes);
    const buffer = Buffer.alloc(n);
    await handle.read(buffer, 0, n, size - n);
    let text = buffer.toString('utf8');
    if (n < size) { const newline = text.indexOf('\n'); text = newline >= 0 ? text.slice(newline + 1) : ''; }
    const summaries: string[] = [];
    for (const line of text.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      let event: unknown;
      try { event = JSON.parse(trimmed); } catch { continue; }
      if (event && typeof event === 'object') summaries.push(...summarizeEvent(event as Record<string, unknown>));
    }
    return summaries.slice(-5);
  } catch { return []; } finally { await handle.close().catch(() => {}); }
}

/** Derives operator controls from stored facts plus a live ownership check of the recorded supervisor. */
export async function factoryView(state: State, redact: (value: string) => string = value => value, runDir?: string): Promise<FactoryView> {
  const live = state.supervisor !== undefined && await isOwnedAlive(state.supervisor.process);
  const factory: FactoryState = terminal(state.status) ? 'terminal'
    : state.control === 'cancel' ? 'stopping'
    : !live ? (state.supervisor ? 'exited' : 'idle')
    : state.control === 'suspend' && state.suspended ? 'paused'
    : state.control === 'suspend' ? 'pausing'
    : state.suspended ? 'resuming' : 'running';
  const job = state.activeJob;
  const jobLive = job?.process !== undefined && await isOwnedAlive(job.process);
  const orphans = job?.process && !jobLive ? await groupMembers(job.process.pgid) : [];
  const supervisorOrphans = state.supervisor && !live ? await groupMembers(state.supervisor.process.pgid) : [];
  const lastExit = state.history.findLast(event => event.type === 'supervisor_exited');
  const exit = lastExit ? exitDetail.safeParse(lastExit.detail) : null;
  const reason = factory === 'exited' ? 'The supervisor process ended without recording its exit. Start clears the stale record.'
    : factory === 'idle' && exit?.success && exit.data.outcome === 'error' ? redact(exit.data.message)
    : state.unknownUsage && !terminal(state.status) ? 'Some worker token use is unknown. Review usage before starting.'
    : state.reason ? redact(state.reason) : null;
  const startable = factory === 'idle' || factory === 'exited' || factory === 'pausing' || factory === 'paused';
  let activity: FactoryView['activity'] = [];
  if (runDir && job && job.kind !== 'check') {
    const file = path.join(runDir, 'attempts', job.id, 'capture', 'stdout.log');
    const summaries = await tailEvents(file);
    let ageSeconds: number | null = null;
    try { ageSeconds = Math.max(0, Math.round((Date.now() - (await lstat(file)).mtimeMs) / 1000)); } catch { /* advisory */ }
    activity = summaries.map((summary, index) => ({ job: job.id, summary: clip(redact(collapse(summary.replace(/\S*\/attempts\/[^/\s]+\/source\//g, '')))), ageSeconds: index === summaries.length - 1 ? ageSeconds : null }));
  }
  return {
    state: factory,
    supervisor: state.supervisor ? { pid: state.supervisor.process.pid, launchedAt: state.supervisor.launchedAt } : null,
    activeJob: job ? { id: job.id, kind: job.kind, startedAt: job.startedAt, frozen: live && state.suspended } : null,
    canStart: startable && !state.unknownUsage,
    canPause: factory === 'running' || factory === 'resuming' || ((factory === 'idle' || factory === 'exited') && state.control !== 'suspend'),
    canStop: factory !== 'terminal' || live || jobLive || orphans.length > 0 || supervisorOrphans.length > 0,
    canReset: resettable(state) && !state.activeJob && !live && !jobLive && !state.control && orphans.length === 0 && supervisorOrphans.length === 0,
    startLabel: state.control === 'suspend' || state.suspended ? 'Resume' : 'Start',
    reason, orphans, activity,
  };
}

function issue(code: string, message: string, recommendation: string, runId?: string): Issue {
  return { code, severity: 'warning', message, recommendation, ...(runId ? { runId } : {}) };
}

const knownReasons = new Set(['completed', 'cancelled', 'timeout', 'output_limit', 'capture_error', 'spawn_error', 'exit_error', 'failed',
    'token_limit', 'context_limit', 'compacted', 'handoff_failed', 'stall_start', 'stall_idle']);
function safeReason(value: string | undefined) { return value && knownReasons.has(value) ? value : value ? 'recorded_failure' : null; }
function safeEvent(value: string) { return /^[a-z][a-z0-9_]{0,40}$/.test(value) ? value : 'recorded_event'; }
type UsageRow = Pick<Segment, 'kind' | 'model' | 'raw' | 'input' | 'cached' | 'output' | 'metered'> & {
  attemptId: string; segment: number | null; status: string; reason: string | null; live: boolean;
  context?: Pick<MeterReading, 'contextMax' | 'trigger' | 'contextTokens' | 'peakContext'> & { progress: number | null };
  providerStartedAt?: string | null; lastActivityAt?: string | null;
};
function usageSum(rows: UsageRow[]) {
  const raw: Record<string, number> = {};
  const lowerBound = { raw, input: 0, cached: 0, output: 0, metered: 0 };
  for (const row of rows) {
    for (const key of ['input', 'cached', 'output', 'metered'] as const) lowerBound[key] += row[key] ?? 0;
    for (const [key, value] of Object.entries(row.raw ?? {})) lowerBound.raw[key] = (lowerBound.raw[key] ?? 0) + value;
  }
  return { lowerBound, total: { raw: rows.some(row => row.raw === null) ? null : lowerBound.raw,
    input: rows.some(row => row.input === null) ? null : lowerBound.input,
    cached: rows.some(row => row.cached === null) ? null : lowerBound.cached,
    output: rows.some(row => row.output === null) ? null : lowerBound.output,
    metered: rows.some(row => row.metered === null) ? null : lowerBound.metered } };
}
export function projectRun(state: State, settings: Settings = defaultSettings, redact: (value: string) => string = value => value, factory?: FactoryView, meters: MeterReading[] = []) {
  const groups = new Map<State['attempts'][number]['role'], UsageRow[]>();
  for (const attempt of state.attempts) {
    const rows: UsageRow[] = (attempt.segments ?? []).map(segment => ({ attemptId: attempt.id, segment: segment.index,
      kind: segment.kind, model: redact(segment.model), status: segment.endedAt ? (segment.reason === 'completed' ? 'completed' : 'failed') : attempt.status,
      reason: safeReason(segment.reason), raw: segment.raw, input: segment.input, cached: segment.cached,
      output: segment.output, metered: segment.metered, live: false, providerStartedAt: segment.providerStartedAt ?? null, lastActivityAt: segment.lastActivityAt ?? null }));
    if (attempt.id === state.activeJob?.id) for (const meter of meters) {
      if (rows.some(row => row.segment === meter.segment)) continue;
      const stalled = meter.reason === 'stall_start' || meter.reason === 'stall_idle';
      const live = meter === meters.at(-1) && !stalled;
      rows.push({ attemptId: attempt.id, segment: meter.segment, kind: meter.kind, model: redact(meter.model),
        status: live ? 'running' : stalled ? 'failed' : 'completed', reason: safeReason(meter.reason), raw: meter.raw,
        input: meter.usage?.input ?? null, cached: meter.usage?.cached ?? null, output: meter.usage?.output ?? null,
        metered: meter.metered, live, providerStartedAt: meter.providerStartedAt ?? null, lastActivityAt: meter.lastActivityAt ?? null, ...(live ? { context: { contextMax: meter.contextMax, trigger: meter.trigger,
          contextTokens: meter.contextTokens, peakContext: meter.peakContext,
          progress: meter.contextTokens !== null && meter.trigger !== null && meter.trigger > 0 ? meter.contextTokens / meter.trigger : null } } : {}) });
    }
    if (!rows.length) rows.push({ attemptId: attempt.id, segment: null, kind: 'work', model: redact(attempt.model.model),
      status: attempt.status, reason: safeReason(attempt.result?.reason), raw: null, input: attempt.inputTokens ?? null,
      cached: attempt.cachedInputTokens ?? null, output: attempt.outputTokens ?? null, metered: null, live: false });
    groups.set(attempt.role, [...(groups.get(attempt.role) ?? []), ...rows]);
  }
  const usageByRole = [...groups].map(([role, agents]) => ({ role, agents, ...usageSum(agents) }));
  const runUsage = usageSum(usageByRole.flatMap(group => group.agents));
  const unknownUsage = state.unknownUsage || runUsage.total.metered === null;
  const attempts = state.attempts.map(attempt => ({
    id: attempt.id, role: attempt.role, model: { ...attempt.model, model: redact(attempt.model.model) },
    status: attempt.status, startedAt: attempt.startedAt,
    ...(attempt.endedAt ? { endedAt: attempt.endedAt } : {}),
    inputTokens: attempt.inputTokens ?? null, outputTokens: attempt.outputTokens ?? null,
    cachedInputTokens: attempt.cachedInputTokens ?? null, reason: safeReason(attempt.result?.reason),
  }));
  const issues: Issue[] = [];
  if (state.attempts.some(attempt => attempt.segments?.some(segment => segment.contextWindowMismatch)))
    issues.push(issue('context_window_mismatch', 'A provider reported a different context window than configured.', 'Review the model context configuration before further work.', state.id));
  if (unknownUsage)
    issues.push(issue('usage_unknown', 'Some worker token use is unknown.', 'Review usage evidence before dispatching another attempt.', state.id));
  for (const attempt of state.attempts) for (const segment of attempt.segments ?? []) if (segment.reason === 'stall_start' || segment.reason === 'stall_idle')
    issues.push(issue(segment.reason, `Worker stopped with ${segment.reason} on attempt ${attempt.id}, segment ${segment.index}.`, segment.reason === 'stall_idle'
      ? 'Inspect the last provider/tool event and capture. Resolve the blockage, reconcile usage, or raise workerIdleTimeoutMs in the run profile if the command is expected to run longer.'
      : 'Inspect capture, Headroom reachability and provider startup configuration, then reconcile unknown usage before a bounded retry.', state.id));
  const supervised = factory !== undefined && ['running', 'pausing', 'paused', 'resuming'].includes(factory.state);
  if (state.activeJob && !supervised) issues.push(issue('job_unconfirmed', 'A job is recorded as active; live process health is unavailable.', 'Inspect captured evidence and reconcile the job before retrying.', state.id));
  if (state.activeJob && Date.now() - Date.parse(state.activeJob.startedAt) > state.project.limits.attemptTimeoutMs)
    issues.push(issue('job_stale', 'The recorded job has exceeded its attempt timeout; process health is unconfirmed.', 'Inspect capture and reconcile ownership before retrying.', state.id));
  if (Date.now() - Date.parse(state.updatedAt) > settings.recovery.staleMinutes * 60000 && ['running', 'verifying'].includes(state.status))
    issues.push(issue('state_stale', 'The run record has not changed within the configured interval; live activity is unknown.', 'Inspect the latest capture before deciding whether to recover.', state.id));
  if (state.reportedTokens >= state.project.limits.maxReportedTokens)
    issues.push(issue('token_limit', 'The run has reached its local reported token limit.', 'Review usage and adjust the approved project profile before further work.', state.id));
  if (state.elapsedMs >= state.project.limits.maxWallMs)
    issues.push(issue('wall_limit', 'The run has reached its local wall-time allowance.', 'Review the recovery brief before allocating more time.', state.id));
  if (state.attempts.length >= state.project.limits.maxAttempts)
    issues.push(issue('attempt_limit', 'The run has used its configured attempt allowance.', 'Review failed evidence and make a new explicit budget decision.', state.id));
  if (state.attempts.length >= state.project.limits.maxAttempts - state.project.limits.verificationReserveAttempts && !['verifying', 'verified', 'handoff_ready'].includes(state.status))
    issues.push(issue('verification_reserve', 'The remaining attempt allowance is reserved for verification.', 'Review failed attempts before allocating more implementation work.', state.id));
  if (state.attempts.filter(attempt => attempt.status === 'failed').length >= settings.recovery.maxFailures)
    issues.push(issue('repeated_failures', 'Repeated attempts failed.', 'Review the recovery brief, then consider a smaller task or a different configured model.', state.id));
  const liveContext = usageByRole.flatMap(group => group.agents).find(agent => agent.live)?.context;
  if (liveContext?.contextTokens != null && liveContext.trigger != null) {
    if (liveContext.contextTokens >= liveContext.trigger)
      issues.push(issue('reported_input_threshold', `Current context ${liveContext.contextTokens} has reached the hand-off trigger ${liveContext.trigger}.`, 'Review the running segment and its hand-off progress.', state.id));
  } else if (state.attempts.some(attempt => (attempt.inputTokens ?? 0) >= settings.recovery.contextTokenThreshold))
    issues.push(issue('reported_input_threshold', 'An attempt reported input tokens above the configured threshold.', 'Review the attempt handoff before a new context. Current context occupancy is unknown.', state.id));
  if (state.status === 'awaiting_input') {
    const answered = Boolean(state.questionBatches?.length) && state.questionBatches?.every(batch => batch.answer);
    issues.push(answered
      ? issue('answers_pending_review', 'Answers await coordinator review.', 'Reconcile the recorded answers with the approved specification before resuming work.', state.id)
      : issue('input_required', 'This run awaits an operator decision.', 'Review the consequential questions and their impact before answering.', state.id));
  }
  if (state.status === 'failed') issues.push(issue('run_failed', 'This run failed.', 'Review recorded attempts and checks.', state.id));
  return {
    id: state.id, status: state.status, revision: state.revision, updatedAt: state.updatedAt,
    repository: redact(state.project.repository), brief: redact(state.project.brief),
    coordinator: { ...state.project.models.coordinator, model: redact(state.project.models.coordinator.model) },
    roles: roles.map(role => ({ role, provider: choiceFor(state, role).provider, model: redact(choiceFor(state, role).model) })),
    reportedTokens: state.reportedTokens, unknownUsage, elapsedMs: state.elapsedMs,
    usageByRole, usageTotal: runUsage.total, usageLowerBound: runUsage.lowerBound,
    limits: { maxReportedTokens: state.project.limits.maxReportedTokens, maxAttempts: state.project.limits.maxAttempts,
      verificationReserveAttempts: state.project.limits.verificationReserveAttempts, maxWallMs: state.project.limits.maxWallMs,
      attemptTimeoutMs: state.project.limits.attemptTimeoutMs },
    attempts, events: state.history.map(({ sequence, at, type }) => ({ sequence, at, type: safeEvent(type) })), issues,
    candidate: state.candidate ?? null, checks: { passed: state.checks.filter(check => check.passed).length, total: state.checks.length },
    specifications: (state.specifications ?? []).map(spec => ({ ...spec, title: redact(spec.title), content: redact(spec.content),
      approval: { ...spec.approval, owner: redact(spec.approval.owner), statement: redact(spec.approval.statement) } })),
    tickets: (state.tickets ?? []).map(ticket => ({ ...ticket, status: ticketProgress(state, ticket), title: redact(ticket.title), architectNote: redact(ticket.architectNote) })),
    questionBatches: (state.questionBatches ?? []).map(batch => ({ ...batch, title: redact(batch.title),
      questions: batch.questions.map(({ visualEvidence, ...question }) => ({ ...question, prompt: redact(question.prompt), owner: redact(question.owner),
        impact: redact(question.impact), recommendation: redact(question.recommendation), options: question.options.map(redact),
        ...(visualEvidence ? { visualEvidence: visualEvidence.filter(image => image.candidate === state.candidate && image.specDigest === state.specDigest)
          .map(image => ({ id: image.id, label: redact(image.label), sourceId: image.sourceId,
            url: `/api/runs/${state.id}/questions/${batch.id}/${question.id}/images/${image.id}` })),
          visualEvidenceStale: visualEvidence.some(image => image.candidate !== state.candidate || image.specDigest !== state.specDigest) } : {}) })),
      ...(batch.answer ? { answer: { ...batch.answer, owner: redact(batch.answer.owner), answers: batch.answer.answers.map(answer => ({ ...answer, value: redact(answer.value) })) } } : {}) })),
    recoveryAvailable: true, ...(factory ? { factory } : {}),
  };
}

type Secrets = { github?: string; application: Record<string, string> };
export class Dashboard {
  readonly store: Store;
  readonly privateDir: string;
  private mutation = Promise.resolve();
  private readonly githubFetch: typeof fetch;
  private readonly supervisorCommand: (root: string, run: string, launchId: string) => string[];
  constructor(root: string, githubFetch: typeof fetch = fetch, options: { supervisorCommand?: (root: string, run: string, launchId: string) => string[] } = {}) {
    this.supervisorCommand = options.supervisorCommand ?? ((stateRoot, run, launchId) => [process.execPath, path.join(factoryRoot, 'src', 'factory-cli.ts'), 'supervise', stateRoot, run, launchId]);
    this.githubFetch = githubFetch; mkdirSync(root, { recursive: true, mode: 0o700 }); const canonical = realpathSync(root); this.store = new Store(canonical); this.privateDir = path.join(canonical, '.dashboard'); }

  private async serialized<T>(fn: () => Promise<T>): Promise<T> {
    const result = this.mutation.then(fn);
    this.mutation = result.then(() => {}, () => {});
    return result;
  }
  async settings(): Promise<Settings> {
    try { return settingsSchema.parse(JSON.parse(await privateRead(path.join(this.privateDir, 'settings.json')))); }
    catch (error) { if (isMissing(error)) return defaultSettings; throw new Error('Stored settings unavailable'); }
  }
  async saveSettings(value: unknown) {
    const settings = settingsSchema.parse(value);
    await this.serialized(() => privateJson(path.join(this.privateDir, 'settings.json'), settings));
    return settings;
  }
  private async secrets(): Promise<Secrets> {
    try {
      const value = JSON.parse(await privateRead(path.join(this.privateDir, 'credentials.json')));
      return z.object({ github: z.string().optional(), application: z.record(z.string()) }).strict().parse(value);
    } catch (error) { if (isMissing(error)) return { application: {} }; throw new Error('Stored credentials unavailable'); }
  }
  async credentials() {
    const secrets = await this.secrets();
    return { github: Boolean(secrets.github), applicationKeys: Object.keys(secrets.application).sort() };
  }
  async saveCredential(value: unknown) {
    const input = z.discriminatedUnion('kind', [
      z.object({ kind: z.literal('github'), value: z.string().max(4096) }).strict(),
      z.object({ kind: z.literal('application'), name: id, value: z.union([z.literal(''), z.string().min(8).max(4096)]) }).strict(),
    ]).parse(value);
    if (input.kind === 'github' && input.value && !/^(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}$/.test(input.value)) throw new Error('Invalid GitHub token format');
    if (input.value.includes('\n') || input.value.includes('\r')) throw new Error('Credentials must be one line');
    return this.serialized(async () => {
      const secrets = await this.secrets();
      if (input.kind === 'github') {
        if (input.value) secrets.github = input.value;
        else delete secrets.github;
      } else if (input.value) secrets.application[input.name] = input.value;
      else delete secrets.application[input.name];
      await privateJson(path.join(this.privateDir, 'credentials.json'), secrets);
      return { github: Boolean(secrets.github), applicationKeys: Object.keys(secrets.application).sort() };
    });
  }
  async authCheck(value: unknown) {
    const { provider } = z.object({ provider: z.enum(['codex', 'claude']) }).strict().parse(value);
    const home = (await this.settings()).authHomes[provider];
    if (!home) return { ok: false, message: `${provider} subscription home is not configured.`, recommendation: provider === 'claude'
      ? 'Set a dedicated Claude credential-file home under Connections. The normal macOS Keychain login is not imported by this runtime.'
      : 'Set a dedicated subscription credential home under Connections.' };
    try { await validateAuthHome(provider, home); return { ok: true, message: `${provider} credential file has the expected subscription shape.`, recommendation: 'A live provider call is still required to confirm authentication and model access.' }; }
    catch { return { ok: false, message: `${provider} subscription credential home could not be validated.`, recommendation: provider === 'claude'
      ? 'This runtime requires a dedicated .credentials.json file; the normal macOS Keychain login alone is unsupported.'
      : 'Check that the dedicated home contains only the subscription credential file.' }; }
  }
  async snapshot() {
    const [settings, credentials, ids] = await Promise.all([this.settings(), this.credentials(), this.store.runs()]);
    const secrets = await this.secrets();
    const secretValues = [secrets.github, ...Object.values(secrets.application)]
      .filter((value): value is string => Boolean(value)).sort((a, b) => b.length - a.length);
    const redact = (value: string) => secretValues.reduce((result, secret) => result.replaceAll(secret, '[REDACTED]'), value);
    const runs = [];
    const issues: Issue[] = [];
    for (const run of ids) {
      try {
        const state = await this.store.read(run);
        const meters: MeterReading[] = [];
        if (state.activeJob && state.activeJob.kind !== 'check') {
          const root = path.join(this.store.dir(run), 'attempts', state.activeJob.id);
          let directories: string[] = [];
          try { directories = (await readdir(root, { withFileTypes: true })).filter(entry => entry.isDirectory() && /^segment-[1-9][0-9]*$/.test(entry.name)).map(entry => entry.name); }
          catch (error) { if (!isMissing(error)) throw error; }
          directories.sort((a, b) => Number(a.slice(8)) - Number(b.slice(8)));
          for (const directory of directories) {
            try {
              const meter = meterReadingSchema.parse(JSON.parse(await readFile(path.join(root, directory, 'meter.json'), 'utf8')));
              if (meter.segment !== Number(directory.slice(8))) throw Error('Meter segment does not match directory');
              meters.push(meter);
            } catch (error) { if (!isMissing(error)) throw error; }
          }
        }
        runs.push(projectRun(state, settings, redact, await factoryView(state, redact, this.store.dir(run)), meters));
      }
      catch { issues.push({ code: 'run_corrupt', severity: 'error', message: `Run ${run} cannot be read safely.`, recommendation: 'Inspect state and event records; do not dispatch or overwrite this run.', runId: run }); }
    }
    runs.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
    const safeSettings: Settings = { ...settings, repositoryUrl: redact(settings.repositoryUrl), brief: redact(settings.brief),
      recipient: redact(settings.recipient), authHomes: { codex: redact(settings.authHomes.codex), claude: redact(settings.authHomes.claude) },
      models: {
        coordinator: { ...settings.models.coordinator, model: redact(settings.models.coordinator.model) },
        developer: { ...settings.models.developer, model: redact(settings.models.developer.model) },
        reviewer: { ...settings.models.reviewer, model: redact(settings.models.reviewer.model) },
        inspector: { ...settings.models.inspector, model: redact(settings.models.inspector.model) },
      } };
    return { generatedAt: new Date().toISOString(), capabilities: { execution: true, liveHeartbeat: false, githubSync: true },
      settings: safeSettings, credentials, runs, issues };
  }
  async recovery(run: string) {
    id.parse(run);
    const state = await this.store.read(run);
    const projected = (await this.snapshot()).runs.find(record => record.id === run);
    if (!projected) throw new Error('Run cannot be projected');
    return {
      specifications: projected.specifications, tickets: projected.tickets, questionBatches: projected.questionBatches,
      kind: 'recovery_brief', runId: state.id, status: state.status, revision: state.revision,
      sourceBase: state.sourceBase, candidate: state.candidate ?? null, specDigest: state.specDigest,
      counters: { attempts: state.attempts.length, reworks: state.reworks, reportedTokens: state.reportedTokens,
        unknownUsage: state.unknownUsage || state.attempts.some(a => a.inputTokens == null || a.outputTokens == null), elapsedMs: state.elapsedMs },
      attempts: state.attempts.map(a => ({ id: a.id, role: a.role, status: a.status, startedAt: a.startedAt,
        endedAt: a.endedAt ?? null, candidate: a.candidate, result: a.result ? { reason: safeReason(a.result.reason),
          passed: a.result.passed, stdoutSha256: a.result.stdout.sha256, stderrSha256: a.result.stderr.sha256 } : null,
        handoffSha256: a.handoff?.sha256 ?? null })),
      checks: state.checks.map(c => ({ id: c.id, candidate: c.candidate, passed: c.passed, reason: safeReason(c.reason),
        stdoutSha256: c.stdout.sha256, stderrSha256: c.stderr.sha256 })),
      artifactSha256: state.artifact?.sha256 ?? null, handoffSha256: state.handoff?.sha256 ?? null,
      events: state.history.map(e => ({ sequence: e.sequence, at: e.at, type: safeEvent(e.type) })),
      unknowns: ['Live worker health and external side effects cannot be inferred from saved state.',
        'This recovery brief is not a verified release handoff.'],
      nextAction: state.activeJob ? 'Reconcile the recorded job and external effects before retrying.' : 'Review failed evidence and resume through a future coordinator.',
    };
  }
  async answerQuestions(run: string, value: unknown) {
    id.parse(run);
    const input = z.object({ expectedRevision: positive, batchId: id, owner: z.string().trim().min(1).max(300),
      answers: z.array(z.object({ questionId: id, value: z.string().trim().min(1).max(10000) }).strict()).min(1),
    }).strict().parse(value);
    await this.store.update(run, 'questions_answered', { batchId: input.batchId, owner: input.owner }, async state => {
      if (state.revision !== input.expectedRevision) throw new StaleRevision();
      const batch = state.questionBatches?.find(record => record.id === input.batchId);
      if (!batch || batch.answer) throw new CollaborationError('Question batch is unavailable');
      if (['cancelled', 'failed', 'handoff_ready'].includes(state.status)) throw new CollaborationError('Run cannot receive answers');
      if (batch.questions.some(question => question.visualEvidence?.length) && input.owner !== state.approval.owner)
        throw new CollaborationError('Visual answer owner does not match approval owner');
      for (const question of batch.questions) for (const image of question.visualEvidence ?? []) {
        if (image.candidate !== state.candidate || image.specDigest !== state.specDigest)
          throw new CollaborationError('Visual evidence belongs to a different candidate or specification');
        if (input.answers.some(answer => answer.questionId === question.id && answer.value === 'approve'))
          await this.store.readVisualEvidence(run, batch.id, question.id, image);
      }
      batch.answer = { owner: input.owner, source: 'operator', answeredAt: new Date().toISOString(), answers: input.answers };
    });
    return { message: 'Answers recorded for the factory. Acceptance and execution status remain unchanged.' };
  }
  async visualImage(run: string, batchId: string, questionId: string, imageId: string) {
    id.parse(run); id.parse(batchId); id.parse(questionId); id.parse(imageId);
    const state = await this.store.read(run);
    const batch = state.questionBatches?.find(item => item.id === batchId);
    const question = batch?.questions.find(item => item.id === questionId);
    const image = question?.visualEvidence?.find(item => item.id === imageId);
    if (!image || image.candidate !== state.candidate || image.specDigest !== state.specDigest) return null;
    return this.store.readVisualEvidence(run, batchId, questionId, image);
  }
  async syncTickets(run: string, value: unknown) {
    id.parse(run);
    const input = z.object({ expectedRevision: positive }).strict().parse(value);
    const secrets = await this.secrets();
    const token = secrets.github;
    const redact = (value: string) => [token, ...Object.values(secrets.application)].filter((secret): secret is string => Boolean(secret)).sort((a, b) => b.length - a.length).reduce((result, secret) => result.replaceAll(secret, '[REDACTED]'), value);
    if (!token) throw new CollaborationError('GitHub credential is required');
    await this.store.update(run, 'tickets_synced', {}, async state => {
      if (state.revision !== input.expectedRevision) throw new StaleRevision();
      const repository = githubUrl.parse(state.project.repository).replace(/\.git$/, '').slice('https://github.com/'.length);
      const headers = { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28' };
      for (const ticket of state.tickets ?? []) {
        ticket.status = ticketProgress(state, ticket);
        const marker = `<!-- factory:${state.id}:${ticket.id} -->`;
        const specifications = (state.specifications ?? []).filter(spec => ticket.specificationIds.includes(spec.id));
        const body = [marker, `Requirements: ${ticket.requirements.join(', ')}`, `Progress: ${ticket.status}`, ticket.architectNote,
          ...specifications.map(spec => `## ${spec.kind}: ${spec.title} (revision ${spec.revision})\nApproved by ${spec.approval.owner}: ${spec.approval.statement}\nDigest: ${spec.digest}\n\n${spec.content}`)].join('\n\n');
        let number = ticket.github?.number;
        if (!number) {
          for (let page = 1; ; page++) {
            const response = await this.githubFetch(`https://api.github.com/repos/${repository}/issues?state=all&per_page=100&page=${page}`, { headers, redirect: 'error', signal: AbortSignal.timeout(15000) });
            if (!response.ok) throw new CollaborationError('GitHub ticket lookup failed');
            const issues = z.array(z.object({ number: positive, body: z.string().nullable(), pull_request: z.unknown().optional() })).parse(await response.json());
            number = issues.find(issue => !issue.pull_request && issue.body?.includes(marker))?.number;
            if (number || issues.length < 100) break;
          }
        }
        if (number) {
          const response = await this.githubFetch(`https://api.github.com/repos/${repository}/issues/${number}`, { headers, redirect: 'error', signal: AbortSignal.timeout(15000) });
          if (!response.ok) throw new CollaborationError('GitHub ticket ownership lookup failed');
          const existing = z.object({ number: positive, body: z.string().nullable(), pull_request: z.unknown().optional() }).parse(await response.json());
          if (existing.number !== number || existing.pull_request || !existing.body?.includes(marker)) throw new CollaborationError('GitHub ticket ownership mismatch');
        }
        const response = await this.githubFetch(`https://api.github.com/repos/${repository}/issues${number ? '/' + number : ''}`, {
          method: number ? 'PATCH' : 'POST', headers, redirect: 'error', signal: AbortSignal.timeout(15000),
          body: JSON.stringify({ title: redact(ticket.title), body: redact(body), state: ticket.status === 'done' ? 'closed' : 'open' }),
        });
        if (!response.ok) throw new CollaborationError('GitHub ticket sync failed');
        const result = z.object({ number: positive, html_url: z.string().url() }).parse(await response.json());
        if (result.html_url !== `https://github.com/${repository}/issues/${result.number}`) throw new CollaborationError('Unexpected GitHub ticket identity');
        ticket.github = { number: result.number, url: result.html_url, syncedAt: new Date().toISOString() };
      }
    });
    return { message: 'Tickets and approved specifications synced to GitHub.' };
  }
  async control(run: string, value: unknown): Promise<{ factory: FactoryView; changed: boolean; message: string }> {
    id.parse(run);
    const input = z.discriminatedUnion('action', [
      z.object({ action: z.literal('start'), expectedRevision: positive }).strict(),
      z.object({ action: z.literal('pause') }).strict(),
      z.object({ action: z.literal('stop') }).strict(),
      z.object({ action: z.literal('reset') }).strict(),
    ]).parse(value);
    const done = async (changed: boolean, message: string) => ({ factory: await factoryView(await this.store.read(run)), changed, message });
    if (input.action === 'reset') return this.store.lock(`launch-${run}`, async () => {
      const state = await this.store.read(run);
      if (!resettable(state)) throw new FactoryConflict('Only failed, cancelled, or execution-stage awaiting runs can be reset.');
      const liveSupervisor = state.supervisor?.process && await isOwnedAlive(state.supervisor.process);
      const liveJob = state.activeJob?.process && await isOwnedAlive(state.activeJob.process);
      const supervisorOrphans = state.supervisor && !liveSupervisor ? await groupMembers(state.supervisor.process.pgid) : [];
      const workerOrphans = state.activeJob?.process && !liveJob ? await groupMembers(state.activeJob.process.pgid) : [];
      if (liveSupervisor || liveJob || state.activeJob || state.control || supervisorOrphans.length || workerOrphans.length)
        throw new FactoryConflict('Reset requires a quiescent run with no live or unconfirmed processes.');
      await this.store.update(run, 'factory_reset', { from: state.status }, current => {
        reset(current, 'Operator reset the run');
        current.activeJob = undefined; current.supervisor = undefined; current.control = undefined; current.suspended = false; current.priorStatus = undefined;
      });
      return done(true, 'The run was reset and is ready to start.');
    });
    if (input.action === 'pause') {
      let changed = false;
      let supervised = false;
      const state = await this.store.read(run);
      if (!terminal(state.status) && !state.control)
        await this.store.update(run, 'factory_pause_requested', {}, async current => {
          if (terminal(current.status) || current.control) return;
          supervised = current.supervisor !== undefined && await isOwnedAlive(current.supervisor.process);
          if (!supervised && current.activeJob?.process && await isOwnedAlive(current.activeJob.process))
            throw new FactoryConflict('A worker started outside the dashboard is running. Pause cannot freeze it. Use Stop.');
          current.control = 'suspend';
          changed = true;
        });
      return done(changed, changed ? supervised
        ? 'Pause requested. The supervisor freezes running workers and starts no new work.'
        : 'Paused. Nothing was running.' : 'The factory is already paused, stopping, or finished.');
    }
    const launchLock = <T>(fn: () => Promise<T>) => this.store.lock(`launch-${run}`, fn).catch((error: unknown) => {
      if (error instanceof Error && 'code' in error && error.code === 'ELOCKED' && 'file' in error && path.basename(String(error.file)) === `launch-${run}`) throw new FactoryConflict('Another start or stop for this run is in progress.');
      throw error;
    });
    if (input.action === 'stop') return launchLock(async () => {
      const before = await this.store.read(run);
      const changed = await this.stop(run);
      return done(changed, !changed ? 'The run is already finished.' : terminal(before.status)
        ? `Remaining processes stopped. The run remains ${before.status}.` : 'The factory stopped and the run is cancelled.');
    });
    return this.serialized(() => launchLock(async () => {
      const state = await this.store.read(run);
      if (terminal(state.status)) throw new FactoryConflict(`The run is ${state.status}; a finished run cannot start.`);
      if (state.unknownUsage) throw new FactoryConflict('Some worker token use is unknown. Review usage before starting.');
      const holder = await this.store.holder();
      if (holder && holder !== run) throw new FactoryConflict(`Run ${holder} holds the state root.`);
      if (state.control === 'cancel') throw new FactoryConflict('The factory is stopping. Finish Stop first.');
      if (state.supervisor && await isOwnedAlive(state.supervisor.process)) {
        if (state.control !== 'suspend') return done(false, 'The factory is already running.');
        await this.store.update(run, 'factory_resume_requested', {}, current => { if (current.control === 'suspend') current.control = undefined; });
        return done(true, 'Resume requested. The supervisor continues the frozen workers.');
      }
      if (state.revision !== input.expectedRevision) throw new StaleRevision();
      // A job left by a lost supervisor is stopped only when its recorded identity is proven.
      // The record stays, so the coordinator moves the run to awaiting_input as an uncertain prior job.
      if (state.activeJob?.process && await terminateOwned(state.activeJob.process, 5000) !== 'gone')
        throw new FactoryConflict('A prior worker process group is still running. Stop the run and inspect its processes before starting.');
      const launchId = randomUUID();
      await this.store.update(run, 'factory_start_requested', { launchId }, current => {
        if (current.revision !== state.revision) throw new StaleRevision();
        current.control = undefined; current.suspended = false; current.supervisor = undefined;
      });
      await this.launch(run, launchId);
      return done(true, 'The factory supervisor started.');
    }));
  }
  private async launch(run: string, launchId: string) {
    const logDir = path.join(this.store.dir(run), 'supervisor');
    await mkdir(logDir, { recursive: true, mode: 0o700 });
    const logPath = path.join(logDir, `${launchId}.log`);
    const log = await open(logPath, 'wx', 0o600);
    const argv = this.supervisorCommand(this.store.root, run, launchId);
    const env = Object.fromEntries(['PATH', 'HOME', 'LANG', 'TMPDIR', 'USER', 'LOGNAME'].flatMap(name => process.env[name] ? [[name, process.env[name]]] : []));
    let exitCode: number | null = null;
    let exited = false;
    const child = spawn(argv[0], argv.slice(1), { cwd: factoryRoot, detached: true, stdio: ['ignore', log.fd, log.fd], env });
    child.once('exit', code => { exited = true; exitCode = code; });
    child.once('error', () => { exited = true; });
    await log.close();
    child.unref();
    const registered = (state: State) => state.supervisor?.launchId === launchId || state.history.some(event =>
      event.type === 'supervisor_started' && z.object({ launchId: z.string() }).passthrough().safeParse(event.detail).data?.launchId === launchId);
    const deadline = Date.now() + 5000;
    while (!exited && Date.now() < deadline) {
      if (registered(await this.store.read(run))) return;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (registered(await this.store.read(run))) return;
    // Our own unreaped child cannot have a reused pid, so signalling its group is safe.
    if (!exited && child.pid !== undefined) { try { process.kill(-child.pid, 'SIGKILL'); } catch { /* Exited meanwhile. */ } }
    throw new LaunchFailed(`The supervisor did not register${exited ? ` and exited with code ${exitCode}` : ' within 5 seconds'}. Log: ${logPath}`);
  }
  /** Returns false when the run is already finished and nothing remains to stop. */
  private async stop(run: string): Promise<boolean> {
    const state = await this.store.read(run);
    if (terminal(state.status) && !state.activeJob?.process && !state.supervisor?.process) return false;
    if (!terminal(state.status) && state.control !== 'cancel')
      await this.store.update(run, 'factory_stop_requested', {}, current => { if (!terminal(current.status)) current.control = 'cancel'; });
    const supervisor = state.supervisor?.process;
    if (supervisor && await isOwnedAlive(supervisor)) {
      try { process.kill(supervisor.pid, 'SIGTERM'); }
      catch (error) { if (!(error instanceof Error && 'code' in error && error.code === 'ESRCH')) throw error; }
      const deadline = Date.now() + 15000;
      while (Date.now() < deadline && await isOwnedAlive(supervisor))
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    const job = (await this.store.read(run)).activeJob?.process ?? state.activeJob?.process;
    if (supervisor) await terminateOwned(supervisor, 0);
    if (job) await terminateOwned(job, 2000);
    const survivors = (await Promise.all([supervisor, job].filter((owned): owned is OwnedProcess => owned !== undefined)
      .map(async owned => await groupAlive(owned.pgid) ? groupMembers(owned.pgid) : []))).flat();
    if (survivors.length) throw new StopIncomplete(`Processes still running after Stop: ${survivors.join(', ')}.`);
    const current = await this.store.read(run);
    if (!terminal(current.status) || current.activeJob || current.supervisor || current.control || current.suspended) {
      await this.store.update(run, current.status === 'failed' || current.status === 'handoff_ready' ? 'factory_processes_cleaned' : 'run_cancelled_recovered', {}, recovered => {
        const at = new Date().toISOString();
        for (const attempt of recovered.attempts.filter(attempt => attempt.status === 'running')) {
          attempt.status = 'interrupted'; attempt.endedAt = at; recovered.unknownUsage = true;
        }
        recovered.activeJob = undefined; recovered.supervisor = undefined;
        recovered.control = undefined; recovered.suspended = false;
        if (!terminal(recovered.status)) move(recovered, 'cancelled', 'Operator stopped the factory');
      });
    }
    await this.store.release(run);
    return !terminal(state.status) || Boolean(supervisor || job);
  }
}

export class FactoryConflict extends Error {}
export class LaunchFailed extends Error {}
export class StopIncomplete extends Error {}
export class CollaborationError extends Error {}
export class StaleRevision extends Error {}
function isMissing(error: unknown) { return error instanceof Error && 'code' in error && error.code === 'ENOENT'; }
