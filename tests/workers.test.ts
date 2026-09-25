import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, appendFileSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { meteredTokens, normalize, parseWorkerOutput, workerCommand, UsageMeter } from '../src/workers.ts';

test('resume argv for codex and claude', () => {
  const codexSession = parseWorkerOutput('codex', readFileSync(new URL('./fixtures/codex-thread-started.jsonl', import.meta.url), 'utf8')).session;
  const claudeSession = parseWorkerOutput('claude', readFileSync(new URL('./fixtures/claude-init.json', import.meta.url), 'utf8')).session;
  assert.equal(codexSession, '00000000-0000-4000-8000-000000000002');
  assert.equal(claudeSession, '00000000-0000-4000-8000-000000000001');
  const schema = { json: '{"type":"object"}', file: '/tmp/policy/handoff-schema.json' };
  const route = { baseUrl: 'http://127.0.0.1:8791/v1' };
  const codex = workerCommand({ provider: 'codex', model: 'gpt-6-sol', effort: 'high' },
    'developer', '/tmp/source', 'Summarize', 'Policy', route, schema, { session: codexSession });
  assert.deepEqual(codex.args, ['exec', '--json', '--ignore-user-config', '--ignore-rules',
    '--disable', 'multi_agent', '--disable', 'apps', '--disable', 'skill_search', '--enable', 'skip_host_skill_discovery',
    '-c', 'openai_base_url="http://127.0.0.1:8791/v1"', '--model', 'gpt-6-sol',
    '-c', 'model_reasoning_effort="high"', '-c', 'approval_policy="never"', '-c', 'project_doc_max_bytes=0',
    '-c', 'model_auto_compact_token_limit=1000000000', '--sandbox', 'danger-full-access', '-C', '/tmp/source',
    '--output-schema', schema.file, 'resume', codexSession, '-']);
  assert.equal(codex.env.OPENAI_BASE_URL, route.baseUrl);
  const claude = workerCommand({ provider: 'claude', model: 'claude-sonnet-5', effort: 'high' },
    'reviewer', '/tmp/source', 'Summarize', 'Policy', route, schema, { session: claudeSession });
  assert.deepEqual(claude.args, ['--print', '--output-format', 'stream-json', '--verbose',
    '--safe-mode', '--restricted', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--disable-slash-commands',
    '--tools', 'Read,Glob,Grep', '--allowedTools', 'Read,Glob,Grep', '--permission-mode', 'dontAsk',
    '--permission-prompts', 'none', '--model', 'claude-sonnet-5', '--effort', 'high', '--append-system-prompt', 'Policy',
    '--json-schema', schema.json, '--resume', claudeSession]);
  assert.deepEqual(claude.env, { CLAUDE_STREAM_IDLE_TIMEOUT_MS: '120000', DISABLE_AUTO_COMPACT: '1',
    DISABLE_COMPACT: '1', ANTHROPIC_BASE_URL: 'http://127.0.0.1:8791' });
});

test('Claude result retains context windows by model', () => {
  const result = readFileSync(new URL('./fixtures/claude-result.json', import.meta.url), 'utf8');
  assert.deepEqual(parseWorkerOutput('claude', result).contextWindows, { 'claude-sonnet-5': 200000 });
});

test('a resumed Codex meter excludes copied history and counts only new usage', () => {
  const root = mkdtempSync(join(tmpdir(), 'factory-resume-meter-'));
  try {
    const file = join(root, 'rollout.jsonl');
    const fixture = readFileSync(new URL('./fixtures/codex-token-count.jsonl', import.meta.url), 'utf8');
    writeFileSync(file, fixture);
    const original = new UsageMeter('codex', file);
    original.read();
    const cursor = original.cursor();
    assert.ok(cursor);
    const resumed = new UsageMeter('codex', file, cursor);
    assert.equal(resumed.read(), null);
    const next = JSON.parse(fixture);
    next.payload.info.total_token_usage.input_tokens += 100;
    next.payload.info.total_token_usage.output_tokens += 10;
    next.payload.info.total_token_usage.total_tokens += 110;
    appendFileSync(file, JSON.stringify(next) + '\n');
    const reading = resumed.read();
    assert.deepEqual(reading?.usage, { input: 100, cached: 0, output: 10 });
    assert.equal(reading?.contextTokens, 122449);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('Codex workers use explicit policy and delegate filesystem isolation to LocalRuntime', () => {
  const developer = workerCommand(
    { provider: 'codex', model: 'gpt-5.4', effort: 'high' },
    'developer', '/tmp/project', 'Implement the change', 'Follow this policy',
  );
  assert.equal(developer.executable, 'codex');
  assert.deepEqual(developer.args.slice(0, 6), [
    'exec', '--json', '--ignore-user-config', '--ignore-rules',
    '--disable', 'multi_agent',
  ]);
  assert.deepEqual(developer.args.slice(-3), ['-C', '/tmp/project', '-']);
  assert.ok(developer.args.includes('model_reasoning_effort="high"'));
  assert.ok(developer.args.includes('approval_policy="never"'));
  assert.ok(developer.args.includes('project_doc_max_bytes=0'));
  assert.ok(developer.args.includes('model_auto_compact_token_limit=1000000000'));
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
  assert.ok(!reviewer.args.includes('--no-session-persistence'));
  assert.equal(reviewer.env.DISABLE_AUTO_COMPACT, '1');
  assert.equal(reviewer.env.DISABLE_COMPACT, '1');
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
  assert.deepEqual(claude.env, { CLAUDE_STREAM_IDLE_TIMEOUT_MS: '120000', DISABLE_AUTO_COMPACT: '1', DISABLE_COMPACT: '1', ANTHROPIC_BASE_URL: 'http://127.0.0.1:8791' });
  const claudeProject = workerCommand({ provider: 'claude', model: 'sonnet', effort: 'medium' },
    'reviewer', '/tmp/project', 'Review the change', 'Follow this policy',
    { baseUrl: 'http://127.0.0.1:8791/p/my-aifactory/v1' });
  assert.deepEqual(claudeProject.env, { CLAUDE_STREAM_IDLE_TIMEOUT_MS: '120000', DISABLE_AUTO_COMPACT: '1', DISABLE_COMPACT: '1', ANTHROPIC_BASE_URL: 'http://127.0.0.1:8791/p/my-aifactory' });
  const claudeUnrouted = workerCommand({ provider: 'claude', model: 'sonnet', effort: 'medium' },
    'reviewer', '/tmp/project', 'Review the change', 'Follow this policy');
  assert.deepEqual(claudeUnrouted.env, { CLAUDE_STREAM_IDLE_TIMEOUT_MS: '120000', DISABLE_AUTO_COMPACT: '1', DISABLE_COMPACT: '1' });
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
    raw: { input_tokens: 100, cached_input_tokens: 20, output_tokens: 30 },
    session: 'test',
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
    raw: { input_tokens: 90, cache_creation_input_tokens: 0, cache_read_input_tokens: 10, output_tokens: 15 },
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
    raw: { input_tokens: 8, cache_creation_input_tokens: 0, cache_read_input_tokens: 2, output_tokens: 3 },
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


test('raw usage is copied verbatim and normalized with cache reads metered at one tenth', () => {
  const codexUsage = { input_tokens: 5471253, cached_input_tokens: 5345280, cache_write_input_tokens: 0, output_tokens: 38810, reasoning_output_tokens: 19574 };
  const codexStdout = [
    { type: 'turn.completed', usage: codexUsage },
  ].map((event) => JSON.stringify(event)).join('\n');
  const codex = parseWorkerOutput('codex', JSON.stringify({ type: 'item.completed', item: { type: 'agent_message', text: 'Done' } }) + '\n' + codexStdout);
  assert.deepEqual(codex.raw, codexUsage);
  const codexNormalized = normalize('codex', codex.raw);
  assert.deepEqual(codexNormalized, { input: 5471253, cached: 5345280, output: 38810 });
  assert.equal(meteredTokens(codexNormalized), 699311);

  const claudeUsage = { input_tokens: 10, cache_creation_input_tokens: 34058, cache_read_input_tokens: 95575, output_tokens: 100 };
  const claude = parseWorkerOutput('claude', JSON.stringify({ type: 'result', subtype: 'success', result: 'Done', usage: claudeUsage }));
  assert.deepEqual(claude.raw, claudeUsage);
  const claudeNormalized = normalize('claude', claude.raw);
  assert.deepEqual(claudeNormalized, { input: 10 + 34058 + 95575, cached: 95575, output: 100 });
});

test('UsageMeter tails a Codex rollout, ignoring an incomplete trailing line until it completes', () => {
  const dir = mkdtempSync(join(tmpdir(), 'usage-meter-codex-'));
  const path = join(dir, 'rollout-1.jsonl');
  const tokenCount = (total: { input: number; cached: number; output: number }, last: { input: number; output: number }) =>
    JSON.stringify({ type: 'event_msg', payload: { type: 'token_count', info: {
      total_token_usage: { input_tokens: total.input, cached_input_tokens: total.cached, output_tokens: total.output },
      last_token_usage: { input_tokens: last.input, output_tokens: last.output },
      model_context_window: 258400,
    } } });
  writeFileSync(path,
    tokenCount({ input: 1000, cached: 900, output: 50 }, { input: 100, output: 20 }) + '\n' +
    tokenCount({ input: 2000, cached: 1800, output: 80 }, { input: 150, output: 25 }) + '\n');
  const meter = new UsageMeter('codex', path);
  const afterTwo = meter.read();
  assert.deepEqual(afterTwo?.raw, { input_tokens: 2000, cached_input_tokens: 1800, output_tokens: 80 });

  const third = tokenCount({ input: 3000, cached: 2700, output: 110 }, { input: 200, output: 30 });
  const fourthHalf = '{"type":"event_msg","payload":{"type":"token';
  appendFileSync(path, third + '\n' + fourthHalf);
  const afterHalf = meter.read();
  assert.deepEqual(afterHalf?.raw, { input_tokens: 3000, cached_input_tokens: 2700, output_tokens: 110 });
  assert.equal(afterHalf?.contextTokens, 230);
  assert.equal(afterHalf?.contextMax, 258400);
  assert.equal(afterHalf?.compacted, false);

  const fourth = tokenCount({ input: 4000, cached: 3600, output: 140 }, { input: 250, output: 35 });
  appendFileSync(path, '_count","info":{}}}\n'); // complete the half line as an unrelated, ignorable event
  appendFileSync(path, fourth + '\n');
  const afterFourth = meter.read();
  assert.deepEqual(afterFourth?.raw, { input_tokens: 4000, cached_input_tokens: 3600, output_tokens: 140 });
});

test('UsageMeter marks a Codex compaction without losing the last reading', () => {
  const dir = mkdtempSync(join(tmpdir(), 'usage-meter-codex-compact-'));
  const path = join(dir, 'rollout-1.jsonl');
  writeFileSync(path, JSON.stringify({ type: 'event_msg', payload: { type: 'token_count', info: {
    total_token_usage: { input_tokens: 500, cached_input_tokens: 400, output_tokens: 20 },
    last_token_usage: { input_tokens: 500, output_tokens: 20 }, model_context_window: 258400 } } }) + '\n');
  const meter = new UsageMeter('codex', path);
  meter.read();
  appendFileSync(path, JSON.stringify({ type: 'compacted' }) + '\n');
  const reading = meter.read();
  assert.equal(reading?.compacted, true);
  assert.deepEqual(reading?.raw, { input_tokens: 500, cached_input_tokens: 400, output_tokens: 20 });
});

test('UsageMeter records provider start and byte activity without usage', t => {
  const dir = mkdtempSync(join(tmpdir(), 'usage-activity-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const file = join(dir, 'events.jsonl');
  writeFileSync(file, JSON.stringify({ type: 'thread.started', thread_id: 'thread-test' }) + '\n');
  const meter = new UsageMeter('codex', file);
  assert.equal(meter.read(), null);
  assert.deepEqual(meter.activity(), { bytes: Buffer.byteLength(readFileSync(file)), started: true, session: 'thread-test' });
});

test('UsageMeter sums a Claude capture log without double counting a repeated message id', () => {
  const dir = mkdtempSync(join(tmpdir(), 'usage-meter-claude-'));
  const path = join(dir, 'stdout.log');
  const assistant = (id: string, usage: Record<string, number>) =>
    JSON.stringify({ type: 'assistant', message: { id, usage } });
  writeFileSync(path,
    assistant('msg1', { input_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, output_tokens: 5 }) + '\n' +
    assistant('msg1', { input_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, output_tokens: 40 }) + '\n' +
    assistant('msg2', { input_tokens: 20, cache_creation_input_tokens: 0, cache_read_input_tokens: 5, output_tokens: 15 }) + '\n');
  const meter = new UsageMeter('claude', path);
  const reading = meter.read();
  assert.deepEqual(reading?.raw, { input_tokens: 30, cache_creation_input_tokens: 0, cache_read_input_tokens: 5, output_tokens: 55 });
  assert.equal(reading?.contextTokens, 40);
  assert.equal(reading?.contextMax, null);
});

test('UsageMeter marks a Claude compact_boundary event', () => {
  const dir = mkdtempSync(join(tmpdir(), 'usage-meter-claude-compact-'));
  const path = join(dir, 'stdout.log');
  writeFileSync(path,
    JSON.stringify({ type: 'assistant', message: { id: 'msg1', usage: { input_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, output_tokens: 5 } } }) + '\n');
  const meter = new UsageMeter('claude', path);
  meter.read();
  appendFileSync(path, JSON.stringify({ type: 'system', subtype: 'compact_boundary' }) + '\n');
  const reading = meter.read();
  assert.equal(reading?.compacted, true);
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


test('Claude meter keeps the latest context separate from aggregate usage', t => {
  const dir = mkdtempSync(join(tmpdir(), 'usage-context-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const file = join(dir, 'stdout.log');
  const usage = { input_tokens: 10000, cache_creation_input_tokens: 10000, cache_read_input_tokens: 110000, output_tokens: 100 };
  writeFileSync(file, ['first', 'second'].map(id => JSON.stringify({ type: 'assistant', message: { id, usage } })).join('\n') + '\n');
  const reading = new UsageMeter('claude', file).read();
  assert.equal(reading?.contextTokens, 130100);
  assert.equal(reading?.usage.input, 260000);
});

test('UsageMeter preserves a context peak within one batch', t => {
  const dir = mkdtempSync(join(tmpdir(), 'usage-peak-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const file = join(dir, 'rollout.jsonl');
  writeFileSync(file, [160000, 1000].map(input => JSON.stringify({ type: 'event_msg', payload: { type: 'token_count', info: {
    total_token_usage: { input_tokens: 170000, cached_input_tokens: 169000, output_tokens: 10 },
    last_token_usage: { input_tokens: input, output_tokens: 0 }, model_context_window: 258400,
  } } })).join('\n') + '\n');
  const reading = new UsageMeter('codex', file).read();
  assert.equal(reading?.contextTokens, 1000);
  assert.equal(reading?.peakContext, 160000);
});

test('invalid provider counts cannot become a normalized or live usage reading', t => {
  const dir = mkdtempSync(join(tmpdir(), 'usage-invalid-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const file = join(dir, 'rollout.jsonl');
  for (const cached of [undefined, -1, 1.5, 101, Number.MAX_SAFE_INTEGER + 1]) {
    const raw = { input_tokens: 100, output_tokens: 10, ...(cached === undefined ? {} : { cached_input_tokens: cached }) };
    assert.throws(() => normalize('codex', raw));
    const parsed = parseWorkerOutput('codex', JSON.stringify({ type: 'turn.completed', usage: raw }));
    assert.equal(parsed.inputTokens, null);
    writeFileSync(file, JSON.stringify({ type: 'event_msg', payload: { type: 'token_count', info: { total_token_usage: raw } } }) + '\n');
    assert.throws(() => new UsageMeter('codex', file).read());
  }
  assert.throws(() => normalize('claude', { input_tokens: 10, cache_read_input_tokens: 20, output_tokens: 1 }));
});
