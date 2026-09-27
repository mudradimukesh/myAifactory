import assert from 'node:assert/strict';
import test from 'node:test';
import { observedSkillReads } from '../src/skill-activity.ts';

const skills = [{ name: 'rendering', path: '/policy/skills/developer/rendering/SKILL.md' }, { name: 'testing', path: '/policy/skills/developer/testing/SKILL.md' }];
const rendering = '/policy/skills/developer/rendering/SKILL.md';
const testing = '/policy/skills/developer/testing/SKILL.md';
const log = (...events: unknown[]) => events.map(event => JSON.stringify(event)).join('\n');
const completed = (command: string | string[], exit_code = 0) => ({ type: 'item.completed', item: { type: 'command_execution', command, exit_code } });

test('records completed Codex reads by exact assigned skill path', () => {
  assert.deepEqual(observedSkillReads(log(completed(`cat ${rendering}`), completed(`sed -n '1,200p' ${testing}`), completed(`cat ${rendering}`)), skills), ['rendering', 'testing']);
  assert.deepEqual(observedSkillReads(log(completed(['/bin/zsh', '-lc', `head -n 20 "${rendering}"`])), skills), ['rendering']);
  assert.deepEqual(observedSkillReads(log(completed(`cat ${rendering} && cat ${testing}`)), skills), ['rendering', 'testing']);
  assert.deepEqual(observedSkillReads(log(completed(`echo 'nothing && cat ${rendering} && nothing'`)), skills), []);
});

test('does not infer skill reads from mentions, started commands, failures or another role', () => {
  assert.deepEqual(observedSkillReads(log(
    { type: 'item.started', item: { type: 'command_execution', command: `cat ${rendering}` } },
    completed(`cat ${rendering}`, 1),
    completed(`echo ${rendering}`),
    completed(`rg --files ${rendering}`),
    completed(`cat /policy/skills/tester/rendering/SKILL.md`),
    { type: 'item.completed', item: { type: 'agent_message', text: `I read ${rendering}` } },
  ), skills), []);
});

test('requires a matching successful Claude read result', () => {
  const call = { type: 'assistant', message: { content: [{ type: 'tool_use', id: 'read-1', name: 'Read', input: { file_path: rendering } }] } };
  const result = (is_error: boolean) => ({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'read-1', is_error }] } });
  assert.deepEqual(observedSkillReads(log(call), skills), []);
  assert.deepEqual(observedSkillReads(log(call, result(true)), skills), []);
  assert.deepEqual(observedSkillReads(log(result(false)), skills), []);
  assert.deepEqual(observedSkillReads(log(call, result(false)), skills), ['rendering']);
});

test('records a Codex read reported as a shell command string', () => {
  assert.deepEqual(observedSkillReads(log(completed(`/bin/zsh -lc "cat ${rendering}"`)), skills), ['rendering']);
});

test('records successful Claude Bash reads without exposing result contents', () => {
  assert.deepEqual(observedSkillReads(log(
    { type: 'assistant', message: { content: [{ type: 'tool_use', id: 'bash-1', name: 'Bash', input: { command: `cat ${testing}` } }] } },
    { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'bash-1', content: 'private skill body' }] } },
  ), skills), ['testing']);
});

test('ignores ambiguous shell expressions, zero-length reads and malformed events', () => {
  assert.deepEqual(observedSkillReads('partial line\n' + log(
    completed(`echo cat ${rendering}`), completed(`false; cat ${rendering}`),
    completed(`cat ${rendering} || true`), completed(`head -n 0 ${rendering}`),
    completed(`sed -i 's/a/b/' ${rendering}`), completed(`cat "${rendering}`),
    { type: 'assistant', message: { content: 'invalid' } }, null,
  ), skills), []);
});
