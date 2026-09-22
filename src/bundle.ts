import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { sha, json, readJson, within } from './store.ts';
import { z } from 'zod';
import { role, digest } from './contracts.ts';
const resourceSchema = z.object({ path: z.string().min(1), sha256: digest }).strict();
const lockSchema = z.object({
    schemaVersion: z.literal(1),
    skills: z.array(resourceSchema.extend({ name: z.string().min(1), references: z.array(resourceSchema), provenance: z.unknown() })).min(1),
}).strict();
const rolesSchema = z.object({
    schemaVersion: z.literal(1),
    roles: z.record(role, z.object({ skills: z.array(z.string().min(1)).min(1), tools: z.array(z.enum(['read', 'grep', 'find', 'ls', 'write', 'edit', 'bash'])) }).strict()),
}).strict().superRefine((value, context) => {
    for (const name of role.options) if (!value.roles[name]) context.addIssue({ code: 'custom', message: `Missing role ${name}` });
});
export const factoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export async function bundle(root = factoryRoot) {
    const lock = lockSchema.parse(await readJson(path.join(root, 'skills.lock.json')));
    const roles = rolesSchema.parse(await readJson(path.join(root, 'roles.json')));
    const names = new Set<string>();
    const files = new Map<string, string>();
    for (const s of lock.skills) {
        if (names.has(s.name))
            throw Error(`Duplicate skill ${s.name}`);
        names.add(s.name);
        for (const file of [s, ...s.references]) {
            const text = await readFile(await within(root, file.path), 'utf8');
            if (sha(text) !== file.sha256)
                throw Error(`Skill hash mismatch: ${file.path}`);
            files.set(file.path, text);
        }
        if (!files.get(s.path)?.includes(`name: ${s.name}`))
            throw Error('Declared skill name mismatch');
    }
    for (const r of Object.values(roles.roles))
        for (const name of r.skills)
            if (!names.has(name))
                throw Error(`Unresolved skill ${name}`);
    const content: Record<string, string> = {};
    for (const [name, r] of Object.entries(roles.roles)) {
        const selected = new Set<string>();
        for (const n of r.skills) {
            const s = lock.skills.find(x => x.name === n)!;
            selected.add(s.path);
            for (const ref of s.references)
                selected.add(ref.path);
        }
        content[name] = [...selected].map(p => `Resource ${p}\n${files.get(p)}`).join('\n\n');
    }
    return { digest: sha(json({ lock, roles })), content, lock, roles };
}
