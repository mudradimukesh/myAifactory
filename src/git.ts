import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, readFile, readdir, lstat, readlink, mkdtemp, rm, writeFile, open } from 'node:fs/promises';
import { constants, createWriteStream } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { sha, relativePath, within, exists } from './store.ts';
const exec = promisify(execFile);
export async function git(cwd: string, args: string[]) {
    const { stdout } = await exec('git', ['--no-replace-objects', '-c', `safe.directory=${cwd}`, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'maintenance.auto=false', '-c', 'protocol.file.allow=always', '-c', 'protocol.ext.allow=never', ...args], { cwd, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024, timeout: 30000, env: { PATH: process.env.PATH, LANG: 'C', GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null', GIT_TERMINAL_PROMPT: '0', GIT_OPTIONAL_LOCKS: '0', GIT_NO_REPLACE_OBJECTS: '1', GIT_NO_LAZY_FETCH: '1' } });
    return args.includes("-z") ? stdout : stdout.trim();
}
// Only call git() on runner-owned metadata. A worker can change its own config,
// filters, refs and index. Snapshot object bytes without evaluating that metadata.
async function snapshot<T>(repository: string, fn: (trusted: string, head: string) => Promise<T>): Promise<T> {
    const metadata = await within(repository, '.git');
    if (!(await lstat(metadata)).isDirectory()) throw Error('Independent Git clones are required; linked worktrees are unsupported');
    if (await exists(path.join(metadata, 'shallow')) || await exists(path.join(metadata, 'objects/info/alternates')))
        throw Error('Shallow repositories and alternate object stores are unsupported');
    let head = (await readFile(await within(metadata, 'HEAD'), 'utf8')).trim();
    if (head.startsWith('ref: ')) {
        const ref = head.slice(5);
        if (!ref.startsWith('refs/heads/')) throw Error('Unsupported symbolic HEAD');
        relativePath(ref);
        if (await exists(path.join(metadata, ref))) head = (await readFile(await within(metadata, ref), 'utf8')).trim();
        else {
            const packed = await readFile(await within(metadata, 'packed-refs'), 'utf8');
            head = packed.split('\n').find(line => line.slice(41) === ref)?.slice(0, 40) ?? '';
        }
    }
    if (!/^[a-f0-9]{40}$/.test(head)) throw Error('Invalid candidate identity');
    const trusted = await mkdtemp(path.join(tmpdir(), 'factory-git-'));
    try {
        await git(trusted, ['init', '--bare', '--template=']);
        const objects = await within(metadata, 'objects');
        for (const group of await readdir(objects)) {
            if (group === 'info') continue;
            if (group !== 'pack' && !/^[a-f0-9]{2}$/.test(group)) throw Error('Unsupported Git object directory');
            const dir = await within(objects, group);
            if (!(await lstat(dir)).isDirectory()) throw Error('Invalid Git object directory');
            await mkdir(path.join(trusted, 'objects', group), { recursive: true });
            for (const name of await readdir(dir)) {
                if (group === 'pack' && (name === 'multi-pack-index' || /^pack-[a-f0-9]{40}\.keep$/.test(name))) continue;
                if (!(group === 'pack' ? /^pack-[a-f0-9]{40}\.(pack|idx|rev|bitmap)$/.test(name) : /^[a-f0-9]{38}$/.test(name)))
                    throw Error('Unsupported Git object file');
                const source = await within(dir, name);
                const file = await open(source, constants.O_RDONLY | constants.O_NOFOLLOW);
                try {
                    if (!(await file.stat()).isFile()) throw Error('Git objects must be regular files');
                    await pipeline(file.createReadStream({ autoClose: false }), createWriteStream(path.join(trusted, 'objects', group, name), { flags: 'wx', mode: 0o600 }));
                } finally { await file.close(); }
            }
        }
        await git(trusted, ['cat-file', '-e', head + '^{commit}']);
        await git(trusted, ['update-ref', 'refs/heads/snapshot', head]);
        await git(trusted, ['symbolic-ref', 'HEAD', 'refs/heads/snapshot']);
        await git(trusted, ['fsck', '--strict', '--no-reflogs', head]);
        return await fn(trusted, head);
    } finally { await rm(trusted, { recursive: true, force: true }); }
}

async function requireClean(trusted: string, repository: string, head: string) {
    await git(trusted, ['read-tree', head]);
    // A fresh trusted index and empty config prevent worker filters/index flags
    // from changing cleanliness. Repository attributes may normalize data only.
    if (await git(trusted, ['--work-tree=' + path.resolve(repository), 'status', '--porcelain', '--untracked-files=all']))
        throw Error('Candidate has staged, unstaged, or untracked changes');
    // The worker index is data too. Read it under trusted configuration to catch
    // staged edits even if the working tree was put back to HEAD afterward.
    const index = await within(path.join(repository, '.git'), 'index');
    const input = await open(index, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
        if (!(await input.stat()).isFile()) throw Error('Git index must be a regular file');
        await writeFile(path.join(trusted, 'index'), await input.readFile());
    } finally { await input.close(); }
    if (await git(trusted, ['diff', '--cached', '--name-only', '--no-ext-diff', '--no-textconv', '-z', head]))
        throw Error('Candidate has staged, unstaged, or untracked changes');
}

export async function cleanRepository(repository: string, base: string) {
    await snapshot(repository, async (trusted, head) => {
        if (head !== base) throw Error(`Source base changed: expected ${base}, found ${head}`);
        await requireClean(trusted, repository, head);
        if (await git(trusted, ['ls-tree', '--name-only', head, '.gitmodules']))
            throw Error('Submodules are not supported by this factory profile');
    });
}
export async function createStore(repository: string, target: string, base: string) {
    await mkdir(path.dirname(target), { recursive: true });
    await snapshot(repository, async (trusted, head) => {
        if (head !== base) throw Error('Source base changed');
        await requireClean(trusted, repository, head);
        await git(path.dirname(target), ['clone', '--bare', '--template=', '--no-hardlinks', '--no-local', '--', trusted, target]);
        await git(target, ['config', '--remove-section', 'remote.origin']);
    });
}
export async function checkout(store: string, target: string, candidate: string) {
    if (!/^[a-f0-9]{40}$/.test(candidate)) throw Error('Invalid candidate identity');
    await mkdir(path.dirname(target), { recursive: true });
    await git(path.dirname(target), ['clone', '--no-hardlinks', '--no-local', '--', store, target]);
    await git(target, ['fetch', '--no-tags', '--', store, candidate]);
    await git(target, ['config', '--remove-section', 'remote.origin']);
    await git(target, ['checkout', '--detach', candidate]);
    if (await git(target, ['rev-parse', 'HEAD']) !== candidate) throw Error('Verifier candidate mismatch');
    await git(target, ['config', 'user.name', 'Factory developer']);
    await git(target, ['config', 'user.email', 'factory@localhost']);
}
export function allowed(file: string, paths: string[]) { return paths.some(p => p === '.' || file === p || file.startsWith(p.replace(/\/$/, '') + '/')); }
export async function importCandidate(store: string, source: string, base: string, allowedPaths: string[]) {
    if (!/^[a-f0-9]{40}$/.test(base)) throw Error('Invalid base identity');
    return snapshot(source, async (trusted, candidate) => {
        await requireClean(trusted, source, candidate);
        await git(trusted, ['merge-base', '--is-ancestor', base, candidate]);
        const files = (await git(trusted, ['diff', '--name-only', '--no-ext-diff', '--no-textconv', '--no-renames', '-z', base, candidate])).split('\0').filter(Boolean);
        if (!files.length)
            throw Error('Candidate contains no implementation change');
        for (const file of files) {
            relativePath(file);
            if (!allowed(file, allowedPaths))
                throw Error(`Candidate changed disallowed path ${file}`);
        }
        const modeLines = (await git(trusted, ['ls-tree', '-r', candidate])).split('\n');
        if (modeLines.some(x => x.startsWith('160000')))
            throw Error('Candidate added an unsupported submodule');
        await git(store, ['fetch', '--no-tags', '--', trusted, `${candidate}:refs/candidates/${candidate}`]);
        if (await git(store, ['rev-parse', `refs/candidates/${candidate}`]) !== candidate)
            throw Error('Imported candidate mismatch');
        return { candidate, files };
    });
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
