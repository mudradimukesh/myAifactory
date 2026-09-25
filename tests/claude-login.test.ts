import assert from 'node:assert/strict';
import { mkdtemp, readdir, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { authorizeLink, ClaudeLogin } from '../src/claude-login.ts';
import type { ClaudeAuthView } from '../src/claude-login.ts';

const fixtures = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures');
const macOnly = { skip: process.platform !== 'darwin' };
const login = (privateDir: string, mode: 'good' | 'bad-host') =>
  new ClaudeLogin(privateDir, { command: [process.execPath, path.join(fixtures, 'fake-claude.mjs'), mode], readPaths: [fixtures] });

async function withDir(fn: (dir: string) => Promise<void>) {
  const dir = await mkdtemp(path.join(tmpdir(), 'factory-claude-login-'));
  try { await fn(dir); } finally { await rm(dir, { recursive: true, force: true }); }
}

async function settled(auth: ClaudeLogin, views: ClaudeAuthView[]) {
  for (let i = 0; i < 100; i++) {
    const view = await auth.status();
    views.push(view);
    if (view.status !== 'verifying') return view;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw Error('Login did not settle');
}

function groupGone(pid: number) {
  try { process.kill(-pid, 0); return false; } catch (error) { return (error as NodeJS.ErrnoException).code === 'ESRCH'; }
}

test('authorizeLink strips an OSC 8 hyperlink wrapper instead of swallowing it into the URL', () => {
  assert.equal(
    authorizeLink('\x1b]8;;https://claude.com/cai/oauth/authorize?code=true&state=s\x07link\x1b]8;;\x07'),
    'https://claude.com/cai/oauth/authorize?code=true&state=s',
  );
});

test('dashboard Claude login saves only the credential file and never exposes secrets or identity', macOnly, () => withDir(async dir => {
  const auth = login(dir, 'good');
  const views = [await auth.status()];
  assert.equal(views[0].status, 'signed_out');
  const [first, second] = await Promise.all([auth.start(), auth.start()]);
  views.push(first, second, await auth.start());
  assert.equal(first.status, 'awaiting_code');
  assert.match(first.authorizeUrl ?? '', /^https:\/\/claude\.com\/cai\/oauth\/authorize\?/);
  assert.ok(views.slice(1).every(view => view.authorizeUrl === first.authorizeUrl), 'a second start reuses the pending login');
  for (const bad of [
    '', 'two words', 'line\nbreak', 'x'.repeat(513),
    'good-code#', `good-code#${'y'.repeat(101)}`, `${'x'.repeat(401)}#fixture-state`,
    'good\x01code#fixture-state', 'good-code#fixture\x01state', 'good-code#fixture\tstate',
  ])
    await assert.rejects(auth.submitCode(bad), /full code/);
  await assert.rejects(auth.submitCode('good-code'), /full code/);
  assert.equal((await auth.status()).status, 'awaiting_code');
  views.push(await auth.submitCode('good-code#fixture-state'));
  assert.equal(views.at(-1)?.status, 'verifying');
  const ready = await settled(auth, views);
  assert.equal(ready.status, 'ready', ready.message);
  assert.deepEqual(ready.account, { authMethod: 'claude.ai', subscriptionType: 'max' });
  views.push(await auth.status(true));
  assert.equal(views.at(-1)?.checkedAt, ready.checkedAt, 'a check within 60 s is cached');
  const text = JSON.stringify(views);
  assert.doesNotMatch(text, /fixture-secret|fixture-person|Fixture Org/);
  assert.deepEqual(await readdir(auth.authHome), ['.credentials.json']);
  assert.equal((await stat(path.join(auth.authHome, '.credentials.json'))).mode & 0o777, 0o600);
  assert.equal((await stat(auth.authHome)).mode & 0o777, 0o700);
  assert.deepEqual(await readdir(path.join(dir, 'claude-login')), []);
  assert.ok(groupGone(Number(new URL(first.authorizeUrl ?? '').searchParams.get('state'))), 'a finished login leaves no process group');
}));

test('a rejected code fails the login and saves nothing', macOnly, () => withDir(async dir => {
  const auth = login(dir, 'good');
  await auth.start();
  await auth.submitCode('wrong-code#fixture-state');
  const view = await settled(auth, []);
  assert.equal(view.status, 'failed');
  assert.deepEqual(await readdir(path.join(dir, 'claude-login')), []);
  assert.equal((await readdir(dir)).includes('auth'), false);
}));

test('a sign-in link outside claude.com or claude.ai is refused', macOnly, () => withDir(async dir => {
  const auth = login(dir, 'bad-host');
  const view = await auth.start();
  assert.equal(view.status, 'failed');
  assert.equal(view.authorizeUrl, null);
  assert.doesNotMatch(JSON.stringify(view), /example\.net/);
  assert.deepEqual(await readdir(path.join(dir, 'claude-login')), []);
}));

test('cancel stops the whole login process group and removes its directory', macOnly, () => withDir(async dir => {
  const auth = login(dir, 'good');
  const pending = await auth.start();
  const pid = Number(new URL(pending.authorizeUrl ?? 'https://claude.com').searchParams.get('state'));
  assert.ok(pid > 0 && !groupGone(pid));
  const cancelled = await auth.cancel();
  assert.equal(cancelled.status, 'signed_out');
  for (let i = 0; i < 20 && !groupGone(pid); i++) await new Promise(resolve => setTimeout(resolve, 100));
  assert.ok(groupGone(pid), 'login group survived cancel');
  assert.deepEqual(await readdir(path.join(dir, 'claude-login')), []);
  assert.equal((await auth.submitCode('good-code#fixture-state')).message, 'No Claude sign-in is waiting for a code.');
}));

test('close at server shutdown stops a pending login', macOnly, () => withDir(async dir => {
  const auth = login(dir, 'good');
  const pid = Number(new URL((await auth.start()).authorizeUrl ?? 'https://claude.com').searchParams.get('state'));
  await auth.close();
  for (let i = 0; i < 20 && !groupGone(pid); i++) await new Promise(resolve => setTimeout(resolve, 100));
  assert.ok(groupGone(pid));
}));
