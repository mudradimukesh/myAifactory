import assert from 'node:assert/strict';
import { test } from 'node:test';
import { chmod, link, mkdir, mkdtemp, readFile, stat, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { referenceImageSchema } from '../src/contracts.ts';
import { workerCommand } from '../src/workers.ts';
import { Store, publishReference, sha } from '../src/store.ts';
import { startDashboard } from '../src/dashboard-server.ts';
import { git } from '../src/git.ts';

const hash = 'a'.repeat(64);
const refs = [{ sha256: hash, mimeType: 'image/png' as const, bytes: 24, path: `references/${hash}.png` }];
const route = { baseUrl: 'http://127.0.0.1:8791/v1' };

test('reference metadata accepts only canonical private image paths', () => {
  assert.equal(referenceImageSchema.safeParse(refs[0]).success, true);
  assert.equal(referenceImageSchema.safeParse({ ...refs[0], path: '../outside.png' }).success, false);
  assert.equal(referenceImageSchema.safeParse({ ...refs[0], path: `references/${hash}.gif` }).success, false);
});

test('worker commands attach references without changing Headroom routing', () => {
  const codex = workerCommand({ provider: 'codex', model: 'gpt-test', effort: 'low' }, 'architect', '/tmp/source', 'task', 'policy', route, undefined, undefined, refs);
  const image = codex.args.indexOf('--image');
  assert.equal(codex.args[image + 1], refs[0].path);
  assert.equal(codex.env.OPENAI_BASE_URL, route.baseUrl);
  const claude = workerCommand({ provider: 'claude', model: 'claude-test', effort: 'low' }, 'reviewer', '/tmp/source', 'task', 'policy', route, undefined, undefined, refs);
  assert.deepEqual(claude.args.slice(-2), ['--add-dir', 'references']);
  assert.equal(claude.env.ANTHROPIC_BASE_URL, 'http://127.0.0.1:8791');
});

test('worker commands reject references without a Headroom route', () => {
  assert.throws(() => workerCommand({ provider: 'codex', model: 'gpt-test', effort: 'low' }, 'architect', '/tmp/source', 'task', 'policy', undefined, undefined, undefined, refs), /Headroom route/);
});

test('draft references are content addressed, private, revisioned, and removable', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-reference-'));
  try {
    const store = new Store(root);
    const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZsAAAAASUVORK5CYII=', 'base64');
    const added = await store.addReference(png, 'image/png', 0);
    assert.equal(added.revision, 1);
    assert.deepEqual((await store.readReference(added.images[0])).bytes, png);
    assert.equal((await stat(path.join(root, '.dashboard', 'reference-images', added.images[0].path.slice(11)))).mode & 0o077, 0);
    const duplicate = await store.addReference(png, 'image/png', 1);
    assert.equal(duplicate.revision, 1);
    for (let index = 0; index < 4; index++) {
      const variant = Buffer.from(png);
      variant[23] = index + 10;
      await store.addReference(variant, 'image/png', index + 1);
    }
    const full = await store.referenceImages();
    assert.equal(full.images.length, 5);
    const extra = Buffer.from(png);
    extra[22] = 9;
    await assert.rejects(store.addReference(extra, 'image/png', 5), /maximum of 5/);
    await store.removeReference(added.images[0].sha256, 5);
    assert.equal((await store.referenceImages()).images.length, 4);
    await assert.rejects(readFile(path.join(root, '.dashboard', 'reference-images', added.images[0].path.slice(11))));
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('reference reads reject symlinked or replaced files', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-reference-symlink-'));
  try {
    const store = new Store(root);
    const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZsAAAAASUVORK5CYII=', 'base64');
    const outside = path.join(root, 'outside');
    await mkdir(path.join(root, '.dashboard'), { mode: 0o700 });
    await mkdir(outside, { mode: 0o700 });
    await symlink(outside, path.join(root, '.dashboard', 'reference-images'));
    await assert.rejects(store.addReference(png, 'image/png', 0), /unsafe|symbolic|symlink/i);
    assert.deepEqual(await readFile(path.join(outside, `${sha(png)}.png`)).catch(() => null), null);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('interrupted reference publication can be retried and existing complete copies are reused', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-reference-retry-'));
  try {
    const store = new Store(root);
    const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZsAAAAASUVORK5CYII=', 'base64');
    const dir = path.join(root, '.dashboard', 'reference-images');
    await mkdir(dir, { recursive: true, mode: 0o700 });
    await writeFile(path.join(dir, `${sha(png)}.png`), png.subarray(0, 12), { mode: 0o600 });
    const added = await store.addReference(png, 'image/png', 0);
    assert.equal(added.images[0].sha256, sha(png));
    assert.deepEqual((await store.readReference(added.images[0])).bytes, png);
    assert.equal((await store.addReference(png, 'image/png', 1)).revision, 1);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('reference publication recovers its own crashed link temp but rejects foreign hard links', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-reference-link-crash-'));
  try {
    const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZsAAAAASUVORK5CYII=', 'base64');
    const dir = path.join(root, 'runs', 'r', 'references');
    const file = path.join(dir, 'a.png');
    await mkdir(dir, { recursive: true, mode: 0o700 });
    await writeFile(file, png, { mode: 0o600 });
    await link(file, `${file}.00000000-0000-4000-8000-000000000000.tmp`);
    await publishReference(root, file, png);
    assert.equal((await stat(file)).nlink, 1);
    await link(file, path.join(root, 'foreign.png'));
    await assert.rejects(publishReference(root, file, png), /unsafe/i);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('reference storage rejects exposed ancestors and symlinked store roots', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-reference-boundary-'));
  try {
    const actual = path.join(root, 'state');
    const store = new Store(actual);
    const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZsAAAAASUVORK5CYII=', 'base64');
    await mkdir(path.join(actual, '.dashboard'), { recursive: true, mode: 0o700 });
    await chmod(path.join(actual, '.dashboard'), 0o777);
    await assert.rejects(store.addReference(png, 'image/png', 0), /unsafe/i);
    await chmod(path.join(actual, '.dashboard'), 0o700);
    const image = (await store.addReference(png, 'image/png', 0)).images[0];
    const alias = path.join(root, 'alias');
    await symlink(actual, alias);
    await assert.rejects(new Store(alias).readReference(image), /unsafe|symlink/i);
    await chmod(path.join(actual, '.dashboard', 'reference-images.json'), 0o666);
    await assert.rejects(store.referenceImages(), /unsafe/i);
    const copy = path.join(actual, 'attempts', 'one', 'policy', 'references', `${sha(png)}.png`);
    await mkdir(path.dirname(copy), { recursive: true, mode: 0o700 });
    const altered = Buffer.from(png); altered[23] ^= 1;
    await writeFile(copy, altered, { mode: 0o600 });
    await assert.rejects(publishReference(actual, copy, png), /mismatch/i);
    assert.deepEqual(await readFile(copy), altered);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('dashboard reference HTTP flow validates uploads and freezes explicit run selection', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-reference-http-'));
  const app = await startDashboard({ root, port: 0 });
  try {
    const { csrfToken } = await (await fetch(`${app.url}/api/session`)).json();
    const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZsAAAAASUVORK5CYII=', 'base64');
    const jpeg = Buffer.from([255, 216, 255, 224, 0, 2, 255, 217]);
    const send = (revision: number, bytes: Buffer, mime: string, csrf = csrfToken) => fetch(`${app.url}/api/reference-images?expectedRevision=${revision}`, {
      method: 'POST', headers: { Origin: app.url, ...(csrf ? { 'X-CSRF-Token': csrf } : {}), 'Content-Type': mime }, body: Uint8Array.from(bytes),
    });
    assert.equal((await send(0, png, 'image/png', '')).status, 403);
    assert.equal((await send(0, Buffer.from('GIF89a'), 'image/gif')).status, 415);
    assert.equal((await send(0, jpeg, 'image/png')).status, 415);
    assert.equal((await send(0, Buffer.alloc(5_000_001), 'image/png')).status, 413);
    let response = await send(0, png, 'image/png');
    assert.equal(response.status, 201);
    let manifest = await response.json();
    assert.equal(manifest.images[0].sha256, sha(png));
    assert.equal((await send(0, jpeg, 'image/jpeg')).status, 409);
    response = await send(1, jpeg, 'image/jpeg');
    assert.equal(response.status, 201);
    manifest = await response.json();
    assert.equal(manifest.images[1].sha256, sha(jpeg));
    const listed = await fetch(`${app.url}/api/reference-images`);
    assert.equal(listed.status, 200);
    assert.deepEqual((await listed.json()).images.map((image: { sha256: string }) => image.sha256), [sha(png), sha(jpeg)]);
    for (let i = 0; i < 3; i++) {
      const variant = Buffer.from(png); variant[23] = i + 10;
      response = await send(manifest.revision, variant, 'image/png');
      assert.equal(response.status, 201);
      manifest = await response.json();
    }
    const sixth = Buffer.from(png); sixth[23] = 25;
    assert.equal((await send(manifest.revision, sixth, 'image/png')).status, 409);
    const draftImage = await fetch(`${app.url}/api/reference-images/${sha(jpeg)}`);
    assert.equal(draftImage.status, 200);
    assert.deepEqual(Buffer.from(await draftImage.arrayBuffer()), jpeg);
    const source = path.join(root, 'source');
    await mkdir(source);
    await git(source, ['init']);
    await git(source, ['config', 'user.name', 'Fixture']);
    await git(source, ['config', 'user.email', 'fixture@localhost']);
    await writeFile(path.join(source, 'README.md'), 'base\n');
    await git(source, ['add', '.']);
    await git(source, ['commit', '-m', 'Base']);
    const base = await git(source, ['rev-parse', 'HEAD']);
    const model = { provider: 'codex', model: 'fixture', effort: 'low' };
    const check = { id: 'unit', argv: ['node', '--version'], cwd: '.', timeoutMs: 1000, requirements: ['behavior'], outputPaths: [] };
    const project = { schemaVersion: 2, name: 'fixture', repository: source, base, recipient: 'operator', brief: 'Match reference', policies: ['policy'], requirements: ['behavior'], checks: [check], artifact: 'README.md', artifactCheck: { ...check, id: 'artifact' }, allowedPaths: ['src'], runtime: { kind: 'macos-sandbox', toolPaths: ['/usr/bin'], network: 'none', authHomes: { codex: null, claude: null } }, models: { coordinator: model, developer: model, reviewer: model, inspector: model }, limits: { maxAttempts: 7, maxReworks: 1, attemptTimeoutMs: 30000, maxWallMs: 300000, maxReportedTokens: 10000, verificationReserveAttempts: 2, maxLogBytes: 16384 }, billing: 'subscription-only', retentionDays: 30 };
    const create = await fetch(`${app.url}/api/runs`, { method: 'POST', headers: { Origin: app.url, 'X-CSRF-Token': csrfToken }, body: JSON.stringify({ id: 'reference-run', project, referenceImageHashes: [sha(png), sha(jpeg)], approval: { owner: 'operator', statement: 'Approved' } }) });
    assert.equal(create.status, 201);
    const selected = await create.json();
    assert.deepEqual(selected, { id: 'reference-run', referenceImageHashes: [sha(png), sha(jpeg)] });
    const remove = await fetch(`${app.url}/api/reference-images/${sha(png)}?expectedRevision=${manifest.revision}`, { method: 'DELETE', headers: { Origin: app.url, 'X-CSRF-Token': csrfToken } });
    assert.equal(remove.status, 200);
    assert.equal((await fetch(`${app.url}/api/reference-images/${sha(jpeg)}?expectedRevision=${manifest.revision}`, { method: 'DELETE', headers: { Origin: app.url, 'X-CSRF-Token': csrfToken } })).status, 409);
    assert.equal((await fetch(`${app.url}/api/reference-images/${sha(png)}`)).status, 404);
    for (const [bytes, mime] of [[png, 'image/png'], [jpeg, 'image/jpeg']] as const) {
      const image = await fetch(`${app.url}/api/runs/reference-run/reference-images/${sha(bytes)}`);
      assert.equal(image.status, 200);
      assert.equal(image.headers.get('content-type'), mime);
      assert.equal(image.headers.get('x-content-type-options'), 'nosniff');
      assert.equal(image.headers.get('cache-control'), 'no-store');
      assert.equal(image.headers.get('etag'), `"${sha(bytes)}"`);
      assert.deepEqual(Buffer.from(await image.arrayBuffer()), bytes);
    }
  } finally { await app.close(); await rm(root, { recursive: true, force: true }); }
});
