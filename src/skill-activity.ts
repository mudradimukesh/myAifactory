import path from 'node:path';
import { z } from 'zod';

const eventSchema = z.object({
  type: z.string(),
  item: z.object({ type: z.string(), command: z.union([z.string(), z.array(z.string())]).optional(), exit_code: z.number().nullable().optional() }).passthrough().optional(),
  message: z.object({ content: z.array(z.unknown()) }).passthrough().optional(),
}).passthrough();
const callSchema = z.object({ type: z.literal('tool_use'), id: z.string(), name: z.string(), input: z.object({ file_path: z.string().optional(), command: z.string().optional() }).passthrough() }).passthrough();
const resultSchema = z.object({ type: z.literal('tool_result'), tool_use_id: z.string(), is_error: z.boolean().optional() }).passthrough();

function commandFiles(command: string | string[]): string[] {
  if (Array.isArray(command)) {
    if (command.length === 3 && ['sh', 'bash', 'zsh'].includes(path.basename(command[0] ?? '')) && ['-c', '-lc'].includes(command[1] ?? ''))
      return commandFiles(command[2] ?? '');
    return readOperands(command);
  }
  if (/[\\$`;|<>\n\r]/.test(command)) return [];
  const groups: string[][] = [[]];
  const token = /\s*(?:"([^"]*)"|'([^']*)'|(&&)|([^\s"'&]+))/gy;
  let offset = 0;
  while (offset < command.trimEnd().length) {
    token.lastIndex = offset;
    const match = token.exec(command);
    if (!match) return [];
    if (match[3]) groups.push([]);
    else groups[groups.length - 1]!.push(match[1] ?? match[2] ?? match[4] ?? '');
    offset = token.lastIndex;
  }
  return groups.some(group => !group.length) ? [] : groups.flatMap(readOperands);
}

function readOperands(tokens: string[]): string[] {
  const [executable, ...args] = tokens;
  if (!executable) return [];
  const command = path.basename(executable);
  if (['sh', 'bash', 'zsh'].includes(command) && args.length === 2 && ['-c', '-lc'].includes(args[0] ?? '')) return commandFiles(args[1] ?? '');
  if (command === 'cat') return args[0] === '--' ? args.slice(1) : args.every(value => !value.startsWith('-')) ? args : [];
  if (command === 'sed' && args[0] === '-n' && /^[1-9]\d*(?:,[1-9]\d*)?p$/.test(args[1] ?? '')) return args.slice(2);
  if (command === 'head' || command === 'tail') {
    if (args[0] === '-n' || args[0] === '-c') return /^[1-9]\d*$/.test(args[1] ?? '') ? args.slice(2) : [];
    return args.every(value => !value.startsWith('-')) ? args : [];
  }
  return [];
}

/** Reports recorded file reads, not instructions injected into an agent prompt. */
export function observedSkillReads(transcript: string, skills: readonly { name: string; path: string }[]): string[] {
  const namesByPath = new Map(skills.map(skill => [path.normalize(skill.path), skill.name]));
  const seen = new Set<string>();
  const pending = new Map<string, string[]>();
  const record = (files: string[]) => {
    for (const file of files) {
      const name = namesByPath.get(path.normalize(file));
      if (name) seen.add(name);
    }
  };
  for (const line of transcript.split('\n')) {
    let value: unknown;
    try { value = JSON.parse(line); } catch { continue; }
    const parsed = eventSchema.safeParse(value);
    if (!parsed.success) continue;
    const event = parsed.data;
    if (event.type === 'item.completed' && event.item?.type === 'command_execution' && event.item.exit_code === 0 && event.item.command)
      record(commandFiles(event.item.command));
    if (event.type === 'assistant') for (const block of event.message?.content ?? []) {
      const call = callSchema.safeParse(block);
      if (!call.success) continue;
      const { id, name, input } = call.data;
      pending.set(id, name === 'Read' && input.file_path ? [input.file_path] : name === 'Bash' && input.command ? commandFiles(input.command) : []);
    }
    if (event.type === 'user') for (const block of event.message?.content ?? []) {
      const result = resultSchema.safeParse(block);
      if (!result.success) continue;
      if (!result.data.is_error) record(pending.get(result.data.tool_use_id) ?? []);
      pending.delete(result.data.tool_use_id);
    }
  }
  return [...seen];
}
