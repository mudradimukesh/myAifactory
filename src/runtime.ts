import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, readdir, lstat } from 'node:fs/promises';
import path from 'node:path';
import type { Project } from './contracts.ts';
import { dockerCommand, runProcess } from './process.ts';
import { exists } from './store.ts';
const exec = promisify(execFile);
export interface Job {
    id: string;
    owner: string;
    project: Project;
    workspace: string;
    policyDir: string;
    outputDir: string;
    argv: string[];
    stdin?: string;
    env?: Record<string, string>;
    provider?: 'codex' | 'claude';
    readOnlySource: boolean;
    network: 'none' | 'bridge';
    timeoutMs: number;
    maxLogBytes: number;
    signal: AbortSignal;
}
export class DockerRuntime {
    identity = 'docker';
    async preflight(project: Project) {
        const errors: string[] = [];
        if (!project.runtime.image || !/@sha256:[a-f0-9]{64}$/.test(project.runtime.image))
            errors.push('Set runtime.image to a prepared Linux image pinned by SHA-256 digest');
        try {
            const { stdout } = await exec('docker', ['info', '--format', '{{.OSType}}'], { timeout: 10000 });
            if (stdout.trim() !== 'linux')
                errors.push('Docker must run Linux containers');
        }
        catch {
            errors.push('Docker is unavailable; install/start a Linux container runtime');
        }
        if (errors.length)
            return errors;
        try {
            await exec('docker', ['image', 'inspect', project.runtime.image!], { timeout: 10000 });
        }
        catch {
            errors.push('The pinned runtime image is not available locally');
        }
        for (const provider of new Set(Object.values(project.models).map(x => x.provider))) {
            const home = project.runtime.authHomes[provider];
            if (!home) {
                errors.push(`Configure a dedicated ${provider} subscription auth home`);
                continue;
            }
            try {
                await validateAuthHome(provider, home);
            }
            catch (e) {
                errors.push(String((e as Error).message));
            }
        }
        return errors;
    }
    async execute(job: Job) {
        const p = job.project;
        const argv = dockerCommand({ name: job.id, owner: job.owner, image: p.runtime.image!, workspace: job.workspace, policyDir: job.policyDir, outputDir: job.outputDir,
            ...(job.provider ? { provider: job.provider, authDir: p.runtime.authHomes[job.provider] ?? undefined } : {}),
            readOnlySource: job.readOnlySource, network: job.network, cpus: p.runtime.cpus, memoryMb: p.runtime.memoryMb, pids: p.runtime.pids, argv: job.argv, env: job.env });
        const result = await runProcess({ argv, cwd: job.outputDir, stdin: job.stdin, stdoutPath: path.join(job.outputDir, 'stdout.log'), stderrPath: path.join(job.outputDir, 'stderr.log'), timeoutMs: job.timeoutMs, maxLogBytes: job.maxLogBytes, signal: job.signal });
        if (result.reason !== 'completed')
            await this.stop(job.id, job.owner);
        return result;
    }
    private async ownedContainer(name: string, owner: string) {
        if (!/^factory-[a-z0-9-]+$/.test(name) || !/^[a-f0-9]{64}$/.test(owner))
            throw Error('Refuse to manage an unowned container');
        let stdout: string;
        try {
            ({ stdout } = await exec('docker', ['container', 'inspect', name], { timeout: 10000 }));
        }
        catch (e) {
            if (e && typeof e === 'object' && 'stderr' in e && String(e.stderr).includes('No such'))
                return null;
            throw Error(`Cannot reconcile container ${name}`);
        }
        const [container] = JSON.parse(stdout);
        if (container?.Config?.Labels?.['dev.agent-factory.owner'] !== owner || !/^\/[a-z0-9-]+$/.test(container?.Name) || container.Name !== `/${name}` || !/^[a-f0-9]{64}$/.test(container?.Id))
            throw Error('Refuse to manage an unowned container');
        return container.Id as string;
    }
    async stop(name: string, owner: string) { const container = await this.ownedContainer(name, owner); if (container)
        await exec('docker', ['stop', '--time', '2', container], { timeout: 15000 }); }
    async remove(name: string, owner: string) { const container = await this.ownedContainer(name, owner); if (container)
        await exec('docker', ['rm', container], { timeout: 10000 }); }
}
export async function validateAuthHome(provider: 'codex' | 'claude', home: string) {
    if (!path.isAbsolute(home))
        throw Error('Auth home must be an absolute dedicated path');
    const allowed = new Set(provider === 'codex' ? ['auth.json'] : ['.credentials.json']);
    if ((await lstat(home)).isSymbolicLink())
        throw Error('Auth home must not be a symlink');
    for (const file of await readdir(home)) {
        if (!allowed.has(file) || (await lstat(path.join(home, file))).isSymbolicLink())
            throw Error(`The ${provider} auth home must contain credentials only, without settings or plugins`);
    }
    const file = path.join(home, provider === 'codex' ? 'auth.json' : '.credentials.json');
    if (!await exists(file))
        throw Error(`No ${provider} subscription credential file in the dedicated auth home`);
    const auth = JSON.parse(await readFile(file, 'utf8'));
    if (provider === 'codex' && (auth.auth_mode !== 'chatgpt' || auth.OPENAI_API_KEY))
        throw Error('Codex worker requires ChatGPT subscription auth, not an API key');
    if (provider === 'claude' && !auth.claudeAiOauth)
        throw Error('Claude worker requires Claude subscription auth');
}
