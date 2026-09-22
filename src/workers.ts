import { choice as choiceSchema, role as roleSchema } from './contracts.ts';
import type { Role, WorkerChoice } from './contracts.ts';
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
const commandRoles = new Set<Role>(['developer', 'tester']);
export function workerCommand(choice: WorkerChoice, role: Role, cwd: string, prompt: string, policy: string): {
    executable: string;
    args: string[];
    stdin: string;
    env: Record<string, string>;
} {
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
        const sandbox = commandRoles.has(role) ? 'workspace-write' : 'read-only';
        return {
            executable: 'codex',
            args: [
                'exec', '--json', '--ephemeral', '--ignore-user-config', '--ignore-rules',
                '--disable', 'multi_agent',
                '--model', choice.model,
                '-c', `model_reasoning_effort="${choice.effort}"`,
                '-c', 'approval_policy="never"',
                '-c', 'project_doc_max_bytes=0',
                '--sandbox', sandbox,
                '-C', cwd,
                '-',
            ],
            stdin: `Worker policy:\n${policy}\n\nTask:\n${prompt}`,
            env: {},
        };
    }
    const tools = writeRoles.has(role) ? 'Read,Glob,Grep,Edit,Write' : 'Read,Glob,Grep';
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
        ],
        stdin: prompt,
        env: {},
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
function usageFields(value: unknown): Pick<WorkerOutput, 'inputTokens' | 'outputTokens' | 'cachedInputTokens'> {
    const usage = object(value);
    return {
        inputTokens: tokens(usage?.input_tokens ?? usage?.inputTokens),
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
                Object.assign(output, usageFields(event.usage));
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
                Object.assign(output, usageFields(message.usage));
            }
        }
        else if (event.type === 'result') {
            completed = true;
            const result = string(event.result);
            if (result)
                output.text = result;
            if (event.usage)
                Object.assign(output, usageFields(event.usage));
            const modelUsage = object(event.modelUsage);
            if (modelUsage) {
                const models = Object.entries(modelUsage);
                if (models.length === 1)
                    output.model = models[0][0];
                if (!event.usage) {
                    const totals = models.map(([, value]) => usageFields(value));
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
        else if (event.type === 'error' || event.type === 'rate_limit_event') {
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
