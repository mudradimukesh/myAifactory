import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import path from 'node:path';
import { z } from 'zod';
import { id, role } from './contracts.ts';
import type { Role, WorkerChoice } from './contracts.ts';
import { factoryView, privateJson, privateRead, type Dashboard } from './dashboard.ts';
import { LocalRuntime } from './runtime.ts';
import { parseWorkerOutput } from './workers.ts';
import { identify, terminateOwnedBatch } from './process.ts';

const message = z.object({ by: z.enum(['operator', 'agent']), text: z.string().min(1).max(12000), at: z.string().datetime() }).strict();
const transcript = z.object({ runId: id, role, messages: z.array(message).max(100) }).strict();
const input = z.object({ role, text: z.string().trim().min(1).max(4000) }).strict();

export class ChatUnavailable extends Error {}

export class AgentChat {
  private readonly dashboard: Dashboard;
  private readonly runtime: LocalRuntime;
  constructor(dashboard: Dashboard, runtime = new LocalRuntime()) { this.dashboard = dashboard; this.runtime = runtime; }

  private file(runId: string, selectedRole: Role) {
    return path.join(this.dashboard.privateDir, `chat-${runId}-${selectedRole}.json`);
  }

  async read(runId: string, selectedRole: Role) {
    id.parse(runId); role.parse(selectedRole);
    await this.dashboard.store.read(runId);
    const file = this.file(runId, selectedRole);
    try { return transcript.parse(JSON.parse(await privateRead(file))); }
    catch (error) {
      if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return { runId, role: selectedRole, messages: [] };
      throw error;
    }
  }

  async send(runId: string, value: unknown) {
    id.parse(runId);
    const { role: selectedRole, text } = input.parse(value);
    try { return await this.dashboard.store.lock(`launch-${runId}`, () => this.dashboard.store.lock('execution', async () => {
      const run = await this.dashboard.store.read(runId);
      if (run.status === 'handoff_ready' || run.shutdown) throw new ChatUnavailable('Chat is unavailable while the run is finishing or complete.');
      if (!run.project.headroom?.baseUrl) throw new ChatUnavailable('Chat requires a validated Headroom route.');
      if (run.project.enabledRoles && !run.project.enabledRoles.includes(selectedRole))
        throw new ChatUnavailable('This agent is disabled for the selected run.');
      const choice: WorkerChoice = run.project.models.roles?.[selectedRole]
        ?? (selectedRole === 'coordinator' ? run.project.models.coordinator
          : selectedRole === 'developer' ? run.project.models.developer
          : selectedRole === 'reviewer' ? run.project.models.reviewer
          : selectedRole === 'tester' ? run.project.models.tester ?? run.project.models.inspector
          : run.project.models.inspector);
      if (choice.provider !== 'claude') throw new ChatUnavailable('Chat is available for Claude roles only. Codex tool access cannot be disabled here.');
      if (!run.project.runtime.authHomes.claude) throw new ChatUnavailable('This run has no Claude subscription connection. Configure one before chatting.');
      const factory = await factoryView(run);
      if (await this.dashboard.store.holder() || !['idle', 'exited', 'terminal'].includes(factory.state) || factory.activeJob || factory.orphans.length)
        throw new ChatUnavailable('Pause or finish the factory run before starting a separate chat turn.');
      const record = await this.read(runId, selectedRole);
      if (record.messages.length >= 98) throw new ChatUnavailable('This chat has reached 98 messages.');
      const root = await mkdtemp('/private/tmp/factory-chat-');
      const dirs = Object.fromEntries(['workspace', 'policy', 'output', 'capture', 'scratch'].map(name => [name, path.join(root, name)]));
      try {
        await Promise.all(Object.values(dirs).map(dir => mkdir(dir, { mode: 0o700 })));
        let spawned = false;
        const history = record.messages.slice(-8).map(item => `${item.by === 'operator' ? 'Operator' : 'Agent'}: ${item.text}`).join('\n\n');
        const prompt = `You are the ${selectedRole} for saved run ${runId}. This is a separate conversation. You cannot inspect files or change the factory run. Answer using only the context below.\nProject: ${run.project.name}\nBrief: ${run.project.brief}\nRun status: ${run.status}\nPrior conversation:\n${history || '(none)'}\n\nOperator: ${text}`;
        const routing = run.project.headroom.baseUrl;
        let result: Awaited<ReturnType<LocalRuntime['execute']>>;
        try { result = await this.runtime.execute({
          id: `chat-${selectedRole}`, project: run.project, ...dirsToJob(dirs), provider: 'claude', readOnlySource: true,
          argv: ['claude', '--print', '--output-format', 'stream-json', '--verbose', '--safe-mode', '--restricted',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--disable-slash-commands',
            '--tools', '', '--allowedTools', '', '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
            '--model', choice.model, '--effort', choice.effort, '--append-system-prompt', 'Reply as the selected role. No tools or project file access are available.'],
          stdin: prompt,
          env: { CLAUDE_STREAM_IDLE_TIMEOUT_MS: '120000', DISABLE_AUTO_COMPACT: '1', DISABLE_COMPACT: '1',
            ...(routing ? { ANTHROPIC_BASE_URL: routing.slice(0, -'/v1'.length) } : {}) },
          network: run.project.runtime.network, timeoutMs: run.project.limits.attemptTimeoutMs,
          maxLogBytes: run.project.limits.maxLogBytes, signal: new AbortController().signal,
          onSpawn: async pid => {
            const owned = await identify(pid);
            if (!owned) return;
            spawned = true;
            await this.dashboard.store.update(runId, 'chat_process_recorded', { role: selectedRole, pid }, current => {
              current.ownedProcesses ??= [];
              current.ownedProcesses.push({ jobId: `chat-${selectedRole}`, segment: 1, process: owned });
            });
          },
        }); } finally { if (spawned) {
          const processes = (await this.dashboard.store.read(runId)).ownedProcesses?.filter(record => record.jobId === `chat-${selectedRole}`).map(record => record.process) ?? [];
          const outcomes = await terminateOwnedBatch(processes, 0);
          if (outcomes.some(outcome => outcome.result !== 'gone')) throw new ChatUnavailable('Chat process cleanup is incomplete.');
          await this.dashboard.store.update(runId, 'chat_process_exited', { role: selectedRole }, current => {
            current.ownedProcesses = (current.ownedProcesses ?? []).filter(record => record.jobId !== `chat-${selectedRole}`);
          });
        } }
        const output = parseWorkerOutput('claude', await readFile(path.join(dirs.capture, 'stdout.log'), 'utf8'));
        if (result.reason !== 'completed' || output.failed || !output.text.trim())
          throw new ChatUnavailable(`The Claude chat turn failed: ${output.reason ?? result.reason}.`);
        const at = new Date().toISOString();
        const next = transcript.parse({ ...record, messages: [...record.messages,
          { by: 'operator', text, at }, { by: 'agent', text: output.text.slice(0, 12000), at: new Date().toISOString() }] });
        await privateJson(this.file(runId, selectedRole), next);
        return next;
      } finally { await rm(root, { recursive: true, force: true }); }
    })); } catch (error) {
      if (error instanceof Error && 'code' in error && error.code === 'ELOCKED')
        throw new ChatUnavailable('The factory or another chat turn is using this run. Retry after it finishes.');
      throw error;
    }
  }
}

function dirsToJob(dirs: Record<string, string>) {
  return { workspace: dirs.workspace, policyDir: dirs.policy, outputDir: dirs.output, captureDir: dirs.capture, scratchDir: dirs.scratch };
}
