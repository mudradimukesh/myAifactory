# Worker contracts

A coordinator constructs a native worker invocation and interprets its structured output without treating malformed or incomplete output as success.

## Sub-features

- Explicit model, policy, and role-specific command settings.
- Final worker text and reported token usage.
- Rejection of malformed output, missing completion, errors, and rate limits.
- Verified instruction loading and rejection of missing roles, unsupported tool names, or modified skills.

## How to get to it (user POV)

The callable entries are `workerCommand`, `parseWorkerOutput`, and `bundle`. No remote worker launches in this verification recipe.

## Driving it with Node tests

Preconditions: complete the parent skill's doctor checks.

Run `"$FACTORY_NODE" --test tests/workers.test.ts tests/bundle.test.ts`, capturing output and exit status under `FACTORY_EVIDENCE`. Require a successful exit. Assertions compare the returned invocation, parsed results, and loaded policy text against literal expected values. A disposable bundle exercises missing roles, unsupported tool names, and changed instruction files.

## Gotchas

Completion and nonblank final text are mandatory. Missing or invalid usage and a missing model can remain unknown without making completed output fail parsing; the coordinator must enforce usage policy separately. The tests cover representative developer/reviewer settings, not every role and provider combination.

Command construction tests do not establish installed CLI compatibility or account access. Codex delegates filesystem restrictions to LocalRuntime because macOS rejects nested Seatbelt sandboxes. Its generated command must only run through LocalRuntime. Claude grants Edit/Write to discovery and architecture roles as well, without Bash; the outer runtime still controls filesystem writes. Discovery and architecture scratch writes, attempt-local credential refresh, and actual provider usage require separate live verification. `LocalRuntime` copies the validated dedicated credential file into a fresh provider home; ordinary user homes and settings are not exposed. No API billing fallback is authorized.
