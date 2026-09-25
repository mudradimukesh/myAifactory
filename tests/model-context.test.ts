import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { contextMax, handoffTrigger } from '../src/model-context.ts';
import { limitsSchema } from '../src/contracts.ts';

const codexLine = JSON.parse(
    readFileSync(new URL('./fixtures/codex-token-count.jsonl', import.meta.url), 'utf8').trim(),
);
const modelsCache = JSON.parse(
    readFileSync(new URL('./fixtures/codex-models-cache.json', import.meta.url), 'utf8'),
);
const claudeResult = JSON.parse(
    readFileSync(new URL('./fixtures/claude-result.json', import.meta.url), 'utf8'),
);

test('Codex hand-off trigger is 60% of the token_count model_context_window', () => {
    const max = contextMax('codex', 'gpt-6-sol', codexLine.payload.info.model_context_window);
    assert.equal(max, 258400);
    assert.equal(handoffTrigger(0.6, max), 155040);
});

test('the models_cache.json entry already matches the rollout model_context_window', () => {
    assert.equal(modelsCache.slug, 'gpt-6-astra');
    assert.equal(Math.floor(modelsCache.context_window * modelsCache.effective_context_window_percent / 100),
        codexLine.payload.info.model_context_window);
});

test('Claude Sonnet 5 hand-off trigger is 60% of its verified context window', () => {
    const window = claudeResult.modelUsage['claude-sonnet-5'].contextWindow;
    const max = contextMax('claude', 'claude-sonnet-5', null);
    assert.equal(max, window);
    assert.equal(handoffTrigger(0.6, max), 120000);
});

test('Claude Opus 5.5 hand-off trigger is capped below 60% of its context window', () => {
    const max = contextMax('claude', 'claude-opus-5-5', null);
    assert.equal(max, 1000000);
    assert.equal(handoffTrigger(0.6, max, 'claude-opus-5-5'), 350000);
});

test('an unverified Claude model throws instead of guessing a context window', () => {
    for (const model of ['claude-haiku-9', 'constructor', 'toString'])
        assert.throws(() => contextMax('claude', model, null), /No verified context window/);
});

test('Codex without a reported model_context_window throws', () => {
    assert.throws(() => contextMax('codex', 'gpt-6-sol', null), /gpt-6-sol/);
});

test('handoffContextRatio must be greater than 0 and at most 0.95', () => {
    const base = {
        maxAttempts: 10, maxReworks: 2, attemptTimeoutMs: 60000, maxWallMs: 3600000,
        maxReportedTokens: 1000000, verificationReserveAttempts: 1, maxLogBytes: 65536,
    };
    assert.throws(() => limitsSchema.parse({ ...base, handoffContextRatio: 0 }));
    assert.throws(() => limitsSchema.parse({ ...base, handoffContextRatio: 0.96 }));
    assert.doesNotThrow(() => limitsSchema.parse({ ...base, handoffContextRatio: 0.6 }));
    assert.doesNotThrow(() => limitsSchema.parse(base));
});
