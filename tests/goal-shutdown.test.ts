import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { identify, terminateOwnedBatch } from '../src/process.ts';
import { stateSchema } from '../src/contracts.ts';
import { factoryView } from '../src/dashboard.ts';

test('legacy state remains valid and shutdown progress retains owned identities', () => {
    const digest = 'a'.repeat(64);
    const state = {
        schemaVersion: 1, id: 'run', revision: 1, status: 'handoff_ready',
        project: { schemaVersion: 2, name: 'fixture', repository: '/tmp/repo', base: 'a'.repeat(40), recipient: 'operator', brief: 'brief', policies: ['policy'], requirements: ['behavior'],
            checks: [{ id: 'unit', argv: ['node', '--version'], cwd: '.', timeoutMs: 1000, requirements: ['behavior'], outputPaths: [] }], artifact: 'result.txt', artifactCheck: { id: 'artifact', argv: ['node', '--version'], cwd: '.', timeoutMs: 1000, requirements: ['behavior'], outputPaths: [] }, allowedPaths: ['src'],
            runtime: { kind: 'macos-sandbox', toolPaths: ['/usr/bin'], network: 'none', authHomes: { codex: null, claude: null } }, models: { coordinator: { provider: 'codex', model: 'fixture', effort: 'low' }, developer: { provider: 'codex', model: 'fixture', effort: 'low' }, reviewer: { provider: 'codex', model: 'fixture', effort: 'low' }, inspector: { provider: 'codex', model: 'fixture', effort: 'low' } }, limits: { maxAttempts: 3, maxReworks: 0, attemptTimeoutMs: 1000, maxWallMs: 1000, maxReportedTokens: 10, verificationReserveAttempts: 1, maxLogBytes: 1024 }, billing: 'subscription-only', retentionDays: 30 },
        specDigest: digest, profileDigest: digest, bundleDigest: digest, policyDigest: digest, sourceBase: 'a'.repeat(40), candidate: 'a'.repeat(40), createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z', approval: { owner: 'operator', statement: 'approved' }, attempts: [], checks: [], reworks: 0, reportedTokens: 0, unknownUsage: false, elapsedMs: 0, suspended: false,
        lastEvent: { sequence: 1, at: '2026-01-01T00:00:00.000Z', type: 'created', detail: {} }, history: [{ sequence: 1, at: '2026-01-01T00:00:00.000Z', type: 'created', detail: {} }],
        shutdown: { candidate: 'a'.repeat(40), specDigest: digest, phase: 'stopping', startedAt: '2026-01-01T00:00:00.000Z' },
        ownedProcesses: [{ jobId: 'job', segment: 1, process: { pid: 1, pgid: 1, started: 'fixture' } }],
    };
    assert.equal(stateSchema.parse(state).shutdown?.phase, 'stopping');
    const legacy = { ...state };
    delete (legacy as Record<string, unknown>).shutdown;
    delete (legacy as Record<string, unknown>).ownedProcesses;
    assert.equal(stateSchema.parse(legacy).status, 'handoff_ready');
});

test('batch cleanup never signals the current process', async () => {
    const result = await terminateOwnedBatch([
        { pid: process.pid, pgid: process.pid, started: 'fixture' },
    ], 100);
    assert.equal(result[0]?.result, 'not_owned');
});

test('leader-safe owned cleanup behavior in process shutdown coverage', async () => {
    const leader = spawn(process.execPath, ['-e', `const {spawn}=require('node:child_process'); const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'}); process.stdout.write(String(child.pid)+'\\n'); setInterval(()=>{},1000);`], { detached: true, stdio: ['ignore', 'pipe', 'ignore'] });
    try {
        assert.ok(leader.pid);
        const chunks: Buffer[] = [];
        leader.stdout!.on('data', chunk => chunks.push(chunk));
        await once(leader.stdout!, 'data');
        const childPid = Number(Buffer.concat(chunks).toString().trim());
        const owned = await identify(childPid);
        assert.ok(owned);
        assert.equal(owned.pgid, leader.pid);
        assert.equal(await terminateOwnedBatch([owned], 500).then(results => results[0]?.result), 'gone');
        assert.ok(await identify(leader.pid!), 'cleanup killed another member of the supervisor group');
    } finally {
        if (leader.pid) { try { process.kill(-leader.pid, 'SIGKILL'); } catch { /* already gone */ } }
    }
});

test('detached child is signalled and gone before cleanup returns', async () => {
    const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { detached: true, stdio: 'ignore' });
    try {
        assert.ok(child.pid);
        const owned = await identify(child.pid!);
        assert.ok(owned);
        const result = await terminateOwnedBatch([owned!], 1000);
        assert.equal(result[0]?.result, 'gone');
    } finally {
        if (child.pid) { try { process.kill(child.pid, 'SIGKILL'); } catch { /* already gone */ } }
    }
});

test('recorded supervisor exit is idle only after its process is gone', async () => {
    const state = stateSchema.parse({
        schemaVersion: 1, id: 'run', revision: 3, status: 'awaiting_input', priorStatus: 'ready',
        project: { schemaVersion: 2, name: 'fixture', repository: '/tmp/repo', base: 'a'.repeat(40), recipient: 'operator', brief: 'brief', policies: ['policy'], requirements: ['behavior'],
            checks: [{ id: 'unit', argv: ['node', '--version'], cwd: '.', timeoutMs: 1000, requirements: ['behavior'], outputPaths: [] }], artifact: 'result.txt', artifactCheck: { id: 'artifact', argv: ['node', '--version'], cwd: '.', timeoutMs: 1000, requirements: ['behavior'], outputPaths: [] }, allowedPaths: ['src'],
            runtime: { kind: 'macos-sandbox', toolPaths: ['/usr/bin'], network: 'none', authHomes: { codex: null, claude: null } }, models: { coordinator: { provider: 'codex', model: 'fixture', effort: 'low' }, developer: { provider: 'codex', model: 'fixture', effort: 'low' }, reviewer: { provider: 'codex', model: 'fixture', effort: 'low' }, inspector: { provider: 'codex', model: 'fixture', effort: 'low' } }, limits: { maxAttempts: 3, maxReworks: 0, attemptTimeoutMs: 1000, maxWallMs: 1000, maxReportedTokens: 10, verificationReserveAttempts: 1, maxLogBytes: 1024 }, billing: 'subscription-only', retentionDays: 30 },
        specDigest: 'a'.repeat(64), profileDigest: 'a'.repeat(64), bundleDigest: 'a'.repeat(64), policyDigest: 'a'.repeat(64), sourceBase: 'a'.repeat(40), createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z', approval: { owner: 'operator', statement: 'approved' }, attempts: [], checks: [], reworks: 0, reportedTokens: 0, unknownUsage: false, elapsedMs: 0, suspended: false,
        supervisor: { launchId: 'launch', process: { pid: 999999, pgid: 999999, started: 'fixture' }, launchedAt: '2026-01-01T00:00:00.000Z' },
        lastEvent: { sequence: 3, at: '2026-01-01T00:00:00.000Z', type: 'supervisor_exited', detail: { launchId: 'launch', outcome: 'waiting', message: 'awaiting_input' } },
        history: [
            { sequence: 1, at: '2026-01-01T00:00:00.000Z', type: 'created', detail: {} },
            { sequence: 2, at: '2026-01-01T00:00:00.000Z', type: 'supervisor_started', detail: { launchId: 'launch' } },
            { sequence: 3, at: '2026-01-01T00:00:00.000Z', type: 'supervisor_exited', detail: { launchId: 'launch', outcome: 'waiting', message: 'awaiting_input' } },
        ],
    });
    assert.equal((await factoryView(state)).state, 'idle');
    state.history.pop();
    assert.equal((await factoryView(state)).state, 'exited');
});
