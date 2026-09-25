# myAifactory

A subscription-based development factory in progress. Native Codex and Claude Code workers run through a restricted local macOS runtime and submit candidates for independent review and runner-executed checks.

The repository contains a local operator dashboard, an execution coordinator, and isolated worker execution. The coordinator records candidate-specific review and checks before a handoff. Live-provider verification and human visual approval remain separate requirements.

`LocalRuntime` requires macOS and `/usr/bin/sandbox-exec`. It fails closed when required sandbox capabilities are unavailable. Worker source, policy, proposals, capture and scratch use separate canonical directories. Reviewer source is read-only; proposals and scratch are writable; runner capture is inaccessible to workers. Each attempt receives a fresh HOME and a whole validated subscription credential file in its own provider home, allowing local refresh without exposing the ordinary user home. Provider calls still need a dedicated subscription login.

Project schema version 2 uses `runtime: { kind: 'macos-sandbox', toolPaths, network, authHomes }`, with network set to `none`, `loopback` or `outbound`. Docker images and CPU, memory and PID quotas are removed. Existing Docker profiles require explicit migration. Native process-group supervision does not establish containment of deliberately detached descendants or resource-limit parity with containers.

## Open the dashboard

Use Node 24.19.x, then run:

```sh
npm run dashboard -- --root .factory --port 4317
```

Open the loopback URL printed by the server. To inspect existing runs, pass their state directory with `--root`. The dashboard reads each run through the existing validated Store. It does not create sample runs.

Use **Project setup** to save a GitHub URL, brief, recipient, and model assignments. Use **Connections** to check dedicated subscription credential directories and save or remove GitHub credentials and application API keys. Use **Budgets** to allocate attempts for implementation and verification, set token and time limits, and configure recovery thresholds.

Settings apply to future configuration. They do not rewrite existing runs or dispatch workers. Application spending is a planning allocation, not a provider-enforced cap. Subscription quota and dollar charges are unavailable. Unknown token usage remains unknown.

The overview shows recorded attempts, checks, events, recommendations, approved specifications, architect tickets, and question batches. Select a run to use **Start**, **Pause**, and **Stop**. Start launches its supervisor or resumes paused work. Pause suspends the worker process group and prevents new work. Stop cancels the run and terminates its owned processes. The dashboard checks recorded process identities before signalling them and reports incomplete cleanup. Recovery downloads preserve candidate identity and counters. They are not accepted release handoffs.

Credentials are stored in private files under the selected state root. The UI returns only credential presence, never saved values. API keys belong to the software being built and do not replace native subscription authentication. Saving a GitHub token does not validate repository access or clone a repository. Keep the state directory outside version control and do not expose this local server through a public proxy.

Claude integration is paused at the operator's request. The partial login dialog and backend are retained, but their real authentication flow is unverified. Workers still require a dedicated credential file. No dashboard check establishes live model access.

## Specifications, tickets, and questions

The coordinator records approved specifications, architect tickets, and question batches through `Store.recordCollaboration(run, { expectedRevision, specifications, tickets, questionBatches })`. The schemas in `src/contracts.ts` define these records. Approved revisions include content digests and named approval evidence. A changed specification needs a new ID; previous revisions and surfaced questions remain in the record.

Select a run to read its specifications and ticket progress. Ticket status follows linked worker attempts and affected questions. Worker completion alone does not mark a ticket done. Recorded review and checks must match the current candidate and specification.

Answer every question in a batch, enter your name, and select **Send answers to factory**. The server saves the whole batch in run state. A stale revision requires refreshing and reviewing before retry. Answers remain decision input until the coordinator reconciles them; they do not approve a new specification or resume execution.

**Sync tickets to GitHub** creates or updates issues in the run's HTTPS GitHub repository using the token saved under **Connections**. Issue bodies include linked approved specifications and architect progress. A run/ticket marker identifies owned issues for retry recovery. Sync is explicit; local worker progress does not trigger automatic GitHub writes. The token needs issue-write access to that repository. GitHub's [issue API](https://docs.github.com/en/rest/issues/issues) defines these operations.

## Headroom for factory workers

The free `headroom-ai` CLI 0.38.0 is installed on this workstation. Start the factory's dedicated local proxy with `npm run headroom` before dispatching workers. It listens on `127.0.0.1:8791` and writes private state under `.factory/headroom`; it does not change the existing proxy on port 8787 or user-wide Codex settings.

New CLI runs default to `headroom: { "baseUrl": "http://127.0.0.1:8791/v1" }` in the project profile. Workers receive an explicit process-local route even though they ignore user configuration. They retain native subscription authentication. No API key or desktop Headroom app is required. Existing persisted profiles remain unchanged. A saved profile without Headroom cannot dispatch workers. Create a new routed run instead of rewriting the old profile. Claude workers get `ANTHROPIC_BASE_URL` set to the Headroom base without its trailing `/v1`.

The launch command selects the general profile, lossless compression, and no retrieval markers. It disables lossy ML compression, user/system message compression, semantic response caching, memory, learning, and telemetry. Original prompts and captured evidence remain intact. The runner checks effective proxy settings before dispatch and stops when the configured service is unavailable or mismatched. Worker permissions remain limited to their isolated source, scratch, and loopback networking; the proxy state is outside their allowed files.

Inspect `http://127.0.0.1:8791/health` and `/stats` for effective settings and measured request savings. Savings depend on the request; an optimization flag is not evidence of a reduction. The installed [Headroom proxy](https://docs.headroomlabs.ai/docs/proxy) handles the routing and compression directly.

## Development

Use Node 24.19.x and install the locked dependencies with `npm ci`. Run `npm run verify` for typechecking and local behavioral tests. The tests create disposable Git repositories, launch harmless processes, exercise state recovery, and parse worker output. Native sandbox tests require macOS; retain any skipped or blocked results. Local fixtures do not establish subscription access or product acceptance.

On this workstation, the matching Node executable is `~/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node`. If the shell uses another Node version, run:

```sh
FACTORY_NODE="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
"$FACTORY_NODE" node_modules/typescript/bin/tsc --noEmit
"$FACTORY_NODE" --test tests/*.test.ts
```

## Design and verification

[The design](agent-factory-design.md) defines acceptance and recovery requirements. [The subscription ADR](docs/adr/0001-subscription-native-workers.md) supersedes its Pi runtime choice; [the native macOS ADR](docs/adr/0002-native-macos-runtime.md) supersedes Docker isolation and records its replacement's limits. [Shared skills](home/.agents/README.md) describe the approved worker bundles and their provenance limits.

[The verification skill](home/.agents/skills/verify-my-aifactory/SKILL.md) drives the implemented library interfaces. Its feature map separates those checks from the missing operator workflow. [The review status](docs/review-status.md) records confirmed fixes and remaining gaps.

Factory code now lives in this repository. The separate `floorplanto3d` application is a future client project and is not included or modified here.
