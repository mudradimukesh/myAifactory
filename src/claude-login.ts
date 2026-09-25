import { spawn } from 'node:child_process';
import type { ChildProcessByStdio } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { constants } from 'node:fs';
import { chmod, copyFile, mkdir, readFile, realpath, rename, rm } from 'node:fs/promises';
import path from 'node:path';
import type { Readable, Writable } from 'node:stream';
import { z } from 'zod';
import { sandboxProfile, validateAuthHome } from './runtime.ts';
import { exists } from './store.ts';

export type ClaudeAuthView = {
  status: 'signed_out' | 'awaiting_code' | 'verifying' | 'ready' | 'failed';
  authHome: string;
  authorizeUrl: string | null;
  account: { authMethod: string | null; subscriptionType: string | null } | null;
  checkedAt: string | null;
  message: string;
};

// The dashboard never runs `setup-token` and never reads the Keychain (ADR 0006). The CLI runs in the
// deny-default worker sandbox, which has no SecurityServer access, so it writes its own plaintext
// credential file. The pasted value is a single-use PKCE authorization code; nothing logs or stores it.
const credentialFile = '.credentials.json';
const maxOutputBytes = 64 * 1024;
const linkTimeoutMs = 30_000;
const loginTimeoutMs = 10 * 60_000;
const checkTimeoutMs = 60_000;
const checkCacheMs = 60_000;
const authorizationCode = z.string().regex(/^[^\s#\p{Cc}]{1,400}#[^\s#\p{Cc}]{1,100}$/u, 'Paste the full code from Claude\'s page, including the part after #');
// Only these fields leave the process. Email, organisation and token fields are dropped by the parse.
const authStatus = z.object({ loggedIn: z.boolean(), authMethod: z.string().nullish(), subscriptionType: z.string().nullish() });
const subscriptionCredential = z.object({ claudeAiOauth: z.record(z.unknown()) });

type Stop = 'cancelled' | 'bad_link' | 'no_link' | 'timeout';
const stopMessages: Record<Exclude<Stop, 'cancelled'>, string> = {
  bad_link: 'Claude printed a sign-in address outside claude.com or claude.ai. Nothing was saved.',
  no_link: 'Claude did not show a sign-in link within 30 seconds. Nothing was saved.',
  timeout: 'Claude sign-in timed out after 10 minutes. Nothing was saved.',
};
type Child = ChildProcessByStdio<Writable, Readable, Readable>;
type WorkDir = { root: string; home: string; tmp: string };
type Login = { phase: 'starting' | 'awaiting_code' | 'verifying'; url: string | null; child: Child | null; stop: Stop | null; linked: Promise<void>; finished: Promise<void> };

// Returns the first OAuth-looking address in the output, null when none has appeared yet, or
// 'invalid' when the address is not an https claude.com or claude.ai authorize page.
export function authorizeLink(output: string): string | null | 'invalid' {
  const candidate = output.match(/https?:\/\/[^\s\x1b\x07"'<>]*(?:oauth|authorize)[^\s\x1b\x07"'<>]*/)?.[0];
  if (!candidate) return null;
  try {
    const url = new URL(candidate);
    const allowed = url.protocol === 'https:' && ['claude.com', 'claude.ai'].includes(url.hostname) && !url.port
      && !url.username && !url.password && url.pathname.endsWith('/oauth/authorize');
    return allowed ? url.href : 'invalid';
  } catch { return 'invalid'; }
}

export class ClaudeLogin {
  readonly privateDir: string;
  readonly authHome: string;
  private readonly options: { command?: string[]; readPaths?: string[] };
  private login: Login | null = null;
  private last: ClaudeAuthView | null = null;
  private checking: Promise<ClaudeAuthView> | null = null;

  // The default command is /opt/homebrew/bin/claude, resolved to its real path at each launch.
  constructor(privateDir: string, options: { command?: string[]; readPaths?: string[] } = {}) {
    this.privateDir = path.resolve(privateDir);
    this.authHome = path.join(this.privateDir, 'auth', 'claude');
    this.options = options;
  }

  async status(check = false): Promise<ClaudeAuthView> {
    const login = this.login;
    if (login?.phase === 'verifying') return this.view('verifying', 'Claude is checking the code.');
    if (login) return this.view('awaiting_code', login.url ? 'Sign in on Claude\'s page, then paste the code it shows.' : 'Waiting for Claude to show a sign-in link.', { authorizeUrl: login.url });
    if (check && !this.fresh()) {
      this.checking ??= this.check().finally(() => { this.checking = null; });
      this.last = await this.checking;
    }
    if (this.last) return this.last;
    return await exists(path.join(this.authHome, credentialFile))
      ? this.view('ready', 'A Claude credential file is saved. Press Check to confirm it with Claude.')
      : this.view('signed_out', 'Claude is not signed in for factory workers.');
  }

  async start(): Promise<ClaudeAuthView> {
    let login = this.login;
    if (!login) {
      let linked = () => {};
      login = { phase: 'starting', url: null, child: null, stop: null, linked: new Promise<void>(resolve => { linked = resolve; }), finished: Promise.resolve() };
      const current = login;
      this.login = current;
      current.finished = this.runLogin(current, linked)
        .catch(() => { this.last = this.view('failed', 'Claude sign-in could not run. Nothing was saved.'); })
        .finally(() => { this.login = null; linked(); });
    }
    await login.linked;
    return this.status();
  }

  async submitCode(code: string): Promise<ClaudeAuthView> {
    const value = authorizationCode.parse(code);
    const login = this.login;
    if (login?.phase !== 'awaiting_code' || !login.child)
      return { ...await this.status(), message: 'No Claude sign-in is waiting for a code.' };
    login.phase = 'verifying';
    login.child.stdin.end(`${value}\n`);
    return this.status();
  }

  async cancel(): Promise<ClaudeAuthView> {
    const login = this.login;
    if (!login) return this.status();
    await this.stop(login, 'cancelled');
    return { ...await this.status(), message: 'Claude sign-in was cancelled. Nothing was saved.' };
  }

  async close(): Promise<void> {
    if (this.login) await this.stop(this.login, 'cancelled');
  }

  private async stop(login: Login, reason: Stop) {
    login.stop ??= reason;
    if (login.child) terminate(login.child);
    await login.finished;
  }

  private fresh() {
    return this.last?.checkedAt != null && Date.now() - Date.parse(this.last.checkedAt) < checkCacheMs;
  }

  private view(status: ClaudeAuthView['status'], message: string, extra: Partial<Pick<ClaudeAuthView, 'authorizeUrl' | 'account' | 'checkedAt'>> = {}): ClaudeAuthView {
    return { status, authHome: this.authHome, authorizeUrl: null, account: null, checkedAt: null, message, ...extra };
  }

  private async runLogin(login: Login, linked: () => void) {
    const dir = await this.workDir();
    try {
      const { child, exit } = await this.sandboxed(dir, ['auth', 'login', '--claudeai'], chunk => {
        if (login.url !== null || login.stop) return;
        const link = authorizeLink(chunk);
        if (link === 'invalid') void this.stop(login, 'bad_link');
        else if (link) { login.url = link; login.phase = 'awaiting_code'; linked(); }
      });
      login.child = child;
      if (login.stop) terminate(child);
      const noLink = setTimeout(() => { if (!login.url) void this.stop(login, 'no_link'); }, linkTimeoutMs);
      const timeout = setTimeout(() => void this.stop(login, 'timeout'), loginTimeoutMs);
      const code = await exit.finally(() => { clearTimeout(noLink); clearTimeout(timeout); });
      if (login.stop === 'cancelled') { this.last = null; return; }
      if (login.stop) { this.last = this.view('failed', stopMessages[login.stop]); return; }
      if (code !== 0 || !await this.install(dir)) {
        this.last = this.view('failed', 'Claude sign-in did not complete. Start it again to get a new link.');
        return;
      }
    } finally {
      await rm(dir.root, { recursive: true, force: true });
    }
    this.last = await this.check();
  }

  // Copies the login's credential into authHome through a temp file and rename.
  private async install(dir: WorkDir): Promise<boolean> {
    const source = path.join(dir.home, '.claude', credentialFile);
    try {
      if (!subscriptionCredential.safeParse(JSON.parse(await readFile(source, 'utf8'))).success) return false;
    } catch { return false; }
    await mkdir(this.authHome, { recursive: true, mode: 0o700 });
    await chmod(path.dirname(this.authHome), 0o700);
    await chmod(this.authHome, 0o700);
    const temp = path.join(this.authHome, `${credentialFile}.${randomBytes(8).toString('hex')}.tmp`);
    try {
      await copyFile(source, temp, constants.COPYFILE_EXCL);
      await chmod(temp, 0o600);
      await rename(temp, path.join(this.authHome, credentialFile));
    } finally {
      await rm(temp, { force: true });
    }
    try { await validateAuthHome('claude', this.authHome); return true; } catch { return false; }
  }

  // Runs `claude auth status --json` against a copy of the saved credential in the same sandbox,
  // so a Keychain login of the user cannot report ready.
  private async check(): Promise<ClaudeAuthView> {
    const checkedAt = new Date().toISOString();
    if (!await exists(path.join(this.authHome, credentialFile)))
      return this.view('signed_out', 'Claude is not signed in for factory workers.', { checkedAt });
    try { await validateAuthHome('claude', this.authHome); }
    catch { return this.view('failed', 'The Claude auth home must hold only a Claude subscription credential file.', { checkedAt }); }
    const dir = await this.workDir();
    try {
      const target = path.join(dir.home, '.claude', credentialFile);
      await copyFile(path.join(this.authHome, credentialFile), target, constants.COPYFILE_EXCL);
      await chmod(target, 0o600);
      const { child, exit, output } = await this.sandboxed(dir, ['auth', 'status', '--json'], () => {});
      child.stdin.end();
      const timeout = setTimeout(() => terminate(child), checkTimeoutMs);
      const code = await exit.finally(() => clearTimeout(timeout));
      let parsed: z.infer<typeof authStatus> | null = null;
      try { parsed = authStatus.parse(JSON.parse(output())); } catch { parsed = null; }
      if (code !== 0 || parsed?.loggedIn !== true)
        return this.view('failed', 'Claude does not accept the saved login. Sign in again.', { checkedAt });
      return this.view('ready', 'Claude accepts the saved login.', {
        checkedAt, account: { authMethod: parsed.authMethod ?? null, subscriptionType: parsed.subscriptionType ?? null },
      });
    } finally {
      await rm(dir.root, { recursive: true, force: true });
    }
  }

  private async workDir(): Promise<WorkDir> {
    await mkdir(this.privateDir, { recursive: true, mode: 0o700 });
    const root = path.join(await realpath(this.privateDir), 'claude-login', randomBytes(12).toString('hex'));
    const home = path.join(root, 'home');
    await mkdir(path.join(home, '.claude'), { recursive: true, mode: 0o700 });
    await mkdir(path.join(root, 'tmp'), { mode: 0o700 });
    return { root, home, tmp: path.join(root, 'tmp') };
  }

  // Output stays in memory, capped at 64 KiB. Nothing from stdout or stderr is written to disk.
  private async sandboxed(dir: WorkDir, args: string[], onOutput: (output: string) => void) {
    const [command = '/opt/homebrew/bin/claude', ...prefix] = this.options.command ?? [];
    const executable = await realpath(command);
    const read = [path.dirname(executable), ...await Promise.all((this.options.readPaths ?? []).map(value => realpath(value))), dir.root];
    const child = spawn('/usr/bin/sandbox-exec', ['-p', sandboxProfile(read, [dir.root], 'outbound', dir.tmp), executable, ...prefix, ...args], {
      cwd: dir.home, detached: true, stdio: ['pipe', 'pipe', 'pipe'],
      env: { PATH: '/usr/bin:/bin', HOME: dir.home, CLAUDE_CONFIG_DIR: path.join(dir.home, '.claude'), TMPDIR: dir.tmp, LANG: 'en_US.UTF-8' },
    });
    let text = '';
    child.stdout.setEncoding('utf8').on('data', (chunk: string) => {
      if (text.length >= maxOutputBytes) return;
      text = (text + chunk).slice(0, maxOutputBytes);
      onOutput(text);
    });
    child.stderr.resume();
    child.stdin.on('error', () => { /* The CLI may exit before reading its input. */ });
    // A group whose leader has just been reaped still exists while members remain, so its id cannot
    // be reused yet. The sweep matches runProcess and removes descendants that kept running.
    child.once('exit', () => { signalGroup(child, 'SIGKILL', true); });
    const exit = new Promise<number | null>(resolve => {
      child.once('error', () => resolve(null));
      child.once('close', code => resolve(code));
    });
    return { child, exit, output: () => text };
  }
}

function signalGroup(child: Child, signal: NodeJS.Signals, afterExit = false) {
  if (!child.pid || (!afterExit && (child.exitCode !== null || child.signalCode !== null))) return;
  try { process.kill(-child.pid, signal); } catch { /* The group is already gone. */ }
}

function terminate(child: Child) {
  signalGroup(child, 'SIGTERM');
  setTimeout(() => signalGroup(child, 'SIGKILL'), 1000).unref();
}
