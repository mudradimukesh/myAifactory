import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { projectSchema } from '../src/contracts.ts';
import { LocalRuntime } from '../src/runtime.ts';
import type { Job } from '../src/runtime.ts';
import { workerCommand } from '../src/workers.ts';

const baseUrl = 'http://127.0.0.1:8791/p/my-aifactory/v1';
const model = { provider: 'codex' as const, model: 'gpt-6-sol', effort: 'high' as const };

function project(authHome: string) {
  const check = { id: 'behavior', argv: ['node', '--test'], timeoutMs: 5000, requirements: ['behavior'] };
  return projectSchema.parse({
    schemaVersion: 2, name: 'headroom-fixture', repository: '/unused-source', base: 'a'.repeat(40),
    recipient: 'operator', brief: 'Use the exact approved brief', policies: ['Keep original evidence'],
    requirements: ['behavior'], checks: [check], artifact: 'result.txt', artifactCheck: { ...check, id: 'artifact' },
    allowedPaths: ['.'], headroom: { baseUrl },
    runtime: { kind: 'macos-sandbox', toolPaths: ['/usr/bin'], network: 'loopback', authHomes: { codex: authHome, claude: null } },
    models: { coordinator: model, developer: model, reviewer: model, inspector: model },
    limits: { maxAttempts: 4, maxReworks: 1, attemptTimeoutMs: 5000, maxWallMs: 10000,
      maxReportedTokens: 10000, verificationReserveAttempts: 2, maxLogBytes: 16384 },
    billing: 'subscription-only', retentionDays: 30,
  });
}

test('Codex worker routes subscription requests through the approved loopback proxy', () => {
  const command = workerCommand(model, 'developer', '/tmp/source', 'Build the approved feature', 'Keep the brief exact', { baseUrl });
  const overrides = command.args.flatMap((arg, index) => arg === '-c' ? [command.args[index + 1]] : []);
  assert.ok(overrides.includes(`openai_base_url=${JSON.stringify(baseUrl)}`));
  assert.equal(command.env.OPENAI_BASE_URL, baseUrl);
  assert.equal(command.env.OPENAI_API_KEY, undefined);
  assert.ok(command.args.includes('--ignore-user-config'));
  assert.match(command.stdin, /Worker policy:\nKeep the brief exact\n\nTask:\nBuild the approved feature/);
});

test('Headroom routing accepts only an explicit IPv4 loopback origin and project path', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'factory-headroom-schema-'));
  try {
    const valid = project(root);
    assert.equal(valid.headroom?.baseUrl, baseUrl);
    for (const bad of [
      'http://localhost:8791/p/my-aifactory/v1',
      'http://127.0.0.1/p/my-aifactory/v1',
      'https://127.0.0.1:8791/p/my-aifactory/v1',
      'http://127.0.0.1:8791/p/../v1',
      'http://127.0.0.1:8791/p/my-aifactory/v1?upstream=elsewhere',
      'http://192.168.1.1:8791/p/my-aifactory/v1',
    ]) {
      assert.equal(projectSchema.safeParse({ ...valid, headroom: { baseUrl: bad } }).success, false, bad);
    }
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('configured Headroom must report ready and optimized before native worker dispatch',
  { skip: process.platform !== 'darwin' }, async () => {
    const root = await mkdtemp(path.join(tmpdir(), 'factory-headroom-health-'));
    const authHome = path.join(root, 'auth');
    await mkdir(authHome);
    await writeFile(path.join(authHome, 'auth.json'), JSON.stringify({ auth_mode: 'chatgpt', tokens: { fixture: 'not-live' } }));
    const originalFetch = globalThis.fetch;
    try {
      const input = project(authHome);
      const requested: string[] = [];
      globalThis.fetch = async (url) => {
        requested.push(String(url));
        return Response.json({ service: 'headroom-proxy', ready: true, status: 'healthy',
          config: { savings_profile: 'general', optimize: true, cache: false, disable_kompress: true, disable_kompress_fallback: true, disable_kompress_openai: true, force_kompress: false,
            compress_user_messages: false, compress_system_messages: false, disable_kompress_anthropic: true, anthropic_api_url: null } });
      };
      assert.deepEqual(await new LocalRuntime().preflight(input), []);
      assert.deepEqual(requested, ['http://127.0.0.1:8791/health']);

      globalThis.fetch = async () => Response.json({ service: 'headroom-proxy', ready: true, status: 'healthy',
        config: { savings_profile: 'general', optimize: false, cache: false, disable_kompress: true, disable_kompress_fallback: true, disable_kompress_openai: true, force_kompress: false,
          compress_user_messages: false, compress_system_messages: false, disable_kompress_anthropic: true, anthropic_api_url: null } });
      assert.match((await new LocalRuntime().preflight(input)).join(' '), /Headroom|proxy|optimization|optimize/i);

      globalThis.fetch = async () => new Response('unavailable', { status: 503 });
      assert.match((await new LocalRuntime().preflight(input)).join(' '), /Headroom|proxy|ready|503/i);

      globalThis.fetch = async () => Response.json({ service: 'headroom-proxy', ready: true, status: 'healthy',
        config: { savings_profile: 'general', optimize: true, cache: false, disable_kompress: true, disable_kompress_fallback: true, disable_kompress_openai: true, force_kompress: false,
          compress_user_messages: false, compress_system_messages: false, disable_kompress_anthropic: false, anthropic_api_url: null } });
      assert.match((await new LocalRuntime().preflight(input)).join(' '), /Headroom/i);

      globalThis.fetch = async () => Response.json({ service: 'headroom-proxy', ready: true, status: 'healthy',
        config: { savings_profile: 'general', optimize: true, cache: false, disable_kompress: true, disable_kompress_fallback: true, disable_kompress_openai: true, force_kompress: false,
          compress_user_messages: false, compress_system_messages: false, disable_kompress_anthropic: true, anthropic_api_url: 'https://proxy.example' } });
      assert.match((await new LocalRuntime().preflight(input)).join(' '), /Headroom/i);
    } finally {
      globalThis.fetch = originalFetch;
      await rm(root, { recursive: true, force: true });
    }
  });

test('routed worker retains original input and isolated subscription credentials',
  { skip: process.platform !== 'darwin' }, async () => {
    const root = await realpath(await mkdtemp(path.join(tmpdir(), 'factory-headroom-native-')));
    const authHome = path.join(root, 'auth');
    const privateFile = path.join(root, 'private-state.txt');
    await mkdir(authHome);
    await writeFile(path.join(authHome, 'auth.json'), JSON.stringify({ auth_mode: 'chatgpt', tokens: { fixture: 'not-live' } }));
    await writeFile(privateFile, 'private');
    const dirs = ['workspace', 'policy', 'output', 'capture', 'scratch'] as const;
    for (const dir of dirs) await mkdir(path.join(root, dir));
    const input = projectSchema.parse({ ...project(authHome), runtime: {
      ...project(authHome).runtime, toolPaths: [path.dirname(await realpath(process.execPath))],
    } });
    const route = workerCommand(model, 'developer', path.join(root, 'workspace'),
      'Exact approved candidate hash abc123', 'Exact control instruction', { baseUrl });
    const script = `
      const fs = require('node:fs');
      const denied = file => { try { fs.readFileSync(file); return false; }
        catch (e) { return e.code === 'EPERM' || e.code === 'EACCES'; } };
      console.log(JSON.stringify({
        baseUrl: process.env.OPENAI_BASE_URL,
        mode: JSON.parse(fs.readFileSync(process.env.CODEX_HOME + '/auth.json', 'utf8')).auth_mode,
        sourceCredentialDenied: denied(${JSON.stringify(path.join(authHome, 'auth.json'))}),
        privateStateDenied: denied(${JSON.stringify(privateFile)}),
        captureDenied: denied(${JSON.stringify(path.join(root, 'capture', 'invocation.json'))}),
        input: fs.readFileSync(0, 'utf8'),
      }));`;
    const job: Job = { id: 'route-check', project: input,
      workspace: path.join(root, 'workspace'), policyDir: path.join(root, 'policy'),
      outputDir: path.join(root, 'output'), captureDir: path.join(root, 'capture'),
      scratchDir: path.join(root, 'scratch'), argv: [process.execPath, '-e', script],
      stdin: route.stdin, env: route.env, provider: 'codex', readOnlySource: true,
      network: 'none', timeoutMs: 5000, maxLogBytes: 16384, signal: new AbortController().signal };
    try {
      const result = await new LocalRuntime().execute(job);
      assert.equal(result.reason, 'completed', await readFile(path.join(root, 'capture', 'stderr.log'), 'utf8'));
      assert.deepEqual(JSON.parse(await readFile(path.join(root, 'capture', 'stdout.log'), 'utf8')), {
        baseUrl, mode: 'chatgpt', sourceCredentialDenied: true, privateStateDenied: true,
        captureDenied: true, input: route.stdin,
      });
    } finally { await rm(root, { recursive: true, force: true }); }
  });

test('Claude worker reaches Anthropic only through the approved Headroom project path',
  { skip: process.platform !== 'darwin' }, async () => {
    const root = await realpath(await mkdtemp(path.join(tmpdir(), 'factory-headroom-claude-')));
    const authHome = path.join(root, 'auth');
    await mkdir(authHome);
    await writeFile(path.join(authHome, '.credentials.json'), JSON.stringify({ claudeAiOauth: { fixture: 'not-live' } }));
    for (const dir of ['workspace', 'policy', 'output', 'capture', 'scratch']) await mkdir(path.join(root, dir));
    const claude = { provider: 'claude' as const, model: 'sonnet', effort: 'medium' as const };
    const base = project(root);
    const input = projectSchema.parse({ ...base, models: { ...base.models, reviewer: claude },
      runtime: { ...base.runtime, toolPaths: [path.dirname(await realpath(process.execPath))], authHomes: { codex: null, claude: authHome } } });
    const route = workerCommand(claude, 'reviewer', path.join(root, 'workspace'), 'Review', 'Policy', input.headroom);
    const script = `console.log(JSON.stringify(['ANTHROPIC_BASE_URL', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY'].map(name => process.env[name] ?? null)))`;
    const job: Job = { id: 'claude-route-check', project: input,
      workspace: path.join(root, 'workspace'), policyDir: path.join(root, 'policy'),
      outputDir: path.join(root, 'output'), captureDir: path.join(root, 'capture'),
      scratchDir: path.join(root, 'scratch'), argv: [process.execPath, '-e', script],
      stdin: route.stdin, env: route.env, provider: 'claude', readOnlySource: true,
      network: 'none', timeoutMs: 5000, maxLogBytes: 16384, signal: new AbortController().signal };
    const inherited = process.env.ANTHROPIC_BASE_URL;
    const inheritedKey = process.env.ANTHROPIC_API_KEY;
    process.env.ANTHROPIC_BASE_URL = 'http://127.0.0.1:8791';
    process.env.ANTHROPIC_API_KEY = 'fixture-parent-key';
    try {
      const result = await new LocalRuntime().execute(job);
      assert.equal(result.reason, 'completed', await readFile(path.join(root, 'capture', 'stderr.log'), 'utf8'));
      assert.deepEqual(JSON.parse(await readFile(path.join(root, 'capture', 'stdout.log'), 'utf8')),
        ['http://127.0.0.1:8791/p/my-aifactory', null, null]);
    } finally {
      if (inherited === undefined) delete process.env.ANTHROPIC_BASE_URL;
      else process.env.ANTHROPIC_BASE_URL = inherited;
      if (inheritedKey === undefined) delete process.env.ANTHROPIC_API_KEY;
      else process.env.ANTHROPIC_API_KEY = inheritedKey;
      await rm(root, { recursive: true, force: true });
    }
  });
