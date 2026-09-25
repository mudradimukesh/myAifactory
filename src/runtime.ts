import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, readdir, lstat, realpath, mkdir, copyFile, cp, chmod, writeFile, access, mkdtemp, rm } from 'node:fs/promises';
import { constants } from 'node:fs';
import { homedir, release } from 'node:os';
import path from 'node:path';
import { id, projectSchema } from './contracts.ts';
import type { Project } from './contracts.ts';
import { runProcess } from './process.ts';
import type { ProcessSpec } from './process.ts';
import { z } from 'zod';
import { durable, exists, json, sha } from './store.ts';

const exec = promisify(execFile);
const sandbox = '/usr/bin/sandbox-exec';
const systemRoots = ['/usr/bin', '/usr/sbin', '/usr/lib', '/usr/share', '/bin', '/sbin', '/System/Library', '/System/Cryptexes', '/System/Volumes/Preboot/Cryptexes', '/Library/Apple', '/Library/Developer', '/private/etc', '/private/var/db/dyld', '/private/var/db/timezone'];
export const workerHome = (scratch: string) => path.join(scratch, 'home');
const contains = (root: string, value: string) => root === path.sep || value === root || value.startsWith(root + path.sep);
const overlaps = (a: string, b: string) => contains(a, b) || contains(b, a);

export interface Job {
    id: string;
    project: Project;
    workspace: string;
    policyDir: string;
    outputDir: string;
    captureDir: string;
    scratchDir: string;
    resumeHome?: string;
    argv: string[];
    stdin?: string;
    env?: Record<string, string>;
    redact?: string[];
    provider?: 'codex' | 'claude';
    readOnlySource: boolean;
    network: 'none' | 'loopback' | 'outbound';
    timeoutMs: number;
    maxLogBytes: number;
    signal: AbortSignal;
    limit?: ProcessSpec['limit'];
    pause?: ProcessSpec['pause'];
    onSpawn?: ProcessSpec['onSpawn'];
}

async function directory(value: string): Promise<string> {
    if (!path.isAbsolute(value) || /[\0\r\n]/.test(value) || (await lstat(value)).isSymbolicLink())
        throw Error('Runtime paths must be absolute directories, not symlinks');
    const resolved = await realpath(value);
    if (!(await lstat(resolved)).isDirectory()) throw Error('Runtime path is not a directory');
    return resolved;
}

export function sandboxProfile(read: string[], write: string[], network: Job['network'], temp?: string): string {
    const paths = (values: string[]) => values.map(value => `(subpath ${JSON.stringify(value)})`).join(' ');
    return [
        '(version 1)', '(deny default)',
        '(allow process-exec)', '(allow process-fork)',
        '(allow signal (target same-sandbox))', '(allow sysctl-read)',
        // Loader/path traversal needs metadata, but no access to file contents.
        '(allow file-read-metadata)',
        `(allow file-read* (literal "/") ${paths([...systemRoots, ...read])} (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom"))`,
        `(allow file-write* (literal "/dev/null") ${paths(write)})`,
        temp ? `(allow network-bind network-inbound network-outbound (subpath ${JSON.stringify(temp)}))` : '',
        network === 'none' ? '' : '(allow mach-lookup (global-name "com.apple.bsd.dirhelper") (global-name "com.apple.system.opendirectoryd") (global-name "com.apple.SystemConfiguration.configd") (global-name "com.apple.networkd") (global-name "com.apple.dnssd.service") (global-name "com.apple.trustd"))',
        network === 'outbound' ? '(allow network-outbound (remote ip "*:*") (literal "/private/var/run/mDNSResponder"))' : '',
        network === 'none' ? '' : '(allow network-bind (local ip "localhost:*")) (allow network-inbound (local ip "localhost:*")) (allow network-outbound (remote ip "localhost:*"))',
    ].filter(Boolean).join('\n');
}

export class LocalRuntime {
    readonly identity = `macos-sandbox:${release()}`;

    async preflight(project: Project): Promise<string[]> {
        projectSchema.parse(project);
        if (process.platform !== 'darwin') return ['Native execution requires macOS'];
        const errors: string[] = [];
        try {
            const result = await exec(sandbox, ['-p', sandboxProfile([], [], 'none'), '/bin/echo', 'factory-sandbox-ready'], {
                cwd: '/', env: { PATH: '/usr/bin:/bin', LANG: 'C.UTF-8' }, timeout: 5000,
            });
            if (result.stdout.trim() !== 'factory-sandbox-ready') throw Error('Unexpected sandbox probe result');
        } catch { errors.push('macOS sandbox-exec is unavailable or cannot apply its profile; unrestricted fallback is disabled'); }
        for (const root of project.runtime.toolPaths) {
            try {
                const tool = await directory(root);
                if (contains(tool, await realpath(homedir()))) throw Error('Tool path exposes home');
            } catch { errors.push(`Invalid tool installation directory: ${root}`); }
        }
        for (const provider of new Set(Object.values(project.models).map(model => model.provider))) {
            const home = project.runtime.authHomes[provider];
            try {
                if (!home) throw Error(`Configure a dedicated ${provider} subscription auth home`);
                await validateAuthHome(provider, home);
            } catch (error) { errors.push((error as Error).message); }
        }
        if (project.headroom) {
            try {
                const response = await fetch(new URL('/health', project.headroom.baseUrl), { redirect: 'error', signal: AbortSignal.timeout(5000) });
                if (!response.ok) throw Error('Headroom health request failed');
                z.object({ service: z.literal('headroom-proxy'), status: z.literal('healthy'), ready: z.literal(true),
                    config: z.object({ savings_profile: z.literal('general'), optimize: z.literal(true), cache: z.literal(false), disable_kompress: z.literal(true),
                        disable_kompress_fallback: z.literal(true), disable_kompress_openai: z.literal(true), force_kompress: z.literal(false), compress_user_messages: z.literal(false), compress_system_messages: z.literal(false),
                        disable_kompress_anthropic: z.literal(true), anthropic_api_url: z.null() }),
                }).parse(await response.json());
            } catch { errors.push('Required Headroom proxy is unavailable or its protected compression configuration does not match'); }
        }
        return errors;
    }

    async execute(job: Job) {
        if (process.platform !== 'darwin') throw Error('Native execution requires macOS');
        id.parse(job.id);
        projectSchema.parse(job.project);
        if (!['none', 'loopback', 'outbound'].includes(job.network) ||
            (job.network !== 'none' && job.project.runtime.network === 'none') ||
            (job.network === 'outbound' && job.project.runtime.network !== 'outbound'))
            throw Error('Job network access exceeds the project profile');
        if (job.timeoutMs > job.project.limits.attemptTimeoutMs || job.maxLogBytes > job.project.limits.maxLogBytes)
            throw Error('Job limits exceed the project profile');
        const roots = await Promise.all([job.workspace, job.policyDir, job.outputDir, job.captureDir, job.scratchDir].map(directory));
        const [workspace, policyDir, outputDir, captureDir, scratchDir] = roots;
        const userHome = await realpath(homedir());
        for (let i = 0; i < roots.length; i++) {
            if (contains(roots[i], userHome) || systemRoots.some(root => overlaps(root, roots[i])))
                throw Error('Job directories must not expose the user home or system directories');
            if (roots.some((root, j) => j !== i && overlaps(root, roots[i])))
                throw Error('Job directories must be disjoint, including capture');
        }
        const tools = await Promise.all(job.project.runtime.toolPaths.map(directory));
        if (tools.some(root => contains(root, userHome) || roots.some(jobRoot => overlaps(root, jobRoot))))
            throw Error('Tool directories must not expose job data or the user home');
        if ((await readdir(captureDir)).length || (await readdir(scratchDir)).length)
            throw Error('Each execution needs fresh capture and scratch directories');
        const search = [...new Set(tools.flatMap(root => [path.join(root, 'bin'), root]).concat(['/usr/bin', '/bin', '/usr/sbin', '/sbin']))];
        if (!job.argv.length || !job.argv[0]) throw Error('Job command is required');
        let executable: string | undefined;
        for (const file of path.isAbsolute(job.argv[0]) ? [job.argv[0]] : search.map(root => path.join(root, job.argv[0]))) {
            try { await access(file, constants.X_OK); executable = await realpath(file); break; } catch { /* Try the next approved installation. */ }
        }
        if (!executable || ![...systemRoots, ...tools, workspace, scratchDir].some(root => contains(root, executable)))
            throw Error('Executable must resolve inside an approved tool installation or workspace');
        for (const [key, value] of Object.entries(job.env ?? {})) {
            if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) || /^(HOME|TMPDIR|CLAUDE_CODE_TMPDIR|PATH|CODEX_HOME|CLAUDE_CONFIG_DIR|OPENAI_API_KEY|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN)$/.test(key) || value.includes('\0'))
                throw Error(`Reserved or invalid worker environment variable: ${key}`);
        }
        const home = workerHome(scratchDir);
        await mkdir(home, { mode: 0o700 });
        let credential: { source: string; copy: string; digest: string } | undefined;
        if (job.provider) {
            const source = job.project.runtime.authHomes[job.provider];
            if (!source) throw Error(`Configure a dedicated ${job.provider} subscription auth home`);
            await validateAuthHome(job.provider, source);
            const authRoot = await realpath(source);
            if ([...roots, ...tools].some(root => overlaps(root, authRoot))) throw Error('Credential source must be outside job and tool directories');
            const target = path.join(home, `.${job.provider}`);
            await mkdir(target, { mode: 0o700 });
            const name = job.provider === 'codex' ? 'auth.json' : '.credentials.json';
            await copyFile(path.join(authRoot, name), path.join(target, name), constants.COPYFILE_EXCL);
            await chmod(path.join(target, name), 0o600);
            credential = { source: path.join(authRoot, name), copy: path.join(target, name), digest: sha(await readFile(path.join(authRoot, name))) };
            if (job.resumeHome) {
                const previous = await directory(job.resumeHome);
                const sessions = job.provider === 'codex' ? 'sessions' : 'projects';
                const sourceSessions = await directory(path.join(previous, `.${job.provider}`, sessions));
                if (!contains(previous, sourceSessions) || roots.some(root => overlaps(root, previous)))
                    throw Error('Resume home must be separate from the current job directories');
                await cp(sourceSessions, path.join(target, sessions), { recursive: true, dereference: false,
                    filter: async source => {
                        if ((await lstat(source)).isSymbolicLink()) throw Error('Resume sessions must not contain symlinks');
                        return true;
                    } });
            }
        }
        const temp = await mkdtemp('/private/tmp/factory-');
        try {
            const policy = sandboxProfile([workspace, policyDir, outputDir, scratchDir, temp, ...tools], [outputDir, scratchDir, temp, ...(job.readOnlySource ? [] : [workspace])], job.network, temp);
            const command = [sandbox, '-p', policy, executable, ...job.argv.slice(1)];
            await writeFile(path.join(captureDir, 'invocation.json'), json({ id: job.id, runtime: this.identity, profileDigest: sha(policy), executable, argv: job.argv.slice(1), cwd: workspace, network: job.network, readOnlySource: job.readOnlySource, environmentNames: Object.keys(job.env ?? {}) }), { flag: 'wx', mode: 0o600 });
            return await runProcess({
                argv: command, cwd: workspace, stdin: job.stdin,
                env: { ...job.env, PATH: search.join(path.delimiter), HOME: home, TMPDIR: temp, CLAUDE_CODE_TMPDIR: temp, CODEX_HOME: path.join(home, '.codex'), CLAUDE_CONFIG_DIR: path.join(home, '.claude') },
                stdoutPath: path.join(captureDir, 'stdout.log'), stderrPath: path.join(captureDir, 'stderr.log'),
                timeoutMs: job.timeoutMs, maxLogBytes: job.maxLogBytes, signal: job.signal, limit: job.limit, redact: job.redact, pause: job.pause, onSpawn: job.onSpawn,
            });
        } finally {
            await rm(temp, { recursive: true, force: true });
            if (credential && job.provider) await returnRefreshedCredential(job.provider, credential);
        }
    }
}

// Providers rotate refresh tokens, so the next attempt needs the credential this attempt refreshed.
// Write it back only if it is still a subscription credential and no other writer changed the source.
async function returnRefreshedCredential(provider: 'codex' | 'claude', credential: { source: string; copy: string; digest: string }) {
    const info = await lstat(credential.copy).catch(() => null);
    if (!info?.isFile() || info.size > 65536) return;
    const refreshed = await readFile(credential.copy);
    if (sha(refreshed) === credential.digest || sha(await readFile(credential.source)) !== credential.digest) return;
    try { subscriptionCredential(provider, JSON.parse(refreshed.toString('utf8'))); } catch { return; }
    await durable(credential.source, refreshed);
}

function subscriptionCredential(provider: 'codex' | 'claude', auth: { auth_mode?: unknown; OPENAI_API_KEY?: unknown; claudeAiOauth?: unknown }) {
    if (provider === 'codex' && (auth.auth_mode !== 'chatgpt' || auth.OPENAI_API_KEY))
        throw Error('Codex worker requires ChatGPT subscription auth, not an API key');
    if (provider === 'claude' && !auth.claudeAiOauth) throw Error('Claude worker requires Claude subscription auth');
}

export async function validateAuthHome(provider: 'codex' | 'claude', home: string) {
    const root = await directory(home);
    const filename = provider === 'codex' ? 'auth.json' : '.credentials.json';
    for (const file of await readdir(root)) {
        if (file !== filename || !(await lstat(path.join(root, file))).isFile())
            throw Error(`The ${provider} auth home must contain credentials only, without settings or plugins`);
    }
    const file = path.join(root, filename);
    if (!await exists(file)) throw Error(`No ${provider} subscription credential file in the dedicated auth home`);
    subscriptionCredential(provider, JSON.parse(await readFile(file, 'utf8')));
}
