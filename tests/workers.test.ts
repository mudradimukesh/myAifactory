import assert from 'node:assert/strict';
import test from 'node:test';
import { parseWorkerOutput, workerCommand } from '../src/workers.ts';

test('Codex workers use explicit policy and delegate filesystem isolation to LocalRuntime', () => {
  const developer = workerCommand(
    { provider: 'codex', model: 'gpt-5.4', effort: 'high' },
    'developer', '/tmp/project', 'Implement the change', 'Follow this policy',
  );
  assert.equal(developer.executable, 'codex');
  assert.deepEqual(developer.args.slice(0, 7), [
    'exec', '--json', '--ephemeral', '--ignore-user-config', '--ignore-rules',
    '--disable', 'multi_agent',
  ]);
  assert.deepEqual(developer.args.slice(-3), ['-C', '/tmp/project', '-']);
  assert.ok(developer.args.includes('model_reasoning_effort="high"'));
  assert.ok(developer.args.includes('approval_policy="never"'));
  assert.ok(developer.args.includes('project_doc_max_bytes=0'));
  assert.equal(developer.args[developer.args.indexOf('--sandbox') + 1], 'danger-full-access');
  assert.match(developer.stdin, /Worker policy:\nFollow this policy\n\nTask:\nImplement the change/);
  assert.deepEqual(developer.env, {});

  const reviewer = workerCommand(
    { provider: 'codex', model: 'gpt-5.4-mini', effort: 'medium' },
    'reviewer', '/tmp/project', 'Review', 'Review policy',
  );
  assert.equal(reviewer.args[reviewer.args.indexOf('--sandbox') + 1], 'danger-full-access');
});

test('Claude workers have explicit model and restricted role tools', () => {
  const reviewer = workerCommand(
    { provider: 'claude', model: 'claude-sonnet-4-6', effort: 'low' },
    'reviewer', '/tmp/project', 'Review', 'Review policy',
  );
  assert.equal(reviewer.executable, 'claude');
  assert.ok(reviewer.args.includes('--safe-mode'));
  assert.ok(reviewer.args.includes('--restricted'));
  assert.ok(reviewer.args.includes('--strict-mcp-config'));
  assert.ok(reviewer.args.includes('--no-session-persistence'));
  assert.equal(reviewer.args[reviewer.args.indexOf('--tools') + 1], 'Read,Glob,Grep');
  assert.equal(reviewer.args[reviewer.args.indexOf('--allowedTools') + 1], 'Read,Glob,Grep');
  assert.equal(reviewer.args[reviewer.args.indexOf('--append-system-prompt') + 1], 'Review policy');
  assert.equal(reviewer.args[reviewer.args.indexOf('--model') + 1], 'claude-sonnet-4-6');
  assert.equal(reviewer.args[reviewer.args.indexOf('--effort') + 1], 'low');
  assert.equal(reviewer.stdin, 'Review');

  const developer = workerCommand(
    { provider: 'claude', model: 'claude-sonnet-4-6', effort: 'xhigh' },
    'developer', '/tmp/project', 'Build', 'Policy',
  );
  assert.equal(developer.args[developer.args.indexOf('--tools') + 1], 'Read,Glob,Grep,Edit,Write');
  assert.ok(!developer.args.join(' ').includes('Bash'));
  assert.ok(!developer.args.join(' ').includes('Agent'));
  assert.ok(!developer.args.join(' ').includes('Task'));
});

test('worker command refuses implicit model or policy', () => {
  assert.throws(() => workerCommand(
    { provider: 'codex', model: '', effort: 'high' },
    'developer', '/tmp', 'Task', 'Policy',
  ), /model must be explicit/);
  assert.throws(() => workerCommand(
    { provider: 'claude', model: 'claude-sonnet-4-6', effort: 'high' },
    'reviewer', '/tmp', 'Task', ' ',
  ), /policy must be explicit/);
});

test('Codex JSONL extracts the final message and terminal usage', () => {
  const stdout = [
    { type: 'thread.started', thread_id: 'test' },
    { type: 'turn.started' },
    { type: 'item.completed', item: { type: 'agent_message', text: 'Working' } },
    { type: 'item.completed', item: { type: 'agent_message', text: 'Done' } },
    { type: 'turn.completed', usage: { input_tokens: 100, cached_input_tokens: 20, output_tokens: 30 } },
  ].map((event) => JSON.stringify(event)).join('\n');
  assert.deepEqual(parseWorkerOutput('codex', stdout), {
    text: 'Done', inputTokens: 100, outputTokens: 30,
    cachedInputTokens: 20, model: null, failed: false, reason: null,
  });
});

test('Claude stream JSON extracts result, usage, and model', () => {
  const stdout = [
    { type: 'system', subtype: 'init', model: 'claude-sonnet-4-6' },
    { type: 'assistant', message: { model: 'claude-sonnet-4-6', content: [{ type: 'text', text: 'Draft' }] } },
    {
      type: 'result', subtype: 'success', is_error: false, result: 'Final answer',
      usage: { input_tokens: 90, output_tokens: 15, cache_read_input_tokens: 10 },
      modelUsage: { 'claude-sonnet-4-6': { inputTokens: 90, outputTokens: 15, cacheReadInputTokens: 10 } },
    },
  ].map((event) => JSON.stringify(event)).join('\n');
  assert.deepEqual(parseWorkerOutput('claude', stdout), {
    text: 'Final answer', inputTokens: 90, outputTokens: 15,
    cachedInputTokens: 10, model: 'claude-sonnet-4-6', failed: false, reason: null,
  });
});

test('Claude modelUsage supplies counts when result usage is absent', () => {
  const stdout = JSON.stringify({
    type: 'result', subtype: 'success', is_error: false, result: 'Done',
    modelUsage: { 'claude-sonnet-4-6': { inputTokens: 8, outputTokens: 3, cacheReadInputTokens: 2 } },
  });
  assert.deepEqual(parseWorkerOutput('claude', stdout), {
    text: 'Done', inputTokens: 8, outputTokens: 3,
    cachedInputTokens: 2, model: 'claude-sonnet-4-6', failed: false, reason: null,
  });
});

test('malformed output, missing completion, errors, and rate limits fail', () => {
  assert.match(parseWorkerOutput('codex', 'not json').reason ?? '', /Malformed/);
  assert.equal(parseWorkerOutput('codex', 'not json').failed, true);
  assert.match(parseWorkerOutput('codex', JSON.stringify({ type: 'turn.started' })).reason ?? '', /no completion/);
  const failedTurn = [
    { type: 'turn.failed', error: { message: 'rate limit exceeded' } },
  ].map((event) => JSON.stringify(event)).join('\n');
  assert.match(parseWorkerOutput('codex', failedTurn).reason ?? '', /rate limit/);
  const claudeError = [
    { type: 'rate_limit_event', message: 'too many requests' },
    { type: 'result', subtype: 'error_during_execution', is_error: true, result: 'Failed' },
  ].map((event) => JSON.stringify(event)).join('\n');
  assert.equal(parseWorkerOutput('claude', claudeError).failed, true);
  assert.match(parseWorkerOutput('claude', claudeError).reason ?? '', /too many requests/);
  assert.equal(parseWorkerOutput('claude', JSON.stringify({
    type: 'result', subtype: 'success', is_error: false, result: '',
  })).failed, true);
});
