import { z } from 'zod';
import { closeSync, fstatSync, openSync, readSync } from 'node:fs';
import { choice as choiceSchema, role as roleSchema, headroomSchema, usageSchema } from './contracts.ts';
import type { Role, WorkerChoice, Headroom } from './contracts.ts';
export type WorkerOutput = {
    text: string;
    inputTokens: number | null;
    outputTokens: number | null;
    cachedInputTokens: number | null;
    model: string | null;
    failed: boolean;
    reason: string | null;
    raw: Record<string, number> | null;
    session?: string;
    contextWindows?: Record<string, number>;
};
/** The provider's usage object, its integer fields copied verbatim. */
export type RawUsage = Record<string, number>;
/** `input` is gross (it already includes cached tokens); `cached` is the portion served from cache. */
export type Usage = z.infer<typeof usageSchema>;
const usageCount = z.number().int().nonnegative().safe();
const codexUsageSchema = z.object({ input_tokens: usageCount, cached_input_tokens: usageCount, output_tokens: usageCount });
const claudeUsageSchema = z.object({ input_tokens: usageCount, cache_creation_input_tokens: usageCount,
    cache_read_input_tokens: usageCount, output_tokens: usageCount });
/** Cache reads are worth a tenth of a fresh token (Q1). */
export function meteredTokens(usage: Usage): number {
    return (usage.input - usage.cached) + usage.output + Math.ceil(usage.cached * 0.1);
}
export function normalize(provider: WorkerChoice['provider'], raw: RawUsage): Usage {
    if (provider === 'codex') {
        const counts = codexUsageSchema.parse(raw);
        return usageSchema.parse({ input: counts.input_tokens, cached: counts.cached_input_tokens, output: counts.output_tokens });
    }
    const counts = claudeUsageSchema.parse(raw);
    return usageSchema.parse({
        input: counts.input_tokens + counts.cache_creation_input_tokens + counts.cache_read_input_tokens,
        cached: counts.cache_read_input_tokens, output: counts.output_tokens,
    });
}
const writeRoles = new Set<Role>(['business', 'domain', 'architect', 'developer', 'tester']);
// Execute these commands only through LocalRuntime. Its outer macOS sandbox
// owns filesystem permissions; macOS rejects a second nested Seatbelt sandbox.
export function workerCommand(choice: WorkerChoice, role: Role, cwd: string, prompt: string, policy: string, headroom?: Headroom, outputSchema?: { json: string; file: string }, resume?: { session: string }): {
    executable: string;
    args: string[];
    stdin: string;
    env: Record<string, string>;
} {
    const routing = headroom === undefined ? undefined : headroomSchema.parse(headroom);
    if (choice.provider !== 'codex' && choice.provider !== 'claude') {
        throw new Error('Unknown worker provider');
    }
    if (!roleSchema.safeParse(role).success)
        throw new Error('Unknown worker role');
    if (!choiceSchema.shape.effort.safeParse(choice.effort).success)
        throw new Error('Unknown worker effort');
    if (!choice.model.trim())
        throw new Error('Worker model must be explicit');
    if (!policy.trim())
        throw new Error('Worker policy must be explicit');
    if (choice.provider === 'codex') {
        return {
            executable: 'codex',
            args: [
                'exec', '--json', '--ignore-user-config', '--ignore-rules',
                '--disable', 'multi_agent',
                '--disable', 'apps',
                '--disable', 'skill_search',
                '--enable', 'skip_host_skill_discovery',
                ...(routing ? ['-c', `openai_base_url=${JSON.stringify(routing.baseUrl)}`] : []),
                '--model', choice.model,
                '-c', `model_reasoning_effort="${choice.effort}"`,
                '-c', 'approval_policy="never"',
                '-c', 'project_doc_max_bytes=0',
                '-c', 'model_auto_compact_token_limit=1000000000',
                '--sandbox', 'danger-full-access',
                '-C', cwd,
                ...(outputSchema ? ['--output-schema', outputSchema.file] : []),
                ...(resume ? ['resume', resume.session] : []),
                '-',
            ],
            stdin: `Worker policy:\n${policy}\n\nTask:\n${prompt}`,
            env: { CODEX_CA_CERTIFICATE: '/private/etc/ssl/cert.pem', SSL_CERT_FILE: '/private/etc/ssl/cert.pem',
                ...(routing ? { OPENAI_BASE_URL: routing.baseUrl } : {}) },
        };
    }
    const tools = (writeRoles.has(role) ? 'Read,Glob,Grep,Edit,Write' : 'Read,Glob,Grep')
        + (role === 'developer' || role === 'tester' ? ',Bash' : '');
    return {
        executable: 'claude',
        args: [
            '--print', '--output-format', 'stream-json', '--verbose',
            '--safe-mode', '--restricted',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--disable-slash-commands',
            '--tools', tools,
            '--allowedTools', tools,
            '--permission-mode', 'dontAsk',
            '--permission-prompts', 'none',
            '--model', choice.model,
            '--effort', choice.effort,
            '--append-system-prompt', policy,
            ...(outputSchema ? ['--json-schema', outputSchema.json] : []),
            ...(resume ? ['--resume', resume.session] : []),
        ],
        stdin: prompt,
        // A dead upstream connection leaves a stream silent until the attempt timeout; fail it fast so the CLI retries.
        env: { CLAUDE_STREAM_IDLE_TIMEOUT_MS: '120000', DISABLE_AUTO_COMPACT: '1', DISABLE_COMPACT: '1',
            ...(routing ? { ANTHROPIC_BASE_URL: routing.baseUrl.slice(0, -'/v1'.length) } : {}) },
    };
}
function object(value: unknown): Record<string, unknown> | null {
    return value !== null && typeof value === 'object' && !Array.isArray(value)
        ? value as Record<string, unknown>
        : null;
}
function string(value: unknown): string | null {
    return typeof value === 'string' && value.length > 0 ? value : null;
}
function tokens(value: unknown): number | null {
    return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}
// Snake-case usage keys, with the camelCase modelUsage fallback for each.
const codexRawFields: [string, string][] = [
    ['input_tokens', 'input_tokens'], ['cached_input_tokens', 'cached_input_tokens'],
    ['cache_write_input_tokens', 'cache_write_input_tokens'], ['output_tokens', 'output_tokens'],
    ['reasoning_output_tokens', 'reasoning_output_tokens'],
];
const claudeRawFields: [string, string][] = [
    ['input_tokens', 'inputTokens'], ['cache_creation_input_tokens', 'cacheCreationInputTokens'],
    ['cache_read_input_tokens', 'cacheReadInputTokens'], ['output_tokens', 'outputTokens'],
];
function rawUsage(value: unknown, provider: WorkerChoice['provider']): RawUsage | null {
    const usage = object(value);
    if (!usage) return null;
    const raw: RawUsage = {};
    for (const [snake, camel] of provider === 'codex' ? codexRawFields : claudeRawFields) {
        const parsed = tokens(snake in usage ? usage[snake] : usage[camel]);
        if (parsed !== null) raw[snake] = parsed;
    }
    return Object.keys(raw).length ? raw : null;
}
function sumRaw(rows: RawUsage[]): RawUsage {
    const sum: RawUsage = {};
    for (const row of rows) for (const [key, value] of Object.entries(row)) sum[key] = (sum[key] ?? 0) + value;
    return sum;
}
function usageFields(value: unknown, provider: WorkerChoice['provider']): Pick<WorkerOutput, 'inputTokens' | 'outputTokens' | 'cachedInputTokens'> {
    const raw = rawUsage(value, provider);
    try {
        if (raw) {
            const usage = normalize(provider, raw);
            return { inputTokens: usage.input, outputTokens: usage.output, cachedInputTokens: usage.cached };
        }
    } catch { /* Invalid provider counts remain unknown. */ }
    return { inputTokens: null, outputTokens: null, cachedInputTokens: null };
}

function eventError(event: Record<string, unknown>): string | null {
    const error = object(event.error);
    return string(error?.message) ?? string(event.message) ?? string(event.error)
        ?? 'Worker reported an error';
}
export function parseWorkerOutput(provider: WorkerChoice['provider'], stdout: string): WorkerOutput {
    const output: WorkerOutput = {
        text: '', inputTokens: null, outputTokens: null, cachedInputTokens: null,
        model: null, failed: false, reason: null, raw: null,
    };
    let completed = false;
    let malformed = false;
    const messages: string[] = [];
    for (const line of stdout.split(/\r?\n/)) {
        if (!line.trim())
            continue;
        let parsed: unknown;
        try {
            parsed = JSON.parse(line);
        }
        catch {
            malformed = true;
            continue;
        }
        const event = object(parsed);
        if (!event) {
            malformed = true;
            continue;
        }
        if (provider === 'codex') {
            if (event.type === 'thread.started') {
                const session = string(event.thread_id);
                if (session) output.session = session;
            }
            if (event.type === 'item.completed') {
                const item = object(event.item);
                if (item?.type === 'agent_message') {
                    const message = string(item.text);
                    if (message)
                        messages.push(message);
                }
            }
            else if (event.type === 'turn.completed') {
                completed = true;
                Object.assign(output, usageFields(event.usage, provider));
                output.raw = rawUsage(event.usage, provider);
            }
            else if (event.type === 'turn.failed' || event.type === 'error') {
                output.failed = true;
                output.reason ??= eventError(event);
            }
            const model = string(event.model);
            if (model)
                output.model = model;
            continue;
        }
        if (event.type === 'system' && event.subtype === 'init') {
            const session = string(event.session_id);
            if (session) output.session = session;
        }
        if (event.type === 'assistant') {
            const message = object(event.message);
            const model = string(message?.model);
            if (model)
                output.model = model;
            if (message?.usage && output.inputTokens === null) {
                Object.assign(output, usageFields(message.usage, provider));
            }
        }
        else if (event.type === 'result') {
            completed = true;
            const result = object(event.structured_output) ? JSON.stringify(event.structured_output) : string(event.result);
            if (result)
                output.text = result;
            if (event.usage) {
                Object.assign(output, usageFields(event.usage, provider));
                output.raw = rawUsage(event.usage, provider);
            }
            const modelUsage = object(event.modelUsage);
            if (modelUsage) {
                const models = Object.entries(modelUsage);
                for (const [model, value] of models) {
                    const window = tokens(object(value)?.contextWindow);
                    if (window !== null && window > 0) (output.contextWindows ??= {})[model] = window;
                }
                if (models.length === 1)
                    output.model = models[0][0];
                if (!event.usage) {
                    const totals = models.map(([, value]) => usageFields(value, provider));
                    for (const key of ['inputTokens', 'outputTokens', 'cachedInputTokens'] as const) {
                        const values = totals.map((total) => total[key]);
                        output[key] = values.every((value): value is number => value !== null)
                            ? tokens(values.reduce((sum, value) => sum + value, 0))
                            : null;
                    }
                    const rawRows = models.map(([, value]) => rawUsage(value, provider));
                    output.raw = rawRows.every((row): row is RawUsage => row !== null) ? sumRaw(rawRows) : null;
                }
            }
            if (event.is_error === true || event.subtype !== 'success') {
                output.failed = true;
                output.reason ??= string(event.result) ?? string(event.subtype) ?? 'Worker failed';
            }
        }
        else if (event.type === 'error' || event.type === 'rate_limit_event'
            && !string(object(event.rate_limit_info)?.status)?.startsWith('allowed')) {
            output.failed = true;
            output.reason ??= eventError(event);
        }
    }
    if (provider === 'codex')
        output.text = messages.at(-1) ?? '';
    if (malformed) {
        output.failed = true;
        output.reason ??= 'Malformed worker output';
    }
    if (!completed) {
        output.failed = true;
        output.reason ??= 'Worker output has no completion event';
    }
    if (!output.text.trim()) {
        output.failed = true;
        output.reason ??= 'Worker returned no final text';
    }
    return output;
}
export type MeterReading = { raw: RawUsage; usage: Usage; contextTokens: number | null; peakContext: number | null; contextMax: number | null; compacted: boolean };
export type Activity = { bytes: number; started: boolean; session: string | null };
/** Tails one Codex rollout or Claude capture log by byte offset. Never re-reads bytes already consumed. */
export class UsageMeter {
    private readonly provider: WorkerChoice['provider'];
    private readonly path: string;
    private offset = 0;
    private partial = '';
    private compacted = false;
    private latest: MeterReading | null = null;
    private readonly baseline: RawUsage | null;
    private readonly claudeUsageById = new Map<string, RawUsage>();
    private bytes = 0;
    private started = false;
    private session: string | null = null;
    constructor(provider: WorkerChoice['provider'], path: string, resume?: { offset: number; raw: RawUsage }) {
        this.provider = provider;
        this.path = path;
        this.offset = resume?.offset ?? 0;
        this.baseline = resume?.raw ?? null;
    }
    cursor(): { path: string; offset: number; raw: RawUsage } | null {
        if (!this.latest) return null;
        return { path: this.path, offset: this.offset - Buffer.byteLength(this.partial),
            raw: this.baseline ? sumRaw([this.baseline, this.latest.raw]) : this.latest.raw };
    }
    activity(): Activity { return { bytes: this.bytes, started: this.started, session: this.session }; }
    read(): MeterReading | null {
        let fd: number;
        try {
            fd = openSync(this.path, 'r');
        }
        catch (error) {
            if (error instanceof Error && 'code' in error && error.code === 'ENOENT' && this.offset === 0) return this.latest;
            throw error;
        }
        try {
            const size = fstatSync(fd).size;
            if (size > this.offset) {
                const buffer = Buffer.alloc(size - this.offset);
                const bytes = readSync(fd, buffer, 0, buffer.length, this.offset);
                this.offset += bytes;
                this.bytes += bytes;
                this.partial += buffer.toString('utf8', 0, bytes);
            }
        }
        finally {
            closeSync(fd);
        }
        const lines = this.partial.split('\n');
        this.partial = lines.pop() ?? '';
        for (const line of lines) {
            if (!line.trim())
                continue;
            let parsed: unknown;
            try {
                parsed = JSON.parse(line);
            }
            catch {
                continue;
            }
            const event = object(parsed);
            if (event)
                this.ingest(event);
        }
        return this.latest;
    }
    private ingest(event: Record<string, unknown>): void {
        if (this.provider === 'codex' && event.type === 'thread.started' && typeof event.thread_id === 'string' && event.thread_id) {
            this.started = true; this.session = event.thread_id;
        }
        if (this.provider === 'claude' && event.type === 'system' && event.subtype === 'init' && typeof event.session_id === 'string' && event.session_id) {
            this.started = true; this.session = event.session_id;
        }
        if (event.type === 'compacted' || (event.type === 'system' && event.subtype === 'compact_boundary')) {
            this.compacted = true;
            if (this.latest)
                this.latest = { ...this.latest, compacted: true };
            return;
        }
        if (this.provider === 'codex') {
            const payload = object(event.payload);
            if (payload?.type !== 'token_count')
                return;
            const info = object(payload.info);
            if (info?.total_token_usage == null) return;
            let raw = rawUsage(info.total_token_usage, 'codex');
            if (!raw) throw Error('Invalid Codex meter usage');
            if (this.baseline) raw = Object.fromEntries(Object.entries(raw).map(([key, value]) => [key, value - (this.baseline?.[key] ?? 0)]));
            const last = object(info?.last_token_usage);
            const input = tokens(last?.input_tokens);
            const output = tokens(last?.output_tokens);
            const contextTokens = input !== null && output !== null ? tokens(input + output) : null;
            this.latest = {
                raw, usage: normalize('codex', raw), contextTokens,
                peakContext: contextTokens === null ? this.latest?.peakContext ?? null : Math.max(this.latest?.peakContext ?? 0, contextTokens),
                contextMax: tokens(info?.model_context_window), compacted: this.compacted,
            };
            return;
        }
        if (event.type !== 'assistant')
            return;
        const message = object(event.message);
        const id = string(message?.id);
        const raw = rawUsage(message?.usage, 'claude');
        if (!id || message?.usage == null) return;
        if (!raw) throw Error('Invalid Claude meter usage');
        const current = normalize('claude', raw);
        const contextTokens = tokens(current.input + current.output);
        this.claudeUsageById.set(id, raw);
        const sum = sumRaw([...this.claudeUsageById.values()]);
        this.latest = { raw: sum, usage: normalize('claude', sum), contextTokens,
            peakContext: contextTokens === null ? this.latest?.peakContext ?? null : Math.max(this.latest?.peakContext ?? 0, contextTokens),
            contextMax: null, compacted: this.compacted };
    }
}
