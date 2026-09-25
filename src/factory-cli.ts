import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { SupervisorConflict, createRun, liveSupervisor, registerVisualReview, resumeRun, runRun, stepRun, superviseRun } from './coordinator.ts';
import { PauseGate } from './process.ts';
import { LocalRuntime } from './runtime.ts';
import { Store } from './store.ts';

export async function main(args: string[]): Promise<void> {
    const [command, root, run, ...rest] = args;
    if (!root || !run) throw Error('Usage: factory-cli <init|step|run|resume|supervise|visual-register|status> <state-root> <run-id> [arguments]');
    const store = new Store(root);
    let state;
    if ((command === 'step' || command === 'run' || command === 'resume') && await liveSupervisor(await store.read(run)))
        throw Error('A factory supervisor is running this run; use the dashboard controls');
    switch (command) {
        case 'init': {
            const [file, owner, statement] = rest;
            if (!file || !owner || !statement) throw Error('init requires project.json, approval owner, and approval statement');
            state = await createRun(store, run, JSON.parse(await readFile(file, 'utf8')), { owner, statement });
            break;
        }
        case 'step': state = await stepRun(store, run); break;
        case 'run': state = await runRun(store, run); break;
        case 'resume': state = await resumeRun(store, run); break;
        case 'supervise': {
            const [launchId] = rest;
            if (!launchId) throw Error('supervise requires a launch id');
            const abort = new AbortController();
            process.once('SIGTERM', () => abort.abort());
            process.once('SIGINT', () => abort.abort());
            try { state = await superviseRun(store, run, launchId, new LocalRuntime(), { signal: abort.signal, pause: new PauseGate() }); }
            catch (error) {
                if (!(error instanceof SupervisorConflict)) throw error;
                process.stderr.write(error.message + '\n');
                process.exitCode = 3;
                return;
            }
            break;
        }
        case 'visual-register': {
            const [sourceRoot, manifestFile] = rest;
            if (!sourceRoot || !manifestFile) throw Error('visual-register requires source-root and manifest.json');
            state = await registerVisualReview(store, run, sourceRoot, JSON.parse(await readFile(manifestFile, 'utf8')));
            break;
        }
        case 'status': state = await store.read(run); break;
        default: throw Error('Unknown factory command');
    }
    process.stdout.write(JSON.stringify({ run: state.id, status: state.status, revision: state.revision,
        attempts: state.attempts.length, candidate: state.candidate ?? null,
        reportedTokens: state.reportedTokens, unknownUsage: state.unknownUsage,
        reason: state.reason ?? null }) + '\n');
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
    main(process.argv.slice(2)).catch(error => {
        process.stderr.write((error instanceof Error ? error.message : String(error)) + '\n');
        process.exitCode = 1;
    });
}
