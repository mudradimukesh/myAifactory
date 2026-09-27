import { readFile, readdir, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { z } from 'zod';
import { bundle, factoryRoot } from './bundle.ts';
import { id, role } from './contracts.ts';
import type { State, Role } from './contracts.ts';
import { Store, durable, immutable, json, readJson, sha, within } from './store.ts';
import { buildRunHandoff } from './run-handoff.ts';

const resourcePath = z.string().min(1).max(500).refine(value => !path.isAbsolute(value) && !value.includes('\\') && !value.includes('\0') && value.split('/').every(part => part !== '..' && part !== '.' && part !== ''), 'Use a relative resource path');
const resource = z.object({ path: resourcePath, content: z.string().max(100000) }).strict();
export const skillSchema = resource.extend({ name: id, references: z.array(resource).max(40) }).strict();
const agentSchema = z.object({ role, skills: z.array(skillSchema).min(1).max(40) }).strict().superRefine((value, ctx) => {
    if (new Set(value.skills.map(skill => skill.name)).size !== value.skills.length) ctx.addIssue({ code: 'custom', message: 'Skill names must be unique per agent' });
    const resources = value.skills.flatMap(skill => [skill, ...skill.references]);
    const texts = new Map<string, string>();
    for (const item of resources) {
        if (texts.has(item.path) && texts.get(item.path) !== item.content) ctx.addIssue({ code: 'custom', message: `Conflicting resource ${item.path}` });
        texts.set(item.path, item.content);
    }
});
const inheritedHandoff = z.object({ runId: id, revision: z.number().int().positive(), digest: z.string().regex(/^[a-f0-9]{64}$/), markdown: z.string() }).strict().refine(value => sha(value.markdown) === value.digest, 'Inherited handoff digest mismatch');
export const factoryProfileSchema = z.object({ id, name: z.string().trim().min(1).max(120), description: z.string().max(2000), revision: z.number().int().positive(), inheritedHandoff: inheritedHandoff.optional(), agents: z.array(agentSchema).length(role.options.length) }).strict().superRefine((value, ctx) => {
    if (new Set(value.agents.map(agent => agent.role)).size !== role.options.length) ctx.addIssue({ code: 'custom', message: 'Every agent role is required exactly once' });
});
export type FactoryProfile = z.infer<typeof factoryProfileSchema>;
export class FactoryRevisionConflict extends Error {}
export async function skillLibrary() {
    const source = await bundle();
    return Promise.all(source.lock.skills.map(async skill => ({ name: skill.name, path: skill.path, content: await readFile(await within(factoryRoot, skill.path), 'utf8'), references: await Promise.all(skill.references.map(async ref => ({ path: ref.path, content: await readFile(await within(factoryRoot, ref.path), 'utf8') }))) })));
}
export async function defaultFactory(): Promise<FactoryProfile> {
    const [source, skills] = await Promise.all([bundle(), skillLibrary()]);
    return factoryProfileSchema.parse({ id: 'default', name: 'General software factory', description: 'Repository default agent skills', revision: 1, agents: role.options.map(name => ({ role: name, skills: source.roles.roles[name]!.skills.map(skill => skills.find(item => item.name === skill)) })) });
}
export class FactoryProfiles {
    private readonly store: Store;
    constructor(store: Store) { this.store = store; }
    private directory() { return path.join(this.store.root, '.factories'); }
    async list() {
        await mkdir(this.directory(), { recursive: true, mode: 0o700 });
        await within(this.store.root, '.factories');
        const files = await readdir(this.directory());
        return Promise.all(files.filter(file => file.endsWith('.json') && id.safeParse(file.slice(0, -5)).success).sort().map(async file => {
            const factory = await this.read(file.slice(0, -5));
            return { id: factory.id, name: factory.name, description: factory.description };
        }));
    }
    async read(name: string) {
        id.parse(name);
        const factory = factoryProfileSchema.parse(await readJson(await within(this.store.root, `.factories/${name}.json`)));
        if (factory.id !== name) throw new Error('Factory identity mismatch');
        return factory;
    }
    async create(input: unknown) {
        const { handoffRunId, ...fields } = z.object({ id, name: z.string().trim().min(1).max(120), description: z.string().max(2000).default(''), handoffRunId: id.optional() }).strict().parse(input);
        const handoff = handoffRunId ? await buildRunHandoff(this.store, await this.store.read(handoffRunId)) : undefined;
        const factory = factoryProfileSchema.parse({ ...await defaultFactory(), ...fields, inheritedHandoff: handoff && { runId: handoff.runId, revision: handoff.revision, digest: sha(handoff.markdown), markdown: handoff.markdown } });
        return this.store.lock('factory-' + fields.id, async () => {
            await mkdir(this.directory(), { recursive: true, mode: 0o700 });
            await within(this.store.root, '.factories');
            try { await immutable(path.join(this.directory(), `${fields.id}.json`), json(factory)); }
            catch (error) { if (error instanceof Error && 'code' in error && error.code === 'EEXIST') throw new FactoryRevisionConflict('Factory already exists'); throw error; }
            return factory;
        }, true);
    }
    async saveAgent(name: string, agentRole: string, input: unknown) {
        id.parse(name); const selected = role.parse(agentRole);
        const change = z.object({ revision: z.number().int().positive(), skills: z.array(skillSchema).min(1).max(40) }).strict().parse(input);
        return this.store.lock('factory-' + name, async () => {
            const factory = await this.read(name);
            if (factory.revision !== change.revision) throw new FactoryRevisionConflict('Factory changed; reload before saving');
            const next = factoryProfileSchema.parse({ ...factory, revision: factory.revision + 1, agents: factory.agents.map(agent => agent.role === selected ? { role: selected, skills: change.skills } : agent) });
            await durable(await within(this.store.root, `.factories/${name}.json`), json(next));
            return next;
        }, true);
    }
}
export async function runFactory(store: Store, state: State): Promise<FactoryProfile | null> {
    if (!state.skillSnapshot) return null;
    const raw = await readFile(await within(store.dir(state.id), state.skillSnapshot.path), 'utf8');
    if (sha(raw) !== state.skillSnapshot.sha256 || sha(raw) !== state.bundleDigest) throw new Error('Run skill snapshot changed after approval');
    return factoryProfileSchema.parse(JSON.parse(raw));
}
export function agentResources(factory: FactoryProfile, selected: Role) {
    const agent = factory.agents.find(agent => agent.role === selected);
    if (!agent) throw new Error('Missing agent policy');
    return agent.skills.flatMap(skill => [skill, ...skill.references]);
}
export async function workerPolicy(store: Store, state: State, selected: Role, policyDir: string) {
    const factory = await runFactory(store, state);
    if (!factory) return (await bundle()).content[selected];
    const resources = new Map(agentResources(factory, selected).map(item => [item.path, item.content]));
    const root = path.join(policyDir, 'skills', selected);
    for (const [file, content] of resources) await durable(path.join(root, file), content);
    const assigned = factory.agents.find(agent => agent.role === selected)!.skills;
    let context = '';
    if (factory.inheritedHandoff) {
        const file = path.join(policyDir, 'context/inherited-handoff.md');
        await durable(file, factory.inheritedHandoff.markdown);
        context = `\nRead historical context at ${file}. The current approved task governs; source approvals and acceptance do not transfer.`;
    }
    return `Read your assigned SKILL.md files before working. Their referenced resources are available at their original relative paths.\n${assigned.map(skill => path.join(root, skill.path)).join('\n')}${context}`;
}
