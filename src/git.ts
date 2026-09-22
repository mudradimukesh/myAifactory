import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, readFile, readdir, lstat, readlink } from 'node:fs/promises';
import path from 'node:path';
import { sha, relativePath } from './store.ts';
const exec = promisify(execFile);
export async function git(cwd: string, args: string[]) {
    const { stdout } = await exec('git', ['-c', `safe.directory=${cwd}`, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'protocol.file.allow=always', '-c', 'protocol.ext.allow=never', ...args], { cwd, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024, env: { PATH: process.env.PATH, LANG: 'C', GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null', GIT_TERMINAL_PROMPT: '0', GIT_OPTIONAL_LOCKS: '0' } });
    return args.includes("-z") ? stdout : stdout.trim();
}
export async function cleanRepository(repository: string, base: string) {
    if (await git(repository, ['rev-parse', '--show-toplevel']) !== path.resolve(repository))
        throw Error('Repository must name its Git root');
    if (await git(repository, ['status', '--porcelain', '--untracked-files=all']))
        throw Error('Source checkout has uncommitted changes; snapshot inclusion needs an explicit decision');
    const actual = await git(repository, ['rev-parse', 'HEAD']);
    if (actual !== base)
        throw Error(`Source base changed: expected ${base}, found ${actual}`);
    if (await git(repository, ['ls-files', '.gitmodules']))
        throw Error('Submodules are not supported by this factory profile');
}
export async function createStore(repository: string, target: string, base: string) {
    await mkdir(path.dirname(target), { recursive: true });
    await git(path.dirname(target), ['clone', '--bare', '--no-hardlinks', '--no-local', '--', repository, target]);
    await git(target, ['config', '--remove-section', 'remote.origin']);
    if (await git(target, ['rev-parse', base + '^{commit}']) !== base)
        throw Error('Base commit missing');
}
export async function checkout(store: string, target: string, candidate: string) {
    await mkdir(path.dirname(target), { recursive: true });
    await git(path.dirname(target), ['clone', '--no-hardlinks', '--no-local', '--', store, target]);
    await git(target, ['config', '--remove-section', 'remote.origin']);
    await git(target, ['checkout', '--detach', candidate]);
    await git(target, ['config', 'user.name', 'Factory developer']);
    await git(target, ['config', 'user.email', 'factory@localhost']);
}
export function allowed(file: string, paths: string[]) { return paths.some(p => p === '.' || file === p || file.startsWith(p.replace(/\/$/, '') + '/')); }
export async function importCandidate(store: string, source: string, base: string, allowedPaths: string[]) {
    if (await git(source, ['status', '--porcelain', '--untracked-files=all']))
        throw Error('Candidate has staged, unstaged, or untracked changes');
    const candidate = await git(source, ['rev-parse', 'HEAD']);
    if (!/^[a-f0-9]{40}$/.test(candidate))
        throw Error('Invalid candidate identity');
    await git(source, ['merge-base', '--is-ancestor', base, candidate]);
    const files = (await git(source, ['diff', '--name-only', '--no-renames', '-z', base, candidate])).split('\0').filter(Boolean);
    if (!files.length)
        throw Error('Candidate contains no implementation change');
    for (const file of files) {
        relativePath(file);
        if (!allowed(file, allowedPaths))
            throw Error(`Candidate changed disallowed path ${file}`);
    }
    const modeLines = (await git(source, ['ls-tree', '-r', candidate])).split('\n');
    if (modeLines.some(x => x.startsWith('160000')))
        throw Error('Candidate added an unsupported submodule');
    await git(store, ['fetch', '--no-tags', '--', source, `${candidate}:refs/candidates/${candidate}`]);
    if (await git(store, ['rev-parse', `refs/candidates/${candidate}`]) !== candidate)
        throw Error('Imported candidate mismatch');
    return { candidate, files };
}
export async function treeDigest(root: string, ignored: string[] = []) {
    const items: string[] = [];
    async function walk(dir: string, rel: string) {
        for (const entry of (await readdir(dir, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
            const p = rel ? rel + '/' + entry.name : entry.name;
            if (p === '.git' || ignored.some(i => p === i || p.startsWith(i + '/')))
                continue;
            const absolute = path.join(dir, entry.name), s = await lstat(absolute);
            if (s.isSymbolicLink()) {
                items.push(JSON.stringify([p, 'symlink', await readlink(absolute)]));
                continue;
            }
            if (s.isDirectory())
                await walk(absolute, p);
            else if (s.isFile())
                items.push(JSON.stringify([p, s.mode & 0o777, sha(await readFile(absolute))]));
            else
                throw Error(`Unsupported source file ${p}`);
        }
    }
    await walk(root, '');
    return sha(items.join('\n'));
}
