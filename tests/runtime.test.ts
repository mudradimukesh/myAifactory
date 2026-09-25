import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, writeFile, symlink, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { LocalRuntime, validateAuthHome } from '../src/runtime.ts';
import type { Job } from '../src/runtime.ts';
import { projectSchema } from '../src/contracts.ts';

async function fixture(run: (job: Job, root: string) => Promise<void>) {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), 'factory-native-')));
  const model = { provider: 'codex', model: 'fixture', effort: 'low' };
  const check = { id: 'unit', argv: ['node', '--test'], timeoutMs: 5000, requirements: ['behavior'] };
  const project = projectSchema.parse({
    schemaVersion: 2, name: 'fixture', repository: '/unused-source', base: 'a'.repeat(40),
    recipient: 'fixture', brief: 'fixture', policies: ['fixture'], requirements: ['behavior'],
    checks: [check], artifact: 'result.txt', artifactCheck: { ...check, id: 'artifact' }, allowedPaths: ['.'],
    runtime: { kind: 'macos-sandbox', toolPaths: [path.dirname(await realpath(process.execPath))], network: 'loopback', authHomes: { codex: null, claude: null } },
    models: { coordinator: model, developer: model, reviewer: model, inspector: model },
    limits: { maxAttempts: 4, maxReworks: 1, attemptTimeoutMs: 5000, maxWallMs: 10000, maxReportedTokens: 10000, verificationReserveAttempts: 2, maxLogBytes: 16384 },
    billing: 'subscription-only', retentionDays: 30,
  });
  for (const dir of ['workspace', 'policy', 'output', 'capture', 'scratch']) await mkdir(path.join(root, dir));
  const job: Job = {
    id: 'fixture', project, workspace: path.join(root, 'workspace'), policyDir: path.join(root, 'policy'),
    outputDir: path.join(root, 'output'), captureDir: path.join(root, 'capture'), scratchDir: path.join(root, 'scratch'),
    argv: [process.execPath, '-e', ''], readOnlySource: true, network: 'none', timeoutMs: 5000, maxLogBytes: 16384,
    signal: new AbortController().signal,
  };
  try { await run(job, root); } finally { await rm(root, { recursive: true, force: true }); }
}

test('native sandbox protects capture and readonly source while allowing proposals, scratch and stdin', { skip: process.platform !== 'darwin' }, async () => {
  await fixture(async (job, root) => {
    await writeFile(path.join(job.workspace, 'source.txt'), 'original');
    const secret = path.join(root, 'runner-private.txt');
    await writeFile(secret, 'private');
    await symlink(job.captureDir, path.join(job.outputDir, 'capture-link'));
    const script = `
      const fs=require('node:fs'), cp=require('node:child_process');
      const denied=[];
      for(const [kind,file] of ${JSON.stringify([
        ['write', path.join(job.workspace, 'source.txt')], ['write', path.join(job.policyDir, 'new')],
        ['write', path.join(job.captureDir, 'stdout.log')], ['write', path.join(job.outputDir, 'capture-link/stdout.log')],
        ['read', secret], ['read', path.join(job.captureDir, 'invocation.json')],
      ])}) {
        try { if(kind==='write')fs.writeFileSync(file,'forged');else fs.readFileSync(file); denied.push(false); }
        catch(e) { denied.push(e.code==='EPERM'||e.code==='EACCES'); }
      }
      fs.writeFileSync(${JSON.stringify(path.join(job.outputDir, 'proposal.txt'))},'proposal');
      fs.writeFileSync(process.env.TMPDIR+'/scratch.txt','scratch');
      const descendant=cp.spawnSync(process.execPath,['-e', 'try{require("fs").readFileSync(process.argv[1]);process.exit(1)}catch(e){process.exit(e.code==="EPERM"||e.code==="EACCES"?0:2)}',${JSON.stringify(secret)}]);
      const signalDenied=(()=>{try{process.kill(${process.pid},0);return false}catch(e){return e.code==='EPERM'}})();
      process.stdout.write(JSON.stringify({denied,descendant:descendant.status,signalDenied,input:fs.readFileSync(0,'utf8'),home:process.env.HOME}));
    `;
    const result = await new LocalRuntime().execute({ ...job, argv: [process.execPath, '-e', script], stdin: 'task input' });
    assert.equal(result.reason, 'completed', await readFile(path.join(job.captureDir, 'stderr.log'), 'utf8'));
    const output = JSON.parse(await readFile(path.join(job.captureDir, 'stdout.log'), 'utf8'));
    assert.deepEqual(output.denied, [true, true, true, true, true, true]);
    assert.equal(output.descendant, 0);
    assert.equal(output.signalDenied, true);
    assert.equal(output.input, 'task input');
    assert.equal(output.home, path.join(job.scratchDir, 'home'));
    assert.equal(await readFile(path.join(job.workspace, 'source.txt'), 'utf8'), 'original');
    assert.equal(await readFile(path.join(job.outputDir, 'proposal.txt'), 'utf8'), 'proposal');
  });
});

test('native developer can write only its workspace and network policy is enforced', { skip: process.platform !== 'darwin' }, async () => {
  for (const network of ['none', 'loopback'] as const) await fixture(async job => {
    const script = `const fs=require('fs'),net=require('net');fs.writeFileSync('changed.txt','changed');const s=net.createServer(c=>c.end('ok'));s.on('error',e=>{console.log(e.code);});s.listen(0,'127.0.0.1',()=>{const c=net.connect(s.address().port,'127.0.0.1');c.on('data',d=>process.stdout.write(d));c.on('end',()=>s.close());c.on('error',e=>{console.log(e.code);s.close()});});`;
    const result = await new LocalRuntime().execute({ ...job, network, readOnlySource: false, argv: [process.execPath, '-e', script] });
    assert.equal(result.reason, 'completed', await readFile(path.join(job.captureDir, 'stderr.log'), 'utf8'));
    const output = (await readFile(path.join(job.captureDir, 'stdout.log'), 'utf8')).trim();
    assert.equal(output, network === 'none' ? 'EPERM' : 'ok');
    assert.equal(await readFile(path.join(job.workspace, 'changed.txt'), 'utf8'), 'changed');
  });
});

test('native runtime copies only dedicated credentials and returns a refreshed credential', { skip: process.platform !== 'darwin' }, async () => {
  await fixture(async (job, root) => {
    const auth = path.join(root, 'auth');
    await mkdir(auth);
    const original = JSON.stringify({ auth_mode: 'chatgpt', tokens: { fixture: 'not-a-live-credential' } });
    const refreshed = JSON.stringify({ auth_mode: 'chatgpt', tokens: { fixture: 'rotated-fixture' } });
    await writeFile(path.join(auth, 'auth.json'), original);
    job.project.runtime.authHomes.codex = auth;
    assert.deepEqual(await new LocalRuntime().preflight(job.project), []);
    const script = `const fs=require('fs');const p=process.env.CODEX_HOME+'/auth.json';const mode=JSON.parse(fs.readFileSync(p)).auth_mode;fs.writeFileSync(p,${JSON.stringify(refreshed)});let denied=false;try{fs.readFileSync(${JSON.stringify(path.join(auth, 'auth.json'))})}catch(e){denied=e.code==='EPERM'};console.log(JSON.stringify({mode,denied}));`;
    const result = await new LocalRuntime().execute({ ...job, provider: 'codex', argv: [process.execPath, '-e', script] });
    assert.equal(result.reason, 'completed');
    assert.deepEqual(JSON.parse(await readFile(path.join(job.captureDir, 'stdout.log'), 'utf8')), { mode: 'chatgpt', denied: true });
    assert.equal(await readFile(path.join(auth, 'auth.json'), 'utf8'), refreshed, 'the next attempt must start from the rotated credential');
    await writeFile(path.join(auth, 'config.toml'), 'unapproved');
    await assert.rejects(validateAuthHome('codex', auth), /credentials only/);
  });
});

test('native runtime never returns an invalid credential or overwrites a changed source', { skip: process.platform !== 'darwin' }, async () => {
  for (const [name, write, source] of [
    ['api key', JSON.stringify({ auth_mode: 'apikey', OPENAI_API_KEY: 'fixture' }), undefined],
    ['cleared', '{}', undefined],
    ['not json', 'updated', undefined],
    ['source changed', JSON.stringify({ auth_mode: 'chatgpt', tokens: { fixture: 'job' } }), JSON.stringify({ auth_mode: 'chatgpt', tokens: { fixture: 'other-login' } })],
  ] as const) {
    await fixture(async (job, root) => {
      const auth = path.join(root, 'auth');
      await mkdir(auth);
      const original = JSON.stringify({ auth_mode: 'chatgpt', tokens: { fixture: 'not-a-live-credential' } });
      await writeFile(path.join(auth, 'auth.json'), original);
      job.project.runtime.authHomes.codex = auth;
      const script = `require('fs').writeFileSync(process.env.CODEX_HOME+'/auth.json',${JSON.stringify(write)});`;
      const running = new LocalRuntime().execute({ ...job, provider: 'codex', argv: [process.execPath, '-e', script + (source === undefined ? '' : `require('child_process').execFileSync('/bin/sleep',['1']);`)] });
      if (source !== undefined) { await new Promise(resolve => setTimeout(resolve, 300)); await writeFile(path.join(auth, 'auth.json'), source); }
      assert.equal((await running).reason, 'completed');
      assert.equal(await readFile(path.join(auth, 'auth.json'), 'utf8'), source ?? original, name);
    });
  }
});

test('native configuration rejects old profiles, overlapping roots and privilege escalation', async () => {
  await fixture(async job => {
    assert.equal(projectSchema.safeParse({ ...job.project, schemaVersion: 1 }).success, false);
    assert.equal(projectSchema.safeParse({ ...job.project, runtime: { ...job.project.runtime, image: 'old-image' } }).success, false);
    if (process.platform !== 'darwin') return;
    const runtime = new LocalRuntime();
    await assert.rejects(runtime.execute({ ...job, captureDir: job.outputDir }), /disjoint/);
    await assert.rejects(runtime.execute({ ...job, env: { HOME: '/Users/other' } }), /Reserved/);
    await assert.rejects(runtime.execute({ ...job, network: 'outbound' }), /exceeds/);
    await assert.rejects(runtime.execute({ ...job, project: { ...job.project, runtime: { ...job.project.runtime, toolPaths: ['/'] } } }), /Tool directories/);
  });
});


test('outbound sandbox can reach the macOS DNS resolver while restricted jobs cannot', { skip: process.platform !== 'darwin' }, async () => {
  for (const network of ['none', 'loopback', 'outbound'] as const) await fixture(async job => {
    job.project.runtime.network = 'outbound';
    const script = `const net=require('node:net');const socket=net.createConnection('/private/var/run/mDNSResponder');socket.once('connect',()=>{console.log('connected');socket.end();});socket.once('error',error=>{console.log(error.code);});`;
    const result = await new LocalRuntime().execute({ ...job, network, argv: [process.execPath, '-e', script] });
    assert.equal(result.reason, 'completed');
    assert.equal((await readFile(path.join(job.captureDir, 'stdout.log'), 'utf8')).trim(), network === 'outbound' ? 'connected' : 'EPERM');
  });
});

test('deep attempt paths retain private Unix IPC and remove temporary files after execution', { skip: process.platform !== 'darwin' }, async () => {
  await fixture(async (job) => {
    job.scratchDir = path.join(job.scratchDir, 'deep-attempt-'.repeat(12));
    await mkdir(job.scratchDir);
    const script = `
      const fs=require('node:fs'), net=require('node:net'), path=require('node:path');
      const temp=process.env.TMPDIR;
      const server=net.createServer();
      server.listen(path.join(temp,'worker.sock'),()=>{
        console.log(JSON.stringify({temp,claudeTemp:process.env.CLAUDE_CODE_TMPDIR,zone:(()=>{try{return fs.readFileSync('/etc/localtime').length>0}catch(e){return e.code}})(),mode:fs.statSync(temp).mode&0o777}));
        server.close();
      });
    `;
    const result = await new LocalRuntime().execute({ ...job, argv: [process.execPath, '-e', script] });
    assert.equal(result.reason, 'completed', await readFile(path.join(job.captureDir, 'stderr.log'), 'utf8'));
    const output = JSON.parse(await readFile(path.join(job.captureDir, 'stdout.log'), 'utf8'));
    assert.equal(output.mode, 0o700);
    assert.equal(output.claudeTemp, output.temp);
    assert.equal(output.zone, true, 'Claude Code spins at startup when it cannot read the local time zone');
    assert.match(output.temp, /^\/private\/tmp\/factory-[A-Za-z0-9]+$/);
    await assert.rejects(readFile(path.join(output.temp, 'worker.sock')), { code: 'ENOENT' });
    await assert.rejects(realpath(output.temp), { code: 'ENOENT' });
  });
});
