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
  assert.deepEqual(developer.env, { CODEX_CA_CERTIFICATE: '/private/etc/ssl/cert.pem', SSL_CERT_FILE: '/private/etc/ssl/cert.pem' });

  const reviewer = workerCommand(
    { provider: 'codex', model: 'gpt-5.4-mini', effort: 'medium' },
    'reviewer', '/tmp/project', 'Review', 'Review policy',
  );
  assert.equal(reviewer.args[reviewer.args.indexOf('--sandbox') + 1], 'danger-full-access');
});

test('JSON-returning workers pass the output schema to the CLI', () => {
  const schema = { json: '{"type":"object"}', file: '/tmp/policy/output-schema.json' };
  const codex = workerCommand({ provider: 'codex', model: 'gpt-5.4', effort: 'high' }, 'architect', '/tmp/project', 'Plan', 'Policy', undefined, schema);
  assert.deepEqual(codex.args.slice(-5), ['-C', '/tmp/project', '--output-schema', schema.file, '-']);
  const claude = workerCommand({ provider: 'claude', model: 'claude-sonnet-4-6', effort: 'low' }, 'reviewer', '/tmp/project', 'Review', 'Policy', undefined, schema);
  assert.equal(claude.args[claude.args.indexOf('--json-schema') + 1], schema.json);
  const plain = workerCommand({ provider: 'claude', model: 'claude-sonnet-4-6', effort: 'low' }, 'reviewer', '/tmp/project', 'Review', 'Policy');
  assert.equal(plain.args.includes('--json-schema'), false);
});

test('Claude structured output replaces a prose result', () => {
  const parsed = parseWorkerOutput('claude', JSON.stringify({ type: 'result', subtype: 'success',
    result: 'Now I have a grounded design.\n```json\n{"a":1}\n```', structured_output: { a: 1 },
    usage: { input_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, output_tokens: 1 } }));
  assert.equal(parsed.failed, false);
  assert.deepEqual(JSON.parse(parsed.text), { a: 1 });
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
  assert.equal(developer.args[developer.args.indexOf('--tools') + 1], 'Read,Glob,Grep,Edit,Write,Bash');
  assert.ok(!developer.args.join(' ').includes('Agent'));
  assert.ok(!developer.args.join(' ').includes('Task'));
});

test('Headroom routes Codex and Claude workers through the same proxy', () => {
  const baseUrl = 'http://127.0.0.1:8791/v1';
  const codex = workerCommand({ provider: 'codex', model: 'gpt-5.4', effort: 'high' },
    'developer', '/tmp/project', 'Implement the change', 'Follow this policy', { baseUrl });
  assert.equal(codex.env.OPENAI_BASE_URL, baseUrl);
  assert.ok(codex.args.includes(`openai_base_url=${JSON.stringify(baseUrl)}`));
  const claude = workerCommand({ provider: 'claude', model: 'sonnet', effort: 'medium' },
    'reviewer', '/tmp/project', 'Review the change', 'Follow this policy', { baseUrl });
  assert.deepEqual(claude.env, { CLAUDE_STREAM_IDLE_TIMEOUT_MS: '120000', ANTHROPIC_BASE_URL: 'http://127.0.0.1:8791' });
  const claudeProject = workerCommand({ provider: 'claude', model: 'sonnet', effort: 'medium' },
    'reviewer', '/tmp/project', 'Review the change', 'Follow this policy',
    { baseUrl: 'http://127.0.0.1:8791/p/my-aifactory/v1' });
  assert.deepEqual(claudeProject.env, { CLAUDE_STREAM_IDLE_TIMEOUT_MS: '120000', ANTHROPIC_BASE_URL: 'http://127.0.0.1:8791/p/my-aifactory' });
  const claudeUnrouted = workerCommand({ provider: 'claude', model: 'sonnet', effort: 'medium' },
    'reviewer', '/tmp/project', 'Review the change', 'Follow this policy');
  assert.deepEqual(claudeUnrouted.env, { CLAUDE_STREAM_IDLE_TIMEOUT_MS: '120000' });
  for (const command of [claude, claudeProject, claudeUnrouted])
    assert.ok(!command.args.some(arg => arg.includes('8791')));
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
      usage: { input_tokens: 90, output_tokens: 15, cache_creation_input_tokens: 0, cache_read_input_tokens: 10 },
      modelUsage: { 'claude-sonnet-4-6': { inputTokens: 90, outputTokens: 15, cacheCreationInputTokens: 0, cacheReadInputTokens: 10 } },
    },
  ].map((event) => JSON.stringify(event)).join('\n');
  assert.deepEqual(parseWorkerOutput('claude', stdout), {
    text: 'Final answer', inputTokens: 100, outputTokens: 15,
    cachedInputTokens: 10, model: 'claude-sonnet-4-6', failed: false, reason: null,
  });
});

test('Claude modelUsage supplies counts when result usage is absent', () => {
  const stdout = JSON.stringify({
    type: 'result', subtype: 'success', is_error: false, result: 'Done',
    modelUsage: { 'claude-sonnet-4-6': { inputTokens: 8, outputTokens: 3, cacheCreationInputTokens: 0, cacheReadInputTokens: 2 } },
  });
  assert.deepEqual(parseWorkerOutput('claude', stdout), {
    text: 'Done', inputTokens: 10, outputTokens: 3,
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
  const allowed = (status: string) => [
    { type: 'rate_limit_event', rate_limit_info: { status } },
    { type: 'result', subtype: 'success', is_error: false, result: 'Plan' },
  ].map((event) => JSON.stringify(event)).join('\n');
  assert.equal(parseWorkerOutput('claude', allowed('allowed')).failed, false);
  assert.equal(parseWorkerOutput('claude', allowed('allowed_warning')).failed, false);
  assert.equal(parseWorkerOutput('claude', allowed('rejected')).failed, true);
});


test('Claude counts uncached, cache creation, and cache read input without changing result precedence', () => {
  const result = { type: 'result', subtype: 'success', result: 'Done',
    usage: { input_tokens: 18, cache_creation_input_tokens: 27728, cache_read_input_tokens: 201700, output_tokens: 5205 },
    modelUsage: { 'claude-sonnet': { inputTokens: 18, cacheCreationInputTokens: 27728, cacheReadInputTokens: 201700, outputTokens: 5205 },
      'claude-haiku': { inputTokens: 1082, cacheCreationInputTokens: 0, cacheReadInputTokens: 0, outputTokens: 19 } } };
  const parsed = parseWorkerOutput('claude', JSON.stringify(result));
  assert.equal(parsed.inputTokens, 229446);
  assert.equal(parsed.outputTokens, 5205);
  assert.equal(parsed.cachedInputTokens, 201700);
  const fallback = parseWorkerOutput('claude', JSON.stringify({ ...result, usage: undefined }));
  assert.equal(fallback.inputTokens, 230528);
  assert.equal(fallback.outputTokens, 5224);
});

test('Claude missing or invalid cache categories leave input usage unknown', () => {
  for (const field of ['cache_creation_input_tokens', 'cache_read_input_tokens']) {
    for (const value of [undefined, null, -1, 1.5, '10', Number.MAX_SAFE_INTEGER]) {
      const parsed = parseWorkerOutput('claude', JSON.stringify({ type: 'result', subtype: 'success', result: 'Done',
        usage: { input_tokens: 18, cache_creation_input_tokens: 27728, cache_read_input_tokens: 201700, output_tokens: 5205,
          [field]: value } }));
      assert.equal(parsed.inputTokens, null);
    }
  }
});


test('Claude Bash access is limited to developer and tester roles', () => {
  for (const role of ['developer', 'tester', 'reviewer', 'business', 'domain', 'architect', 'coordinator'] as const) {
    const command = workerCommand({ provider: 'claude', model: 'claude-sonnet-4-6', effort: 'low' },
      role, '/tmp/project', 'Task', 'Policy');
    for (const flag of ['--tools', '--allowedTools']) {
      assert.equal(command.args[command.args.indexOf(flag) + 1].split(',').includes('Bash'),
        role === 'developer' || role === 'tester', `${role} ${flag}`);
    }
  }
});
