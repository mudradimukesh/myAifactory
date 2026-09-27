import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile, rename, open, realpath, lstat, unlink, readdir, link } from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';
import lockfile from 'proper-lockfile';
import { buildRunHandoff, saveRunHandoff } from './run-handoff.ts';
import { z } from 'zod';
import { stateSchema, transitions, id as idSchema, ticketProgress, specificationSchema, ticketSchema, questionBatchSchema, visualEvidenceSchema, referenceImageSchema } from './contracts.ts';
import type { State, Status, FileRecord, Event, VisualEvidence, ReferenceImage } from './contracts.ts';
export const sha = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');
export const json = (value: unknown) => JSON.stringify(value, null, 2) + '\n';
export class ReferenceConflict extends Error {}
export class ReferenceImageInvalid extends Error {}
export async function readJson(file: string): Promise<unknown> { return JSON.parse(await readFile(file, 'utf8')); }
const reservationSchema = z.object({ run: idSchema }).strict();
export async function exists(file: string) { try {
    await lstat(file);
    return true;
}
catch (e) {
    if ((e as NodeJS.ErrnoException).code === 'ENOENT')
        return false;
    throw e;
} }
export async function durable(file: string, data: string | Buffer) {
    await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    const tmp = file + '.' + randomUUID() + '.tmp';
    const h = await open(tmp, 'wx', 0o600);
    try {
        await h.writeFile(data);
        await h.sync();
    }
    finally {
        await h.close();
    }
    await rename(tmp, file);
    const d = await open(path.dirname(file), 'r');
    try {
        await d.sync();
    }
    finally {
        await d.close();
    }
}
export async function immutable(file: string, data: string | Buffer) { await mkdir(path.dirname(file), { recursive: true, mode: 0o700 }); const h = await open(file, 'wx', 0o600); try {
    await h.writeFile(data);
    await h.sync();
}
finally {
    await h.close();
} }
export function relativePath(value: string) { if (!value || path.isAbsolute(value) || value.includes('\\') || value.split('/').some(p => p === '..' || p === '') || value.includes('\0'))
    throw Error(`Unsafe relative path: ${value}`); return value; }
export async function within(root: string, value: string) {
    relativePath(value);
    const base = await realpath(root);
    let p = base;
    for (const part of value.split('/')) {
        if (part === '.')
            continue;
        p = path.join(p, part);
        const st = await lstat(p);
        if (st.isSymbolicLink())
            throw Error(`Symlink not allowed: ${value}`);
    }
    if (p !== base && !p.startsWith(base + path.sep))
        throw Error('Path escapes root');
    return p;
}
export async function record(root: string, relative: string): Promise<FileRecord> { const p = await within(root, relative); if (!(await lstat(p)).isFile())
    throw Error('Evidence must be a regular file'); return { path: relative, sha256: sha(await readFile(p)) }; }
export async function verifyFile(root: string, file: FileRecord) { if ((await record(root, file.path)).sha256 !== file.sha256)
    throw Error(`Evidence hash mismatch: ${file.path}`); }
function visualPath(batchId: string, questionId: string, evidence: VisualEvidence) {
    idSchema.parse(batchId);
    idSchema.parse(questionId);
    const extension = evidence.mimeType === 'image/png' ? 'png' : 'jpg';
    return `visual/${batchId}/${questionId}/${evidence.id}.${extension}`;
}
function validImage(bytes: Buffer, mimeType: VisualEvidence['mimeType']) {
    if (mimeType === 'image/png')
        return bytes.length >= 24 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])) && bytes.toString('ascii', 12, 16) === 'IHDR';
    return bytes.length >= 4 && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 && bytes.at(-2) === 255 && bytes.at(-1) === 217;
}
const referenceManifest = z.object({ revision: z.number().int().nonnegative(), images: z.array(referenceImageSchema).max(5) }).strict();
const referenceRoot = (root: string) => path.join(root, '.dashboard', 'reference-images');
const referenceManifestPath = (root: string) => path.join(root, '.dashboard', 'reference-images.json');
async function referenceDirectory(root: string, dir: string, create = false) {
    const base = path.resolve(root), relative = path.relative(base, path.resolve(dir));
    if (relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) throw Error('Reference path escapes store');
    let current = base;
    for (const part of [null, ...relative.split(path.sep).filter(Boolean)]) {
        if (part) current = path.join(current, part);
        let info;
        try { info = await lstat(current); }
        catch (error) {
            if (!create || (error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
            await mkdir(current, { mode: 0o700 });
            info = await lstat(current);
        }
        if (!info.isDirectory() || info.isSymbolicLink() || (info.mode & 0o022) || (process.getuid && info.uid !== process.getuid())) throw Error('Reference directory is unsafe');
    }
}
export async function privateFileBytes(root: string, file: string, maxBytes: number, expectedSize?: number) {
    await referenceDirectory(root, path.dirname(file));
    const handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
        const info = await handle.stat();
        if (!info.isFile() || info.nlink !== 1 || (info.mode & 0o077) || (process.getuid && info.uid !== process.getuid()) ||
            info.size > maxBytes || (expectedSize !== undefined && info.size !== expectedSize)) throw Error('Reference file is unsafe');
        const buffer = Buffer.allocUnsafe((expectedSize ?? maxBytes) + 1);
        let offset = 0;
        while (offset < buffer.length) {
            const { bytesRead } = await handle.read(buffer, offset, buffer.length - offset, null);
            if (!bytesRead) break;
            offset += bytesRead;
        }
        if (offset > maxBytes || offset !== info.size) throw Error('Reference file changed during read');
        await referenceDirectory(root, path.dirname(file));
        return buffer.subarray(0, offset);
    } finally { await handle.close(); }
}
export async function referenceBytes(root: string, file: string, expectedSize: number) {
    return privateFileBytes(root, file, 5_000_000, expectedSize);
}
export async function publishReference(root: string, file: string, bytes: Buffer, replaceIncomplete = false) {
    await referenceDirectory(root, path.dirname(file), true);
    const ownTemp = new RegExp(`^${path.basename(file).replace(/[.*+?^{}$()|[\]\\]/g, '\\$&')}\\.[0-9a-f-]{36}\\.tmp$`);
    for (const name of await readdir(path.dirname(file))) if (ownTemp.test(name)) await unlink(path.join(path.dirname(file), name));
    if (replaceIncomplete && await exists(file)) {
        const existing = await privateFileBytes(root, file, 5_000_000);
        if (existing.length === bytes.length && sha(existing) === sha(bytes)) return;
    }
    const tmp = `${file}.${randomUUID()}.tmp`;
    const handle = await open(tmp, 'wx', 0o600);
    try {
        try {
            await handle.writeFile(bytes);
            await handle.sync();
        } finally { await handle.close(); }
        if (replaceIncomplete) await rename(tmp, file);
        else {
            try { await link(tmp, file); }
            catch (error) {
                if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
                const existing = await referenceBytes(root, file, bytes.length);
                if (sha(existing) !== sha(bytes)) throw Error('Reference image content mismatch');
            }
        }
        if (await exists(tmp)) await unlink(tmp);
        const dir = await open(path.dirname(file), 'r'); try { await dir.sync(); } finally { await dir.close(); }
    } finally { if (await exists(tmp)) await unlink(tmp); }
}
function referenceImageValid(bytes: Buffer, mimeType: ReferenceImage['mimeType']) {
    if (mimeType === 'image/png') return bytes.length >= 24 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])) && bytes.toString('ascii', 12, 16) === 'IHDR';
    return bytes.length >= 4 && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 && bytes.at(-2) === 255 && bytes.at(-1) === 217;
}
export class Store {
    root: string;
    constructor(root: string) { this.root = path.resolve(root); }
    async referenceImages(): Promise<{ revision: number; images: ReferenceImage[] }> {
        try { return referenceManifest.parse(JSON.parse((await privateFileBytes(this.root, referenceManifestPath(this.root), 16_384)).toString('utf8'))); }
        catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return { revision: 0, images: [] }; throw error; }
    }
    async readReference(image: ReferenceImage) {
        const value = referenceImageSchema.parse(image);
        const manifest = await this.referenceImages();
        if (!manifest.images.some(item => item.sha256 === value.sha256)) throw Error('Reference image is not in the draft manifest');
        const file = path.join(referenceRoot(this.root), value.path.slice('references/'.length));
        const bytes = await referenceBytes(this.root, file, value.bytes);
        if (!referenceImageValid(bytes, value.mimeType) || sha(bytes) !== value.sha256) throw Error('Reference image content mismatch');
        return { bytes, mimeType: value.mimeType, sha256: value.sha256 };
    }
    async readRunReference(run: string, image: ReferenceImage) {
        const value = referenceImageSchema.parse(image);
        const state = await this.read(run);
        if (!state.project.referenceImages?.some(item => item.sha256 === value.sha256)) throw Error('Reference image is not in the run manifest');
        const file = path.join(this.dir(run), value.path);
        const bytes = await referenceBytes(this.root, file, value.bytes);
        if (!referenceImageValid(bytes, value.mimeType) || sha(bytes) !== value.sha256) throw Error('Reference image content mismatch');
        return { bytes, mimeType: value.mimeType, sha256: value.sha256 };
    }
    async addReference(bytes: Buffer, mimeType: ReferenceImage['mimeType'], expectedRevision: number) {
        if (bytes.length === 0 || bytes.length > 5_000_000) throw Error('Reference image exceeds 5,000,000 bytes');
        if (!referenceImageValid(bytes, mimeType)) throw new ReferenceImageInvalid('Reference image content does not match its MIME type');
        return this.lock('reference-images', async () => {
            const manifest = await this.referenceImages();
            if (manifest.revision !== expectedRevision) throw new ReferenceConflict('Stale reference image revision');
            const hash = sha(bytes), extension = mimeType === 'image/png' ? 'png' : 'jpg';
            const image = { sha256: hash, mimeType, bytes: bytes.length, path: `references/${hash}.${extension}` } satisfies ReferenceImage;
            if (manifest.images.some(item => item.sha256 === hash)) {
                await this.readReference(image);
                return manifest;
            }
            if (manifest.images.length >= 5) throw new ReferenceConflict('A maximum of 5 reference images is allowed');
            await referenceDirectory(this.root, referenceRoot(this.root), true);
            const file = path.join(referenceRoot(this.root), `${hash}.${extension}`);
            await publishReference(this.root, file, bytes, true);
            const next = { revision: manifest.revision + 1, images: [...manifest.images, image] };
            await durable(referenceManifestPath(this.root), json(next));
            return next;
        }, true);
    }
    async removeReference(hash: string, expectedRevision: number) {
        if (!/^[a-f0-9]{64}$/.test(hash)) throw Error('Invalid reference image hash');
        return this.lock('reference-images', async () => {
            const manifest = await this.referenceImages();
            if (manifest.revision !== expectedRevision) throw new ReferenceConflict('Stale reference image revision');
            const image = manifest.images.find(item => item.sha256 === hash);
            if (!image) throw Error('Reference image not found');
            const next = { revision: manifest.revision + 1, images: manifest.images.filter(item => item.sha256 !== hash) };
            await durable(referenceManifestPath(this.root), json(next));
            await referenceDirectory(this.root, referenceRoot(this.root));
            try { await unlink(path.join(referenceRoot(this.root), image.path.slice('references/'.length))); } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
            return next;
        }, true);
    }
    dir(run: string) { idSchema.parse(run); return path.join(this.root, run); }
    async importVisualEvidence(run: string, batchId: string, questionId: string, entries: VisualEvidence[], sourceRoot: string): Promise<VisualEvidence[]> {
        idSchema.parse(batchId);
        idSchema.parse(questionId);
        const images = z.array(visualEvidenceSchema).min(1).max(200).parse(entries);
        if (new Set(images.map(image => image.id)).size !== images.length) throw Error('Duplicate visual evidence identity');
        const state = await this.read(run);
        const imported: VisualEvidence[] = [];
        for (const image of images) {
            if (image.candidate !== state.candidate || image.specDigest !== state.specDigest) throw Error('Visual evidence identity is stale');
            const source = await within(sourceRoot, image.path);
            const info = await lstat(source);
            if (!info.isFile() || info.size > 20_000_000) throw Error('Visual evidence must be a regular image under 20 MB');
            const bytes = await readFile(source);
            if (!validImage(bytes, image.mimeType) || sha(bytes) !== image.sha256) throw Error('Visual evidence content mismatch');
            const output = { ...image, path: visualPath(batchId, questionId, image) };
            const destination = path.join(this.dir(run), output.path);
            try { await immutable(destination, bytes); }
            catch (error) {
                if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
                await this.readVisualEvidence(run, batchId, questionId, output);
            }
            imported.push(output);
        }
        return imported;
    }
    async readVisualEvidence(run: string, batchId: string, questionId: string, value: VisualEvidence) {
        const evidence = visualEvidenceSchema.parse(value);
        if (evidence.path !== visualPath(batchId, questionId, evidence)) throw Error('Visual evidence path mismatch');
        const file = await within(this.dir(run), evidence.path);
        const info = await lstat(file);
        if (!info.isFile() || info.size > 20_000_000) throw Error('Visual evidence must be a regular image under 20 MB');
        const bytes = await readFile(file);
        if (!validImage(bytes, evidence.mimeType) || sha(bytes) !== evidence.sha256) throw Error('Visual evidence content mismatch');
        return { bytes, mimeType: evidence.mimeType, sha256: evidence.sha256 };
    }
    /** Record writers wait for a short write to finish. Execution and launch locks fail fast so a second owner never queues. */
    async lock<T>(name: string, fn: () => Promise<T>, wait = false): Promise<T> {
        await mkdir(this.root, { recursive: true, mode: 0o700 });
        const target = path.join(this.root, name);
        if (!await exists(target))
            await writeFile(target, '', { flag: 'a', mode: 0o600 });
        let compromised: Error | undefined;
        // A writer must outlast the stale window, or a holder killed mid-write fails every record write until the lock ages out.
        const release = await lockfile.lock(target, { realpath: false, stale: 10000, update: 2000, retries: wait ? { retries: 60, minTimeout: 25, maxTimeout: 250 } : 0, onCompromised: e => { compromised = e; } });
        try {
            const r = await fn();
            if (compromised)
                throw compromised;
            return r;
        }
        finally {
            await release();
        }
    }
    async execution<T>(run: string, fn: () => Promise<T>, wait = false): Promise<T> {
        return this.lock('execution', async () => {
            const reservation = path.join(this.root, 'active.json');
            if (await exists(reservation)) {
        const active = reservationSchema.parse(await readJson(reservation));
                if (active.run !== run)
                    throw Error(`Run ${active.run} holds the state root; suspend or cancel it first`);
            }
            await durable(reservation, json({ run }));
            return fn();
        }, wait);
    }
    /** The run holding the state-root reservation, if any. */
    async holder(): Promise<string | null> { const p = path.join(this.root, 'active.json'); return await exists(p) ? reservationSchema.parse(await readJson(p)).run : null; }
    async release(run: string) {
        await this.lock('execution', async () => {
            const p = path.join(this.root, 'active.json');
            if (await exists(p) && reservationSchema.parse(await readJson(p)).run === run)
                await unlink(p);
        }, true);
    }
    async read(run: string): Promise<State> { const s = stateSchema.parse(await readJson(path.join(this.dir(run), 'state.json'))); if (s.id !== run)
        throw Error('Run identity mismatch'); return s; }
    async reconcile(run: string, s: State) {
        const p = path.join(this.dir(run), 'events.jsonl');
        let raw = await exists(p) ? await readFile(p, 'utf8') : '';
        const cut = raw.lastIndexOf('\n');
        const complete = cut < 0 ? '' : raw.slice(0, cut + 1);
        const lines = complete.split('\n').filter(Boolean);
        const events = lines.map((line, i) => { let e: Event; try {
            e = JSON.parse(line);
        }
        catch {
            throw Error('Event log corruption before trailing record');
        } if (e.sequence !== i + 1)
            throw Error('Event sequence conflict'); return e; });
        if (events.length > s.revision)
            throw Error('Event log is ahead of state');
        for (let i = 0; i < events.length; i++)
            if (json(events[i]) !== json(s.history[i]))
                throw Error('Event content conflict');
        if (s.history.length !== s.revision || json(s.history.at(-1)) !== json(s.lastEvent))
            throw Error('State event history conflict');
        if (events.length < s.revision - 1)
            throw Error('Event history missing before last transition');
        if (raw !== complete)
            await durable(p, complete);
        if (events.length < s.revision) {
            const h = await open(p, 'a', 0o600);
            try {
                await h.writeFile(JSON.stringify(s.lastEvent) + '\n');
                await h.sync();
            }
            finally {
                await h.close();
            }
        }
        const stateFile = path.join(this.dir(run), 'state.json');
        let persistedRevision = 0;
        try { persistedRevision = stateSchema.parse(JSON.parse(await readFile(stateFile, 'utf8'))).revision; } catch { /* state may be between atomic writes */ }
        if (persistedRevision === s.revision) {
            const handoff = await buildRunHandoff(this, s);
            const files = [path.join(this.dir(run), 'HANDOFF.md'), path.join(this.dir(run), 'handoff.json')];
            const expected = [handoff.markdown, json(handoff)];
            for (let i = 0; i < files.length; i++) {
                let current = '';
                try { current = await readFile(files[i], 'utf8'); } catch { /* repair below */ }
                if (current !== expected[i]) await durable(files[i], expected[i]);
            }
        }
    }
    async create(s: State) { stateSchema.parse(s); return this.lock('run-' + s.id, async () => { const d = this.dir(s.id); if (await exists(path.join(d, 'state.json')))
        throw Error('Run already exists'); const handoff = await buildRunHandoff(this, s); await mkdir(d, { recursive: true, mode: 0o700 }); await durable(path.join(d, 'state.json'), json(s)); await this.reconcile(s.id, s); await saveRunHandoff(this, handoff); }, true); }
    async update(run: string, type: string, detail: unknown, fn: (s: State) => void | Promise<void>) {
        return this.lock('run-' + run, async () => {
            const s = await this.read(run);
            await this.reconcile(run, s);
            await fn(s);
            for (const ticket of s.tickets ?? []) {
                const status = ticketProgress(s, ticket);
                if (ticket.status !== status) { ticket.status = status; ticket.updatedAt = new Date().toISOString(); }
            }
            s.revision++;
            s.updatedAt = new Date().toISOString();
            s.lastEvent = { sequence: s.revision, at: s.updatedAt, type, detail };
            s.history.push(s.lastEvent);
            stateSchema.parse(s);
            const handoff = await buildRunHandoff(this, s);
            await durable(path.join(this.dir(run), 'state.json'), json(s));
            await this.reconcile(run, s);
            await saveRunHandoff(this, handoff);
            return s;
        }, true);
    }
    async recordCollaboration(run: string, value: unknown) {
        const input = z.object({ expectedRevision: z.number().int().positive(),
            specifications: z.array(specificationSchema), tickets: z.array(ticketSchema), questionBatches: z.array(questionBatchSchema),
        }).strict().parse(value);
        return this.update(run, 'collaboration_recorded', {}, state => {
            if (state.revision !== input.expectedRevision) throw new Error('Stale collaboration revision');
            for (const spec of input.specifications) {
                if (sha(spec.content) !== spec.digest) throw new Error('Specification digest mismatch');
                const previous = state.specifications?.find(record => record.id === spec.id);
                if (previous && json(previous) !== json(spec)) throw new Error('Approved specification revisions are immutable; use a new ID');
            }
            if (state.specifications?.some(spec => !input.specifications.some(record => record.id === spec.id)))
                throw new Error('Approved specification history must be preserved');
            if (state.questionBatches?.some(batch => !input.questionBatches.some(record => json(record) === json(batch))))
                throw new Error('Question and answer history must be preserved');
            state.specifications = input.specifications;
            state.tickets = input.tickets;
            state.questionBatches = input.questionBatches;
        });
    }
    async transition(run: string, next: Status, reason: string) { return this.update(run, 'transition', { next, reason }, s => { move(s, next, reason); }); }
    async runs() { if (!await exists(this.root))
        return []; const names = (await readdir(this.root, { withFileTypes: true })).filter(x => x.isDirectory() && idSchema.safeParse(x.name).success).map(x => x.name);
        // A directory without state.json is a crashed creation or another tool's workspace (Headroom), not a run.
        const runs: string[] = []; for (const name of names) if (await exists(path.join(this.root, name, 'state.json'))) runs.push(name); return runs; }
}
export function move(s: State, next: Status, reason: string) {
    if (!transitions[s.status].includes(next))
        throw Error(`Invalid transition ${s.status} -> ${next}`);
    if (s.status === 'awaiting_input' && next !== 'cancelled' && next !== s.priorStatus)
        throw Error('Decision must resume the recorded prior state');
    if (next === 'awaiting_input')
        s.priorStatus = s.status;
    s.status = next;
    s.reason = reason;
}

/**
 * Reset returns failed, cancelled, or execution-stage awaiting_input runs to ready.
 * A run paused at the candidate (tester clarification) keeps its candidate and returns to candidate.
 */
export function resettable(s: State) {
    return s.status === 'failed' || s.status === 'cancelled'
        || (s.status === 'awaiting_input' && ['ready', 'running', 'candidate', 'verifying'].includes(s.priorStatus ?? ''));
}

export function reset(s: State, reason: string) {
    if (!resettable(s)) throw Error(`Invalid reset from ${s.status}${s.priorStatus ? ` after ${s.priorStatus}` : ''}`);
    s.status = s.status === 'awaiting_input' && s.priorStatus === 'candidate' ? 'candidate' : 'ready';
    s.reason = reason;
}
