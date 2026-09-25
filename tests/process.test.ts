import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { PauseGate, groupAlive, identify, runProcess, terminateOwned } from '../src/process.ts';

async function withLogs(run: (paths: { stdoutPath: string; stderrPath: string }) => Promise<void>): Promise<void> {
  const dir = await mkdtemp(join(tmpdir(), 'factory-process-'));
  try {
    await run({ stdoutPath: join(dir, 'stdout.log'), stderrPath: join(dir, 'stderr.log') });
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

test('runs an argv vector without a shell and excludes inherited secrets', async () => {
  await withLogs(async (paths) => {
    const inherited = process.env.OPENAI_API_KEY;
    process.env.OPENAI_API_KEY = 'must-not-reach-child';
    try {
      const result = await runProcess({
        argv: [process.execPath, '-e', 'process.stdout.write(JSON.stringify({arg:process.argv[1],secret:process.env.OPENAI_API_KEY ?? null,explicit:process.env.EXPLICIT}))', 'a; echo injected'],
        cwd: process.cwd(), env: { EXPLICIT: 'allowed' }, ...paths,
        timeoutMs: 5000, maxLogBytes: 4096,
      });
      assert.equal(result.reason, 'completed');
      assert.deepEqual(JSON.parse(await readFile(paths.stdoutPath, 'utf8')), {
        arg: 'a; echo injected', secret: null, explicit: 'allowed',
      });
      assert.equal(await readFile(paths.stderrPath, 'utf8'), '');
      assert.ok(Date.parse(result.endedAt) >= Date.parse(result.startedAt));
      assert.equal((await stat(paths.stdoutPath)).mode & 0o777, 0o600);
    } finally {
      if (inherited === undefined) delete process.env.OPENAI_API_KEY;
      else process.env.OPENAI_API_KEY = inherited;
    }
  });
});

test('redacts a secret split across writes and keeps log files exclusive', async () => {
  await withLogs(async (paths) => {
    const result = await runProcess({
      argv: [process.execPath, '-e', "process.stdout.write('before sec'); setTimeout(() => process.stdout.end('ret after'), 20)"],
      cwd: process.cwd(), ...paths, timeoutMs: 5000, maxLogBytes: 4096,
      redact: ['secret'],
    });
    assert.equal(result.reason, 'completed');
    assert.equal(await readFile(paths.stdoutPath, 'utf8'), 'before [REDACTED] after');
    await assert.rejects(runProcess({
      argv: [process.execPath, '-e', ''], cwd: process.cwd(), ...paths,
      timeoutMs: 5000, maxLogBytes: 4096,
    }), { code: 'EEXIST' });
  });
});

test('stops a process group after timeout even when a descendant holds output pipes', async () => {
  await withLogs(async (paths) => {
    const code = "require('node:child_process').spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:['ignore','inherit','inherit']});";
    const result = await runProcess({
      argv: [process.execPath, '-e', code], cwd: process.cwd(), ...paths,
      timeoutMs: 100, maxLogBytes: 4096,
    });
    assert.equal(result.reason, 'timeout');
  });
});

test('timeout kills descendants that ignore termination and close their output pipes', async () => {
  await withLogs(async paths => {
    let pid: number | undefined;
    try {
      const descendant = "process.on('SIGTERM',()=>{});process.send('ready');setInterval(()=>{},1000)";
      const parent = `const child=require('node:child_process').spawn(process.execPath,['-e',${JSON.stringify(descendant)}],{stdio:['ignore','ignore','ignore','ipc']});child.on('message',()=>process.stdout.write(String(child.pid)));setInterval(()=>{},1000)`;
      const result = await runProcess({ argv: [process.execPath, '-e', parent], cwd: process.cwd(), ...paths, timeoutMs: 500, maxLogBytes: 4096 });
      assert.equal(result.reason, 'timeout');
      pid = Number(await readFile(paths.stdoutPath, 'utf8'));
      assert.ok(Number.isInteger(pid) && pid > 0);
      const deadline = Date.now() + 1500;
      let alive = true;
      while (Date.now() < deadline) {
        try { process.kill(pid, 0); } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error;
          alive = false; break;
        }
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      assert.equal(alive, false);
    } finally { if (pid) { try { process.kill(pid, 'SIGKILL'); } catch {} } }
  });
});

test('redacts the complete secret when configured secrets share a prefix across chunks', async () => {
  await withLogs(async paths => {
    const result = await runProcess({
      argv: [process.execPath, '-e', "process.stdout.write('token');setTimeout(()=>process.stdout.end('-suffix'),50)"],
      cwd: process.cwd(), ...paths, timeoutMs: 5000, maxLogBytes: 4096, redact: ['token', 'token-suffix'],
    });
    assert.equal(result.reason, 'completed');
    assert.equal(await readFile(paths.stdoutPath, 'utf8'), '[REDACTED]');
  });
});

for (const exitCode of [0, 7]) {
  test(`parent exit ${exitCode} stops descendants and preserves captured output`, async () => {
    await withLogs(async paths => {
      let pid: number | undefined;
      try {
        const descendant = "process.on('SIGTERM',()=>{});process.send('ready');setInterval(()=>{},1000)";
        const parent = `const child=require('node:child_process').spawn(process.execPath,['-e',${JSON.stringify(descendant)}],{stdio:['ignore','ignore','ignore','ipc']});child.on('message',()=>{process.stdout.write(String(child.pid));process.stderr.write('retained');process.exitCode=${exitCode};child.disconnect();child.unref()})`;
        const result = await runProcess({ argv: [process.execPath, '-e', parent], cwd: process.cwd(), ...paths, timeoutMs: 5000, maxLogBytes: 4096 });
        pid = Number(await readFile(paths.stdoutPath, 'utf8'));
        assert.ok(Number.isInteger(pid) && pid > 0);
        assert.equal(result.reason, exitCode === 0 ? 'completed' : 'exit_error');
        assert.equal(result.exitCode, exitCode);
        assert.equal(await readFile(paths.stderrPath, 'utf8'), 'retained');
        const deadline = Date.now() + 1500;
        let alive = true;
        while (Date.now() < deadline) {
          try { process.kill(pid, 0); } catch (error) {
            if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error;
            alive = false; break;
          }
          await new Promise(resolve => setTimeout(resolve, 20));
        }
        assert.equal(alive, false);
      } finally { if (pid) { try { process.kill(pid, 'SIGKILL'); } catch {} } }
    });
  });
}

test('parent exit closes inherited descendant pipes without waiting for timeout', async () => {
  await withLogs(async paths => {
    const descendant = "process.send('ready');setInterval(()=>{},1000)";
    const parent = `const child=require('node:child_process').spawn(process.execPath,['-e',${JSON.stringify(descendant)}],{stdio:['ignore','inherit','inherit','ipc']});child.on('message',()=>{process.stdout.write('done');child.disconnect();child.unref()})`;
    const result = await runProcess({ argv: [process.execPath, '-e', parent], cwd: process.cwd(), ...paths, timeoutMs: 1000, maxLogBytes: 4096 });
    assert.equal(result.reason, 'completed');
    assert.equal(await readFile(paths.stdoutPath, 'utf8'), 'done');
  });
});

for (const finalFlush of [false, true]) {
  test(`capture failure during ${finalFlush ? 'final flush' : 'execution'} returns a failed result`, async t => {
    await withLogs(async paths => {
      const original = fs.fsyncSync;
      t.mock.method(fs, 'fsyncSync', () => { throw new Error('simulated capture failure'); });
      syncBuiltinESMExports();
      try {
        const code = finalFlush ? "process.stdout.write('sec')" : "process.stdout.write('capture');setInterval(()=>{},1000)";
        const result = await runProcess({ argv: [process.execPath, '-e', code], cwd: process.cwd(), ...paths, timeoutMs: 1000, maxLogBytes: 4096, redact: ['secret'] });
        assert.equal(result.reason, 'capture_error');
      } finally {
        fs.fsyncSync = original;
        syncBuiltinESMExports();
      }
    });
  });
}

test('stops a cancelled process and reports an output limit', async () => {
  await withLogs(async (paths) => {
    const controller = new AbortController();
    const pending = runProcess({
      argv: [process.execPath, '-e', 'setInterval(()=>{},1000)'], cwd: process.cwd(), ...paths,
      timeoutMs: 5000, maxLogBytes: 4096, signal: controller.signal,
    });
    setTimeout(() => controller.abort(), 50);
    assert.equal((await pending).reason, 'cancelled');
  });
  await withLogs(async (paths) => {
    const result = await runProcess({
      argv: [process.execPath, '-e', "process.stdout.write('x'.repeat(4096));process.stderr.write('y')"],
      cwd: process.cwd(), ...paths, timeoutMs: 5000, maxLogBytes: 4096,
    });
    assert.equal(result.reason, 'output_limit');
    const size = (await stat(paths.stdoutPath)).size + (await stat(paths.stderrPath)).size;
    assert.equal(size, 4096);
  });
});

test('a limit signal stops the process quickly with its reason', { timeout: 10000 }, async () => {
  await withLogs(async (paths) => {
    const controller = new AbortController();
    const pending = runProcess({
      argv: [process.execPath, '-e', 'setInterval(()=>{},1000)'], cwd: process.cwd(), ...paths,
      timeoutMs: 30000, maxLogBytes: 4096, limit: controller.signal,
    });
    const started = Date.now();
    setTimeout(() => controller.abort('token_limit'), 50);
    const result = await pending;
    assert.equal(result.reason, 'token_limit');
    assert.ok(Date.now() - started < 5000);
  });
});

for (const reason of ['stall_start', 'stall_idle'] as const) test(`a ${reason} signal preserves its reason`, { timeout: 10000 }, async () => {
  await withLogs(async (paths) => {
    const controller = new AbortController();
    const pending = runProcess({ argv: [process.execPath, '-e', 'setInterval(()=>{},1000)'], cwd: process.cwd(), ...paths,
      timeoutMs: 30000, maxLogBytes: 4096, limit: controller.signal });
    setTimeout(() => controller.abort(reason), 25);
    assert.equal((await pending).reason, reason);
  });
});

test('reports spawn failures and retains empty logs', async () => {
  await withLogs(async (paths) => {
    const result = await runProcess({
      argv: ['/definitely/missing/program'], cwd: process.cwd(), ...paths,
      timeoutMs: 5000, maxLogBytes: 4096,
    });
    assert.equal(result.reason, 'spawn_error');
    assert.equal(await readFile(paths.stdoutPath, 'utf8'), '');
  });
});

test('persists output while the worker is still running', async () => {
  await withLogs(async (paths) => {
    const controller = new AbortController();
    const pending = runProcess({
      argv: [process.execPath, '-e', "process.stdout.write('checkpoint');setInterval(()=>{},1000)"],
      cwd: process.cwd(), ...paths, timeoutMs: 5000, maxLogBytes: 4096,
      signal: controller.signal,
    });
    try {
      const deadline = Date.now() + 2000;
      let output = '';
      while (Date.now() < deadline) {
        output = await readFile(paths.stdoutPath, 'utf8').catch(() => '');
        if (output === 'checkpoint') break;
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      assert.equal(output, 'checkpoint');
    } finally {
      controller.abort();
      await pending;
    }
  });
});

const execAsync = promisify(execFile);
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
async function psStat(pid: number): Promise<string> {
  try { return (await execAsync('/bin/ps', ['-o', 'stat=', '-p', String(pid)])).stdout.trim(); }
  catch { return ''; }
}
async function until<T>(read: () => Promise<T>, done: (value: T) => boolean, ms = 3000): Promise<T> {
  const deadline = Date.now() + ms;
  let value = await read();
  while (!done(value) && Date.now() < deadline) { await sleep(25); value = await read(); }
  return value;
}
function alive(pid: number) {
  try { process.kill(pid, 0); return true; } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ESRCH') return false; throw error; }
}
// The leader prints the grandchild pid once the grandchild runs. The marker lets a ps filter find leftovers.
const forking = (grandchild: string) => `/*dev1-fixture*/const c=require('node:child_process').spawn(process.execPath,['-e',${JSON.stringify('/*dev1-fixture*/' + grandchild)}],{stdio:['ignore','ignore','ignore','ipc']});c.on('message',()=>process.stdout.write(String(c.pid)));setInterval(()=>{},1000)`;
const readyGrandchild = "process.send('ready');setInterval(()=>{},1000)";

test('pause stops every group member including a grandchild and resume continues them', async () => {
  await withLogs(async paths => {
    const gate = new PauseGate();
    const controller = new AbortController();
    let leader = 0;
    const pending = runProcess({ argv: [process.execPath, '-e', forking(readyGrandchild)], cwd: process.cwd(), ...paths,
      timeoutMs: 20000, maxLogBytes: 4096, signal: controller.signal, pause: gate, onSpawn: async pid => { leader = pid; } });
    try {
      const grandchild = Number(await until(() => readFile(paths.stdoutPath, 'utf8').catch(() => ''), text => text.length > 0));
      assert.ok(leader > 0 && grandchild > 0 && grandchild !== leader);
      gate.pause();
      const frozen = await until(() => Promise.all([psStat(leader), psStat(grandchild)]), all => all.every(value => value.includes('T')));
      assert.ok(frozen.every(value => value.includes('T')), `stopped states ${frozen}`);
      gate.resume();
      const thawed = await until(() => Promise.all([psStat(leader), psStat(grandchild)]), all => all.every(value => /^[SR]/.test(value)));
      assert.ok(thawed.every(value => /^[SR]/.test(value)), `running states ${thawed}`);
      controller.abort();
      const result = await pending;
      assert.equal(result.reason, 'cancelled');
      assert.ok(result.pausedMs > 0);
      assert.equal(await until(async () => alive(grandchild), value => !value), false);
    } finally { controller.abort(); await pending; }
  });
});

test('a pause freezes the timeout and reports the frozen time', async () => {
  await withLogs(async paths => {
    const gate = new PauseGate();
    const started = Date.now();
    let settled = false;
    const pending = runProcess({ argv: [process.execPath, '-e', '/*dev1-fixture*/setInterval(()=>{},1000)'], cwd: process.cwd(), ...paths,
      timeoutMs: 1500, maxLogBytes: 4096, pause: gate });
    void pending.then(() => { settled = true; });
    await sleep(200);
    gate.pause();
    await sleep(3000);
    assert.equal(settled, false, 'the timeout fired during the pause');
    gate.resume();
    const result = await pending;
    assert.equal(result.reason, 'timeout');
    assert.ok(result.pausedMs >= 2500, `pausedMs ${result.pausedMs}`);
    assert.ok(Date.now() - started >= 1500 + 2500);
  });
});

test('cancel while paused kills a descendant that ignores SIGTERM', async () => {
  await withLogs(async paths => {
    const gate = new PauseGate();
    const controller = new AbortController();
    const pending = runProcess({ argv: [process.execPath, '-e', forking("process.on('SIGTERM',()=>{});" + readyGrandchild)], cwd: process.cwd(), ...paths,
      timeoutMs: 20000, maxLogBytes: 4096, signal: controller.signal, pause: gate });
    let grandchild = 0;
    try {
      grandchild = Number(await until(() => readFile(paths.stdoutPath, 'utf8').catch(() => ''), text => text.length > 0));
      gate.pause();
      assert.ok((await until(() => psStat(grandchild), value => value.includes('T'))).includes('T'));
      controller.abort();
      const result = await pending;
      assert.equal(result.reason, 'cancelled');
      assert.equal(await until(async () => alive(grandchild), value => !value), false);
    } finally { controller.abort(); await pending; if (grandchild && alive(grandchild)) process.kill(grandchild, 'SIGKILL'); }
  });
});

test('a rejected onSpawn record kills the group and reports spawn_error', async () => {
  await withLogs(async paths => {
    let leader = 0;
    const result = await runProcess({ argv: [process.execPath, '-e', '/*dev1-fixture*/setInterval(()=>{},1000)'], cwd: process.cwd(), ...paths,
      timeoutMs: 20000, maxLogBytes: 4096, onSpawn: async pid => { leader = pid; throw new Error('record failed'); } });
    assert.equal(result.reason, 'spawn_error');
    assert.ok(leader > 0);
    assert.equal(await groupAlive(leader), false);
  });
});

test('terminateOwned signals nothing for a changed identity and stops the proven owner', async () => {
  const child = spawn(process.execPath, ['-e', '/*dev1-fixture*/setInterval(()=>{},1000)'], { detached: true, stdio: 'ignore' });
  try {
    const owned = await until(() => identify(child.pid!), value => value !== null);
    assert.ok(owned);
    assert.equal(owned.pgid, child.pid);
    assert.equal(await terminateOwned({ ...owned, started: 'Thu Jan  1 00:00:00 1970' }, 0), 'not_owned');
    await sleep(200);
    assert.deepEqual(await identify(owned.pid), owned, 'the process was signalled');
    assert.equal(await terminateOwned(owned, 1000), 'gone');
    assert.equal(await groupAlive(owned.pgid), false);
    assert.equal(await identify(owned.pid), null);
  } finally { try { process.kill(-child.pid!, 'SIGKILL'); } catch { /* Already gone. */ } }
});
