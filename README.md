# myAifactory

A subscription-based development factory in progress. Native Codex and Claude Code workers will develop isolated candidates and submit them for independent review and runner-executed checks.

The repository currently contains tested library modules. It does not yet provide an operational factory CLI, acceptance coordinator, prepared container image, or live-provider verification.

## Development

Use Node 24.19.x and install the locked dependencies with `npm ci`. Run `npm run verify` for typechecking and local behavioral tests. The tests create disposable Git repositories, launch harmless processes, exercise state recovery, and parse worker output. Docker command tests use a named local fixture and do not establish container isolation.

On this workstation, the matching Node executable is `~/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node`. If the shell uses another Node version, run:

```sh
FACTORY_NODE="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
"$FACTORY_NODE" node_modules/typescript/bin/tsc --noEmit
"$FACTORY_NODE" --test tests/*.test.ts
```

## Design and verification

[The design](agent-factory-design.md) defines acceptance and recovery requirements. [The subscription ADR](docs/adr/0001-subscription-native-workers.md) supersedes its Pi runtime choice. [Shared skills](home/.agents/README.md) describe the approved worker bundles and their provenance limits.

[The verification skill](home/.agents/skills/verify-my-aifactory/SKILL.md) drives the implemented library interfaces. Its feature map separates those checks from the missing operator workflow. [The review status](docs/review-status.md) records confirmed fixes and remaining gaps.

Factory code now lives in this repository. The separate `floorplanto3d` application is a future client project and is not included or modified here.
