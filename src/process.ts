import { spawn } from 'node:child_process';
import { open } from 'node:fs/promises';
import { fsyncSync, writeSync } from 'node:fs';
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
    reason: 'completed' | 'exit_error' | 'timeout' | 'cancelled' | 'spawn_error' | 'output_limit' | 'capture_error';
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
    let cleanup: (() => void) | undefined;
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
            cleanup = () => {
                spec.signal?.removeEventListener('abort', onAbort);
                kill('SIGKILL');
            };
            let captureFailed = false;
            const captureError = () => {
                captureFailed = true;
                stop('capture_error');
            };
            const collect = (target: typeof stdout, chunk: Buffer) => {
                if (captureFailed)
                    return;
                if (bytes >= spec.maxLogBytes) {
                    stop('output_limit');
                    return;
                }
                const remaining = spec.maxLogBytes - bytes;
                target.pending = Buffer.concat([target.pending, chunk.subarray(0, remaining)]);
                try {
                    flush(target);
                }
                catch {
                    captureError();
                    return;
                }
                bytes += Math.min(chunk.length, remaining);
                if (chunk.length > remaining)
                    stop('output_limit');
            };
            child.stdout.on('data', (chunk: Buffer) => collect(stdout, chunk));
            child.stderr.on('data', (chunk: Buffer) => collect(stderr, chunk));
            child.stdout.on('error', captureError);
            child.stderr.on('error', captureError);
            child.stdin.on('error', () => { });
            if (spec.stdin !== undefined)
                child.stdin.end(spec.stdin);
            else
                child.stdin.end();
            await new Promise<void>((resolve) => {
                child.once('error', () => { reason ??= 'spawn_error'; });
                // The direct process can exit while descendants retain pipes or
                // keep running without them. Neither extends this invocation.
                child.once('exit', () => kill('SIGKILL'));
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
            try {
                flush(stdout, true);
                flush(stderr, true);
            }
            catch {
                reason = 'capture_error';
            }
        }
        return { exitCode, signal: exitSignal, reason: reason!, startedAt, endedAt: new Date().toISOString() };
    }
    finally {
        cleanup?.();
        if (timeout)
            clearTimeout(timeout);
        if (forceKill)
            clearTimeout(forceKill);
        await Promise.all([stdoutFile.close(), stderrFile.close()]);
    }
}
