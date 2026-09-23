import { randomUUID } from 'node:crypto';
import { mkdir, rename, open, chmod, lstat } from 'node:fs/promises';
import { constants, mkdirSync, realpathSync } from 'node:fs';
import path from 'node:path';
import { z } from 'zod';
import { choice, id } from './contracts.ts';
import type { State } from './contracts.ts';
import { Store } from './store.ts';
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
  if (value.models.developer.provider !== 'codex') ctx.addIssue({ code: 'custom', path: ['models', 'developer', 'provider'], message: 'Developer requires Codex' });
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

function issue(code: string, message: string, recommendation: string, runId?: string): Issue {
  return { code, severity: 'warning', message, recommendation, ...(runId ? { runId } : {}) };
}

const knownReasons = new Set(['completed', 'cancelled', 'timeout', 'output_limit', 'capture_error', 'spawn_error', 'exit_error', 'failed']);
function safeReason(value: string | undefined) { return value && knownReasons.has(value) ? value : value ? 'recorded_failure' : null; }
function safeEvent(value: string) { return /^[a-z][a-z0-9_]{0,40}$/.test(value) ? value : 'recorded_event'; }
export function projectRun(state: State, settings: Settings = defaultSettings, redact: (value: string) => string = value => value) {
  const unknownUsage = state.unknownUsage || state.attempts.some(attempt => attempt.inputTokens == null || attempt.outputTokens == null);
  const attempts = state.attempts.map(attempt => ({
    id: attempt.id, role: attempt.role, model: { ...attempt.model, model: redact(attempt.model.model) },
    status: attempt.status, startedAt: attempt.startedAt,
    ...(attempt.endedAt ? { endedAt: attempt.endedAt } : {}),
    inputTokens: attempt.inputTokens ?? null, outputTokens: attempt.outputTokens ?? null,
    cachedInputTokens: attempt.cachedInputTokens ?? null, reason: safeReason(attempt.result?.reason),
  }));
  const issues: Issue[] = [];
  if (unknownUsage)
    issues.push(issue('usage_unknown', 'Some worker token use is unknown.', 'Review usage evidence before dispatching another attempt.', state.id));
  if (state.activeJob) issues.push(issue('job_unconfirmed', 'A job is recorded as active; live process health is unavailable.', 'Inspect captured evidence and reconcile the job before retrying.', state.id));
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
  if (state.attempts.some(attempt => (attempt.inputTokens ?? 0) >= settings.recovery.contextTokenThreshold))
    issues.push(issue('reported_input_threshold', 'An attempt reported input tokens above the configured threshold.', 'Review the attempt handoff before a new context. Current context occupancy is unknown.', state.id));
  if (state.status === 'awaiting_input') issues.push(issue('input_required', 'This run awaits an operator decision.', 'Review the last event and failed checks.', state.id));
  if (state.status === 'failed') issues.push(issue('run_failed', 'This run failed.', 'Review recorded attempts and checks.', state.id));
  return {
    id: state.id, status: state.status, revision: state.revision, updatedAt: state.updatedAt,
    repository: redact(state.project.repository), brief: redact(state.project.brief),
    coordinator: { ...state.project.models.coordinator, model: redact(state.project.models.coordinator.model) },
    reportedTokens: state.reportedTokens, unknownUsage, elapsedMs: state.elapsedMs,
    limits: { maxReportedTokens: state.project.limits.maxReportedTokens, maxAttempts: state.project.limits.maxAttempts,
      verificationReserveAttempts: state.project.limits.verificationReserveAttempts, maxWallMs: state.project.limits.maxWallMs,
      attemptTimeoutMs: state.project.limits.attemptTimeoutMs },
    attempts, events: state.history.map(({ sequence, at, type }) => ({ sequence, at, type: safeEvent(type) })), issues,
    candidate: state.candidate ?? null, checks: { passed: state.checks.filter(check => check.passed).length, total: state.checks.length },
    recoveryAvailable: true, controlPending: state.control ?? null,
  };
}

type Secrets = { github?: string; application: Record<string, string> };
export class Dashboard {
  readonly store: Store;
  readonly privateDir: string;
  private mutation = Promise.resolve();
  constructor(root: string) { mkdirSync(root, { recursive: true, mode: 0o700 }); const canonical = realpathSync(root); this.store = new Store(canonical); this.privateDir = path.join(canonical, '.dashboard'); }

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
      try { runs.push(projectRun(await this.store.read(run), settings, redact)); }
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
    return { generatedAt: new Date().toISOString(), capabilities: { execution: false, liveHeartbeat: false },
      settings: safeSettings, credentials, runs, issues };
  }
  async recovery(run: string) {
    id.parse(run);
    const state = await this.store.read(run);
    return {
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
  async control(run: string, value: unknown) {
    id.parse(run);
    const input = z.object({ action: z.enum(['suspend', 'cancel']), expectedRevision: positive }).strict().parse(value);
    await this.store.update(run, 'control_requested', { action: input.action }, state => {
      if (state.revision !== input.expectedRevision) throw new StaleRevision();
      state.control = input.action;
    });
    return { message: `${input.action} requested and recorded. No coordinator is running to confirm the worker has stopped.` };
  }
}

export class StaleRevision extends Error {}
function isMissing(error: unknown) { return error instanceof Error && 'code' in error && error.code === 'ENOENT'; }
