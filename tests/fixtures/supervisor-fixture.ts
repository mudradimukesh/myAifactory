// Supervisor process for tests: the real superviseRun with a runtime that runs an unsandboxed
// node worker. The worker forks a grandchild, reports its pid on stderr, and runs until stopped.
import path from 'node:path';
import { SupervisorConflict, superviseRun } from '../../src/coordinator.ts';
import { PauseGate, runProcess } from '../../src/process.ts';
import { LocalRuntime } from '../../src/runtime.ts';
import type { Job } from '../../src/runtime.ts';
import { Store } from '../../src/store.ts';

const worker = `/*dev1-supervisor-worker*/const c=require('node:child_process').spawn(process.execPath,['-e','/*dev1-supervisor-grandchild*/setInterval(()=>{},1000)'],{stdio:'ignore'});process.stderr.write(String(c.pid));setInterval(()=>{},1000)`;

class WorkerRuntime extends LocalRuntime {
  override async preflight() { return []; }
  override async execute(job: Job) {
    return runProcess({ argv: [process.execPath, '-e', worker], cwd: job.workspace,
      stdoutPath: path.join(job.captureDir, 'stdout.log'), stderrPath: path.join(job.captureDir, 'stderr.log'),
      timeoutMs: job.timeoutMs, maxLogBytes: job.maxLogBytes, signal: job.signal, pause: job.pause, onSpawn: job.onSpawn });
  }
}

const [root, run, launchId] = process.argv.slice(2);
const abort = new AbortController();
process.once('SIGTERM', () => abort.abort());
process.once('SIGINT', () => abort.abort());
try {
  await superviseRun(new Store(root), run, launchId, new WorkerRuntime(), { signal: abort.signal, pause: new PauseGate() });
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = error instanceof SupervisorConflict ? 3 : 1;
}
