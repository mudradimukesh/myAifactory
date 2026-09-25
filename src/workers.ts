import { choice as choiceSchema, role as roleSchema, headroomSchema } from './contracts.ts';
import type { Role, WorkerChoice, Headroom } from './contracts.ts';
export type WorkerOutput = {
    text: string;
    inputTokens: number | null;
    outputTokens: number | null;
    cachedInputTokens: number | null;
    model: string | null;
    failed: boolean;
    reason: string | null;
};
const writeRoles = new Set<Role>(['business', 'domain', 'architect', 'developer', 'tester']);
// Execute these commands only through LocalRuntime. Its outer macOS sandbox
// owns filesystem permissions; macOS rejects a second nested Seatbelt sandbox.
export function workerCommand(choice: WorkerChoice, role: Role, cwd: string, prompt: string, policy: string, headroom?: Headroom, outputSchema?: { json: string; file: string }): {
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
                'exec', '--json', '--ephemeral', '--ignore-user-config', '--ignore-rules',
                '--disable', 'multi_agent',
                '--disable', 'apps',
                '--disable', 'skill_search',
                '--enable', 'skip_host_skill_discovery',
                ...(routing ? ['-c', `openai_base_url=${JSON.stringify(routing.baseUrl)}`] : []),
                '--model', choice.model,
                '-c', `model_reasoning_effort="${choice.effort}"`,
                '-c', 'approval_policy="never"',
                '-c', 'project_doc_max_bytes=0',
                '--sandbox', 'danger-full-access',
                '-C', cwd,
                ...(outputSchema ? ['--output-schema', outputSchema.file] : []),
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
            '--no-session-persistence', '--safe-mode', '--restricted',
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
        ],
        stdin: prompt,
        // A dead upstream connection leaves a stream silent until the attempt timeout; fail it fast so the CLI retries.
        env: { CLAUDE_STREAM_IDLE_TIMEOUT_MS: '120000', ...(routing ? { ANTHROPIC_BASE_URL: routing.baseUrl.slice(0, -'/v1'.length) } : {}) },
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
function usageFields(value: unknown, provider: WorkerChoice['provider']): Pick<WorkerOutput, 'inputTokens' | 'outputTokens' | 'cachedInputTokens'> {
    const usage = object(value);
    let inputTokens = tokens(usage?.input_tokens ?? usage?.inputTokens);
    if (provider === 'claude') {
        const created = tokens(usage && ('cache_creation_input_tokens' in usage ? usage.cache_creation_input_tokens : usage.cacheCreationInputTokens));
        const read = tokens(usage && ('cache_read_input_tokens' in usage ? usage.cache_read_input_tokens : usage.cacheReadInputTokens));
        inputTokens = inputTokens !== null && created !== null && read !== null
            ? tokens(inputTokens + created + read) : null;
    }
    return {
        inputTokens,
        outputTokens: tokens(usage?.output_tokens ?? usage?.outputTokens),
        cachedInputTokens: tokens(usage?.cached_input_tokens ?? usage?.cache_read_input_tokens ?? usage?.cacheReadInputTokens),
    };
}
function eventError(event: Record<string, unknown>): string | null {
    const error = object(event.error);
    return string(error?.message) ?? string(event.message) ?? string(event.error)
        ?? 'Worker reported an error';
}
export function parseWorkerOutput(provider: WorkerChoice['provider'], stdout: string): WorkerOutput {
    const output: WorkerOutput = {
        text: '', inputTokens: null, outputTokens: null, cachedInputTokens: null,
        model: null, failed: false, reason: null,
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
            if (event.usage)
                Object.assign(output, usageFields(event.usage, provider));
            const modelUsage = object(event.modelUsage);
            if (modelUsage) {
                const models = Object.entries(modelUsage);
                if (models.length === 1)
                    output.model = models[0][0];
                if (!event.usage) {
                    const totals = models.map(([, value]) => usageFields(value, provider));
                    for (const key of ['inputTokens', 'outputTokens', 'cachedInputTokens'] as const) {
                        const values = totals.map((total) => total[key]);
                        output[key] = values.every((value): value is number => value !== null)
                            ? values.reduce((sum, value) => sum + value, 0)
                            : null;
                    }
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
