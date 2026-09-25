# Worker contracts

Worker supervision records provider start and last activity independently of token usage. Codex start is `thread.started`; Claude start is `system/init`. Missing start and 600-second idle expiry use `stall_start` and `stall_idle`.

A coordinator constructs a native worker invocation and interprets its structured output without treating malformed or incomplete output as success.

## Sub-features

- Explicit model, policy, and role-specific command settings.
- Final worker text and reported token usage.
- Rejection of malformed output, missing completion, errors, and rate limits.
- Verified instruction loading and rejection of missing roles, unsupported tool names, or modified skills.
- Shared worker skills. Every role in `roles.json` loads `unslop`, `bro`, `principle-guard-the-context-window` and `principle-never-block-on-the-human`.
- Headroom routing. Codex workers get `OPENAI_BASE_URL` and `-c openai_base_url=...`. Claude workers get `ANTHROPIC_BASE_URL` set to the Headroom base without its trailing `/v1`, and an empty `env` when no route is configured.
- Dashboard Claude login (`src/claude-login.ts`, ADR 0006). It runs `claude auth login --claudeai` in the worker sandbox and saves only `.credentials.json` into `<privateDir>/auth/claude`.

## How to get to it (user POV)

The callable entries are `workerCommand`, `parseWorkerOutput`, `bundle` and `ClaudeLogin`. No remote worker launches in this verification recipe. The dashboard's Connections card drives `ClaudeLogin` through `/api/auth/claude`.

## Driving it with Node tests

Preconditions: complete the parent skill's doctor checks.

Run `"$FACTORY_NODE" --test tests/workers.test.ts tests/bundle.test.ts tests/headroom.test.ts tests/claude-login.test.ts`, capturing output and exit status under `FACTORY_EVIDENCE`. Require a successful exit. Assertions compare the returned invocation, parsed results, and loaded policy text against literal expected values. A disposable bundle exercises missing roles, unsupported tool names, and changed instruction files. The real bundle test requires the four shared skills in all seven roles.

The bundle doctor prints the digest and the role count, which must be 7:

```sh
"$FACTORY_NODE" --input-type=module -e 'import {bundle} from "./src/bundle.ts"; const b=await bundle(); console.log(b.digest, Object.keys(b.content).length)'
```

The native Headroom case sets `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` in the test process and requires a sandboxed Claude job to see only the approved Headroom project route on `ANTHROPIC_BASE_URL`, with no leaked `OPENAI_BASE_URL` or `ANTHROPIC_API_KEY`. The login tests run `tests/fixtures/fake-claude.mjs` in the real sandbox. They require that no view contains the fixture secret or email, that the auth home holds only the 0600 file, and that cancel leaves no process group.

## Gotchas

Completion and nonblank final text are mandatory. Missing or invalid usage and a missing model can remain unknown without making completed output fail parsing; the coordinator must enforce usage policy separately. The tests cover representative developer/reviewer settings, not every role and provider combination.

Command construction tests do not establish installed CLI compatibility or account access. Codex delegates filesystem restrictions to LocalRuntime because macOS rejects nested Seatbelt sandboxes. Its generated command must only run through LocalRuntime. Claude grants Edit/Write to discovery and architecture roles as well, without Bash; the outer runtime still controls filesystem writes. Discovery and architecture scratch writes, attempt-local credential refresh, and actual provider usage require separate live verification. `LocalRuntime` copies the validated dedicated credential file into a fresh provider home; ordinary user homes and settings are not exposed. No API billing fallback is authorized.

Changing any worker skill or `roles.json` changes the bundle digest. `stepUnlocked` then refuses every run approved before the change with "Worker skill bundle changed after run approval", so continuing needs a new run id. Each role prompt grows by about 8.6 KB, roughly 2.2k tokens, from the shared skills. `bro` and `principle-guard-the-context-window` conflict with JSON-only planner and reviewer output and with ADR 0004's no-delegation rule. The lock file records both conflicts. A live smoke test must confirm that planner and reviewer output still parses.

Never run a real `claude auth login` in tests. The fake CLI stands in for it. A real login needs the operator at the browser, and whether the CLI writes its plaintext file when the Keychain is denied is still unobserved.
