// Verified context windows for Claude models, in tokens. Codex reports its own
// model_context_window on every token_count event, so it needs no table entry here.
export const claudeContextWindows: Record<string, number> = {
    'claude-sonnet-5': 200000,
    'claude-opus-5-5': 1000000,
};

// Opus degrades past about 40% of its window, so its hand-off trigger is capped
// far below the ratio that would otherwise apply to its raw context size.
const handoffCaps: Record<string, number> = {
    'claude-opus-5-5': 350000,
};

export function contextMax(provider: 'codex' | 'claude', model: string, meterWindow: number | null): number {
    if (provider === 'codex') {
        if (meterWindow === null) throw new Error(`No context window reported for Codex model ${model}`);
        return meterWindow;
    }
    const max = Object.hasOwn(claudeContextWindows, model) ? claudeContextWindows[model] : undefined;
    if (max === undefined) throw new Error(`No verified context window for Claude model ${model}`);
    return max;
}

export function handoffTrigger(ratio: number, max: number, model?: string): number {
    const trigger = Math.floor(ratio * max);
    const cap = model && Object.hasOwn(handoffCaps, model) ? handoffCaps[model] : undefined;
    return cap !== undefined ? Math.min(trigger, cap) : trigger;
}
