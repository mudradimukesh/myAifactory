import { open } from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';
import { z } from 'zod';
import type { State } from './contracts.ts';
import { Store, durable, json, within } from './store.ts';
import { git } from './git.ts';

type Entry = { text: string; evidence: string[] };
const entry = (text: string, ...evidence: string[]): Entry => ({ text, evidence });

async function redactor(store: Store) {
    let values: string[] = [];
    try {
        const file = await within(store.root, '.dashboard/credentials.json');
        const handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
        try {
            const info = await handle.stat();
            if (!info.isFile() || info.mode & 0o077) throw Error('Stored credentials unavailable');
            const secrets = z.object({ github: z.string().optional(), application: z.record(z.string()) }).strict().parse(JSON.parse(await handle.readFile('utf8')));
            values = [secrets.github ?? '', ...Object.values(secrets.application)].filter(Boolean).sort((a, b) => b.length - a.length);
        } finally { await handle.close(); }
    } catch (error) {
        if (!(error instanceof Error && 'code' in error && error.code === 'ENOENT')) throw error;
    }
    return (text: string) => values.reduce((value, secret) => value.replaceAll(secret, '[redacted]'), text);
}

export async function buildRunHandoff(store: Store, state: State) {
    const redact = await redactor(store);
    const completed: Entry[] = [], current: Entry[] = [], difficulties: Entry[] = [], blocked: Entry[] = [], next: Entry[] = [];
    for (const attempt of state.attempts) {
        const evidence = `attempts/${attempt.id}`;
        if (attempt.status === 'completed') completed.push(entry(`${attempt.role} attempt ${attempt.id} completed. This records execution, not acceptance of its claims.`, evidence));
        if (attempt.status === 'running') current.push(entry(`${attempt.role} is working on ${attempt.id}.`, evidence));
        if (attempt.status === 'failed' || attempt.status === 'interrupted') difficulties.push(entry(`${attempt.role} attempt ${attempt.id} ${attempt.status}.`, evidence));
        for (const segment of attempt.segments ?? []) {
            if (segment.reason && segment.reason !== 'completed') difficulties.push(entry(`${attempt.role} segment ${segment.index}: ${segment.reason}`, `${evidence}/segment-${segment.index}`));
        }
    }
    for (const check of state.checks) {
        const item = entry(`Check ${check.id} ${check.passed ? 'passed' : 'failed'} for candidate ${check.candidate}: ${check.reason}`, check.stdout.path, check.stderr.path);
        (check.passed ? completed : difficulties).push(item);
    }
    for (const finding of state.review?.record.findings ?? []) difficulties.push(entry(`${finding.severity} review finding: ${finding.message}`, finding.evidence, state.review!.file.path));
    if (state.activeJob && !current.length) current.push(entry(`Active job ${state.activeJob.id}: ${state.activeJob.kind}`, 'state.json'));
    if (state.status === 'awaiting_input' || state.status === 'failed' || state.suspended || state.control) blocked.push(entry(state.reason ?? `Run is ${state.status}`, 'state.json', 'events.jsonl'));
    if (state.status === 'verified' || state.status === 'handoff_ready') completed.push(entry('The coordinator verified this candidate through its required gates.', 'state.json'));
    if (state.project.enabledRoles && !state.project.enabledRoles.includes('reviewer')) difficulties.push(entry('Independent reviewer is disabled. Mandatory checks remain the acceptance evidence.', 'state.json'));
    if (state.shutdown?.error) difficulties.push(entry(`Cleanup failed: ${state.shutdown.error}`, 'state.json', 'events.jsonl'));
    else if (state.status === 'handoff_ready' || state.status === 'verified') next.push(entry('The candidate is ready for operator handoff.', 'state.json'));
    else if (state.status === 'cancelled') next.push(entry('This run was cancelled. Start a separately approved run to continue.', 'state.json'));
    else if (blocked.length) next.push(entry('Resolve the recorded blocker, then resume through the coordinator with the current approved task.', 'state.json'));
    else next.push(entry(`Continue the coordinator from ${state.status}; verify the candidate against the current approved task.`, 'state.json'));
    let changedFiles: string[] = [];
    if (state.candidate) {
        try {
            const repository = await within(store.dir(state.id), 'candidates.git');
            changedFiles = (await git(repository, ['diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', state.sourceBase, state.candidate, '--'])).split('\0').filter(Boolean);
        } catch {
            difficulties.push(entry('Candidate file changes could not be read. Inspect the candidate before relying on a change list.', 'state.json'));
        }
    }
    const clean = (items: Entry[]) => items.map(item => ({ text: redact(item.text), evidence: item.evidence.map(redact) }));
    const handoff = { schemaVersion: 1, runId: state.id, revision: state.revision, updatedAt: state.updatedAt, status: state.status,
        recipient: state.project.recipient, ...(state.shutdown ? { shutdown: { phase: state.shutdown.phase, startedAt: state.shutdown.startedAt, completedAt: state.shutdown.completedAt, error: state.shutdown.error } } : {}),
        objective: redact(state.project.brief), completed: clean(completed), current: clean(current), difficulties: clean(difficulties), blocked: clean(blocked), next: clean(next),
        changedFiles: changedFiles.map(redact), source: { candidate: state.candidate, specDigest: state.specDigest,
            referenceImageHashes: (state.project.referenceImages ?? []).map(image => image.sha256).sort(),
            references: (state.project.referenceImages ?? []).map((image, index) => `Reference ${index + 1} ${image.sha256.slice(0, 12)} (${image.mimeType})`) } };
    const sections = [['Completed', handoff.completed], ['Current work', handoff.current], ['Difficulties', handoff.difficulties], ['Blocked', handoff.blocked], ['Next steps', handoff.next]] as const;
    const markdown = [`# Coordinator handoff: ${state.id}`, '', `Recipient: ${handoff.recipient}.`, `Revision ${state.revision}. Updated ${state.updatedAt}. Status: ${state.status}.`, ...(handoff.shutdown ? [`Shutdown: ${handoff.shutdown.phase}.`] : []), '',
        'Historical context only. The current approved task governs new work. Recheck evidence before claiming completion. Source approvals, budgets, and acceptance do not transfer.', '',
        '## Objective', '', handoff.objective, '',
        ...sections.flatMap(([title, items]) => [`## ${title}`, '', ...(items.length ? items.map(item => `- ${item.text} Evidence: ${item.evidence.join(', ')}`) : ['None recorded.']), '']),
        '## Reference screenshots', '', ...(handoff.source.references.length ? handoff.source.references.map(reference => `- ${reference}`) : ['None recorded.']), '',
        '## Candidate file changes', '', ...(handoff.changedFiles.length ? handoff.changedFiles.map(file => `- ${file}`) : ['None recorded.']), '',
        'This record is derived from coordinator state and Git evidence using the writing-for-agents document structure. It does not infer difficulties or successful skill reads from worker prose.', ''].join('\n');
    return { ...handoff, markdown };
}

export async function saveRunHandoff(store: Store, handoff: Awaited<ReturnType<typeof buildRunHandoff>>) {
    await durable(path.join(store.dir(handoff.runId), 'HANDOFF.md'), handoff.markdown);
    await durable(path.join(store.dir(handoff.runId), 'handoff.json'), json(handoff));
    return handoff;
}
