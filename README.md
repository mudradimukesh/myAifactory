# myAifactory

A subscription-based development factory in progress. Native Codex and Claude Code workers run through a restricted local macOS runtime and submit candidates for independent review and runner-executed checks.

The repository contains a local operator dashboard and execution library modules. An execution coordinator and automated acceptance workflow remain unimplemented. Live-provider verification remains outstanding.

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

The overview shows recorded attempts, checks, events, and recommendations. A saved `running` status is not proof of a live process. Recovery downloads preserve the candidate identity and counters for a fresh coordinator context. They are not accepted release handoffs. Suspend and cancel controls record pending requests; no coordinator currently consumes them or stops a process.

Credentials are stored in private files under the selected state root. The UI returns only credential presence, never saved values. API keys belong to the software being built and do not replace native subscription authentication. Saving a GitHub token does not validate repository access or clone a repository. Keep the state directory outside version control and do not expose this local server through a public proxy.

Claude login can use macOS Keychain, while this runtime requires a dedicated credential file. The connection check reports this limitation rather than extracting tokens. Follow the [Claude authentication documentation](https://code.claude.com/docs/en/authentication) for native sign-in. No dashboard check establishes live model access.

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
