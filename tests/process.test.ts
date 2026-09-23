import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { runProcess } from '../src/process.ts';

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
