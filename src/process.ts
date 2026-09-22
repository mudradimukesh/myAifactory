import { spawn } from 'node:child_process';
import { open } from 'node:fs/promises';
import { fsyncSync, writeSync } from 'node:fs';
import { isAbsolute } from 'node:path';
export interface ProcessSpec {
    argv: string[];
    cwd: string;
    env?: Record<string, string>;
    stdin?: string;
    stdoutPath: string;
    stderrPath: string;
    timeoutMs: number;
    maxLogBytes: number;
    signal?: AbortSignal;
    redact?: string[];
}
export interface ProcessResult {
    exitCode: number | null;
    signal: string | null;
    reason: 'completed' | 'exit_error' | 'timeout' | 'cancelled' | 'spawn_error' | 'output_limit';
    startedAt: string;
    endedAt: string;
}
function redactBytes(input: Buffer, secrets: Buffer[]): Buffer {
    let output = input;
    const marker = Buffer.from('[REDACTED]');
    for (const secret of secrets) {
        const parts: Buffer[] = [];
        let from = 0;
        let match = output.indexOf(secret, from);
        if (match < 0)
            continue;
        while (match >= 0) {
            parts.push(output.subarray(from, match), marker);
            from = match + secret.length;
            match = output.indexOf(secret, from);
        }
        parts.push(output.subarray(from));
        output = Buffer.concat(parts);
    }
    return output;
}
export async function runProcess(spec: ProcessSpec): Promise<ProcessResult> {
    if (!spec.argv.length || !spec.argv[0] || !Number.isSafeInteger(spec.timeoutMs) || spec.timeoutMs <= 0 ||
        !Number.isSafeInteger(spec.maxLogBytes) || spec.maxLogBytes <= 0 || spec.stdoutPath === spec.stderrPath) {
        throw new Error('Invalid process specification');
    }
    const stdoutFile = await open(spec.stdoutPath, 'wx', 0o600);
    let stderrFile;
    try {
        stderrFile = await open(spec.stderrPath, 'wx', 0o600);
    }
    catch (error) {
        await stdoutFile.close();
        throw error;
    }
    const startedAt = new Date().toISOString();
    const stdout = { fd: stdoutFile.fd, pending: Buffer.alloc(0) };
    const stderr = { fd: stderrFile.fd, pending: Buffer.alloc(0) };
    const secrets = [...new Set(spec.redact?.filter(Boolean) ?? [])]
        .map((value) => Buffer.from(value)).sort((a, b) => b.length - a.length);
    let bytes = 0;
    let storedBytes = 0;
    const flush = (target: typeof stdout, final = false) => {
        let end = target.pending.length;
        if (!final) {
            for (let i = 0; i < target.pending.length; i++) {
                const tail = target.pending.subarray(i);
                if (secrets.some(secret => tail.length < secret.length && secret.subarray(0, tail.length).equals(tail))) {
                    end = i;
                    break;
                }
                const match = secrets.find(secret => tail.length >= secret.length && tail.subarray(0, secret.length).equals(secret));
                if (match) {
                    i += match.length - 1;
                    continue;
                }
            }
        }
        const clean = redactBytes(target.pending.subarray(0, end), secrets);
        const output = clean.subarray(0, Math.max(0, spec.maxLogBytes - storedBytes));
        let offset = 0;
        while (offset < output.length)
            offset += writeSync(target.fd, output, offset);
        if (output.length)
            fsyncSync(target.fd);
        storedBytes += output.length;
        target.pending = Buffer.from(target.pending.subarray(end));
    };
    let reason: ProcessResult['reason'] | undefined;
    let exitCode: number | null = null;
    let exitSignal: string | null = null;
    let timeout: NodeJS.Timeout | undefined;
    let forceKill: NodeJS.Timeout | undefined;
    try {
        if (spec.signal?.aborted) {
            reason = 'cancelled';
        }
        else {
            const child = spawn(spec.argv[0], spec.argv.slice(1), {
                cwd: spec.cwd,
                env: { PATH: process.env.PATH || '/usr/bin:/bin', LANG: 'C.UTF-8', ...spec.env },
                stdio: ['pipe', 'pipe', 'pipe'],
                shell: false,
                detached: process.platform !== 'win32',
            });
            const kill = (signal: NodeJS.Signals) => {
                if (child.pid === undefined)
                    return;
                try {
                    if (process.platform === 'win32')
                        child.kill(signal);
                    else
                        process.kill(-child.pid, signal);
                }
                catch {
                    try {
                        child.kill(signal);
                    }
                    catch { /* Already exited. */ }
                }
            };
            const stop = (why: ProcessResult['reason']) => {
                if (reason)
                    return;
                reason = why;
                kill('SIGTERM');
                forceKill = setTimeout(() => {
                    kill('SIGKILL');
                    child.stdout.destroy();
                    child.stderr.destroy();
                }, 1000);
            };
            const onAbort = () => stop('cancelled');
            const collect = (target: typeof stdout, chunk: Buffer) => {
                if (bytes >= spec.maxLogBytes) {
                    stop('output_limit');
                    return;
                }
                const remaining = spec.maxLogBytes - bytes;
                target.pending = Buffer.concat([target.pending, chunk.subarray(0, remaining)]);
                flush(target);
                bytes += Math.min(chunk.length, remaining);
                if (chunk.length > remaining)
                    stop('output_limit');
            };
            child.stdout.on('data', (chunk: Buffer) => collect(stdout, chunk));
            child.stderr.on('data', (chunk: Buffer) => collect(stderr, chunk));
            child.stdin.on('error', () => { });
            if (spec.stdin !== undefined)
                child.stdin.end(spec.stdin);
            else
                child.stdin.end();
            await new Promise<void>((resolve) => {
                child.once('error', () => { reason ??= 'spawn_error'; });
                child.once('close', (code, signal) => {
                    exitCode = code;
                    exitSignal = signal;
                    resolve();
                });
                timeout = setTimeout(() => stop('timeout'), spec.timeoutMs);
                spec.signal?.addEventListener('abort', onAbort, { once: true });
                if (spec.signal?.aborted)
                    onAbort();
            });
            spec.signal?.removeEventListener('abort', onAbort);
            if (reason && reason !== 'spawn_error')
                kill('SIGKILL');
            if (timeout)
                clearTimeout(timeout);
            if (forceKill)
                clearTimeout(forceKill);
            if (!reason)
                reason = exitCode === 0 ? 'completed' : 'exit_error';
        }
        // Incomplete secrets stay withheld when execution is interrupted or truncated.
        if (reason === 'completed' || reason === 'exit_error') {
            flush(stdout, true);
            flush(stderr, true);
        }
        return { exitCode, signal: exitSignal, reason: reason!, startedAt, endedAt: new Date().toISOString() };
    }
    finally {
        if (timeout)
            clearTimeout(timeout);
        if (forceKill)
            clearTimeout(forceKill);
        await Promise.all([stdoutFile.close(), stderrFile.close()]);
    }
}
export interface DockerSpec {
    name: string;
    owner: string;
    image: string;
    workspace: string;
    policyDir: string;
    outputDir: string;
    authDir?: string;
    provider?: 'codex' | 'claude';
    readOnlySource: boolean;
    network: 'none' | 'bridge';
    cpus: number;
    memoryMb: number;
    pids: number;
    argv: string[];
    env?: Record<string, string>;
}
export function dockerCommand(spec: DockerSpec): string[] {
    if (!/^[a-z0-9][a-z0-9_.-]{0,127}$/.test(spec.name) ||
        !/^[a-f0-9]{64}$/.test(spec.owner) ||
        !/^[^\s@]+@sha256:[a-f0-9]{64}$/.test(spec.image) ||
        !spec.argv.length || !spec.argv[0] ||
        !Number.isFinite(spec.cpus) || spec.cpus <= 0 ||
        !Number.isSafeInteger(spec.memoryMb) || spec.memoryMb <= 0 ||
        !Number.isSafeInteger(spec.pids) || spec.pids <= 0 ||
        !['none', 'bridge'].includes(spec.network) ||
        (spec.authDir !== undefined && !spec.provider)) {
        throw new Error('Invalid Docker specification');
    }
    for (const path of [spec.workspace, spec.policyDir, spec.outputDir, spec.authDir].filter((value): value is string => value !== undefined)) {
        if (!isAbsolute(path) || /[,\r\n\0]/.test(path))
            throw new Error('Docker mount paths must be absolute and cannot contain commas or control characters');
    }
    const uid = process.getuid?.() ?? 1000;
    const gid = process.getgid?.() ?? 1000;
    const command = [
        'docker', 'run', '--name', spec.name, '--label', `dev.agent-factory.owner=${spec.owner}`, '--interactive', '--init', '--read-only',
        '--user', `${uid}:${gid}`, '--workdir', '/workspace',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--network', spec.network, '--cpus', String(spec.cpus),
        '--memory', `${spec.memoryMb}m`, '--pids-limit', String(spec.pids),
        '--tmpfs', '/tmp:rw,nosuid,nodev',
        '--tmpfs', `/home/worker:rw,nosuid,nodev,uid=${uid},gid=${gid},mode=0700`,
        '--mount', `type=bind,source=${spec.workspace},target=/workspace${spec.readOnlySource ? ',readonly' : ''}`,
        '--mount', `type=bind,source=${spec.policyDir},target=/policy,readonly`,
        '--mount', `type=bind,source=${spec.outputDir},target=/output`,
    ];
    if (spec.authDir)
        command.push('--mount', `type=bind,source=${spec.authDir},target=/home/worker/.${spec.provider},readonly`);
    command.push('--env', 'HOME=/home/worker');
    for (const [key, value] of Object.entries(spec.env ?? {})) {
        if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) || key === 'HOME' || value.includes('\0')) {
            throw new Error(`Invalid Docker environment variable: ${key}`);
        }
        command.push('--env', `${key}=${value}`);
    }
    return [...command, spec.image, ...spec.argv];
}
