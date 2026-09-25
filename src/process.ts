import { execFile, spawn } from 'node:child_process';
import { open } from 'node:fs/promises';
import { fsyncSync, writeSync } from 'node:fs';
import { promisify } from 'node:util';
import type { OwnedProcess } from './contracts.ts';
const exec = promisify(execFile);
/** Operator pause for a running process group. Emits 'pause' and 'resume'. */
export class PauseGate extends EventTarget {
    paused = false;
    pause() { if (this.paused) return; this.paused = true; this.dispatchEvent(new Event('pause')); }
    resume() { if (!this.paused) return; this.paused = false; this.dispatchEvent(new Event('resume')); }
}
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
    /** Aborted with a usage or context reason to stop the process before the timeout. */
    limit?: AbortSignal;
    redact?: string[];
    pause?: PauseGate;
    /** Awaited before the timeout is armed. A rejection kills the group and yields spawn_error. */
    onSpawn?: (pid: number) => Promise<void>;
}
export interface ProcessResult {
    exitCode: number | null;
    signal: string | null;
    reason: 'completed' | 'exit_error' | 'timeout' | 'cancelled' | 'spawn_error' | 'output_limit' | 'capture_error'
        | 'token_limit' | 'context_limit' | 'compacted' | 'stall_start' | 'stall_idle';
    startedAt: string;
    endedAt: string;
    pausedMs: number;
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
    let pausedMs = 0;
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
                // A group frozen by SIGSTOP acts on SIGTERM only after SIGCONT.
                kill('SIGCONT');
                forceKill = setTimeout(() => {
                    kill('SIGKILL');
                    child.stdout.destroy();
                    child.stderr.destroy();
                }, 1000);
            };
            const onAbort = () => stop('cancelled');
            const limitReasons = new Set<ProcessResult['reason']>(['token_limit', 'context_limit', 'compacted', 'stall_start', 'stall_idle']);
            const onLimit = () => {
                const reason = spec.limit?.reason;
                stop(typeof reason === 'string' && limitReasons.has(reason as ProcessResult['reason']) ? reason as ProcessResult['reason'] : 'token_limit');
            };
            // The timeout counts running time only. A pause keeps the unused remainder.
            let remaining = spec.timeoutMs;
            let armedAt = 0;
            let pausedAt: number | null = null;
            const arm = () => {
                armedAt = Date.now();
                timeout = setTimeout(() => stop('timeout'), remaining);
            };
            const freeze = () => {
                if (pausedAt !== null || reason)
                    return;
                pausedAt = Date.now();
                kill('SIGSTOP');
                if (timeout) {
                    clearTimeout(timeout);
                    timeout = undefined;
                    remaining = Math.max(0, remaining - (pausedAt - armedAt));
                }
            };
            const thaw = () => {
                if (pausedAt === null)
                    return;
                pausedMs += Date.now() - pausedAt;
                pausedAt = null;
                kill('SIGCONT');
                if (!reason)
                    arm();
            };
            cleanup = () => {
                spec.signal?.removeEventListener('abort', onAbort);
                spec.limit?.removeEventListener('abort', onLimit);
                spec.pause?.removeEventListener('pause', freeze);
                spec.pause?.removeEventListener('resume', thaw);
                if (pausedAt !== null) {
                    pausedMs += Date.now() - pausedAt;
                    pausedAt = null;
                }
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
            const closed = new Promise<void>((resolve) => {
                child.once('error', () => { reason ??= 'spawn_error'; });
                // The direct process can exit while descendants retain pipes or
                // keep running without them. Neither extends this invocation.
                child.once('exit', () => kill('SIGKILL'));
                child.once('close', (code, signal) => {
                    exitCode = code;
                    exitSignal = signal;
                    resolve();
                });
            });
            if (child.pid !== undefined && spec.onSpawn) {
                // An unrecorded worker must not keep running.
                try { await spec.onSpawn(child.pid); }
                catch {
                    reason ??= 'spawn_error';
                    kill('SIGKILL');
                }
            }
            if (!reason) {
                spec.pause?.addEventListener('pause', freeze);
                spec.pause?.addEventListener('resume', thaw);
                if (spec.pause?.paused)
                    freeze();
                else
                    arm();
                spec.signal?.addEventListener('abort', onAbort, { once: true });
                if (spec.signal?.aborted)
                    onAbort();
                spec.limit?.addEventListener('abort', onLimit, { once: true });
                if (spec.limit?.aborted)
                    onLimit();
            }
            await closed;
            cleanup();
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
        return { exitCode, signal: exitSignal, reason: reason!, startedAt, endedAt: new Date().toISOString(), pausedMs };
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

/** Identity of a live, non-zombie process, or null when it does not exist. */
export async function identify(pid: number): Promise<OwnedProcess | null> {
    try {
        const { stdout } = await exec('/bin/ps', ['-o', 'pgid=,stat=,lstart=', '-p', String(pid)], { env: { PATH: '/usr/bin:/bin', LC_ALL: 'C' }, timeout: 5000 });
        const match = /^\s*(\d+)\s+(\S+)\s+(.+?)\s*$/.exec(stdout);
        if (!match || match[2].startsWith('Z'))
            return null;
        return { pid, pgid: Number(match[1]), started: match[3] };
    }
    catch (error) {
        // ps exits 1 when the pid does not exist.
        if ((error as { code?: unknown }).code === 1)
            return null;
        throw error;
    }
}
export async function isOwnedAlive(p: OwnedProcess): Promise<boolean> {
    const live = await identify(p.pid);
    return live !== null && live.pgid === p.pgid && live.started === p.started;
}
export async function groupAlive(pgid: number): Promise<boolean> {
    try {
        process.kill(-pgid, 0);
        return true;
    }
    catch (error) {
        const code = (error as NodeJS.ErrnoException).code;
        if (code === 'ESRCH')
            return false;
        // macOS answers EPERM for a group whose only members are unreaped zombies.
        if (code === 'EPERM')
            return (await groupMembers(pgid)).length > 0;
        throw error;
    }
}
/** Live members of a process group, excluding zombies. */
export async function groupMembers(pgid: number): Promise<number[]> {
    const { stdout } = await exec('/bin/ps', ['-A', '-o', 'pid=,pgid=,stat='], { env: { PATH: '/usr/bin:/bin', LC_ALL: 'C' }, timeout: 5000, maxBuffer: 16 * 1024 * 1024 });
    return stdout.split('\n').map(line => line.trim().split(/\s+/)).filter(([pid, group, stat]) => pid && Number(group) === pgid && !stat?.startsWith('Z')).map(([pid]) => Number(pid));
}
/** Whether every live member of a process group has acknowledged SIGSTOP. */
export async function groupStopped(pgid: number): Promise<boolean> {
    const { stdout } = await exec('/bin/ps', ['-A', '-o', 'pgid=,stat='], { env: { PATH: '/usr/bin:/bin', LC_ALL: 'C' }, timeout: 5000, maxBuffer: 16 * 1024 * 1024 });
    return stdout.split('\n').map(line => line.trim().split(/\s+/))
        .filter(([group, stat]) => Number(group) === pgid && stat && !stat.startsWith('Z'))
        .every(([, stat]) => stat.includes('T'));
}
async function groupGone(pgid: number, ms: number): Promise<boolean> {
    const deadline = Date.now() + ms;
    for (;;) {
        if (!await groupAlive(pgid))
            return true;
        if (Date.now() >= deadline)
            return false;
        await new Promise(resolve => setTimeout(resolve, 50));
    }
}
/**
 * Terminates a recorded process group only after proving the leader is the recorded process.
 * A group whose leader is dead cannot be proven ours (ADR 0002), so it is reported, never signalled.
 */
export async function terminateOwned(p: OwnedProcess, graceMs: number): Promise<'gone' | 'not_owned' | 'survived'> {
    if (!await isOwnedAlive(p))
        return await groupAlive(p.pgid) ? 'not_owned' : 'gone';
    const signal = (name: NodeJS.Signals) => {
        try { process.kill(-p.pgid, name); }
        catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error; }
    };
    signal('SIGTERM');
    signal('SIGCONT');
    // The pgid cannot be reused while any member of the group is alive, so a group
    // still present after a proven identity check remains ours.
    if (await groupGone(p.pgid, graceMs))
        return 'gone';
    signal('SIGKILL');
    return await groupGone(p.pgid, 2000) ? 'gone' : 'survived';
}
