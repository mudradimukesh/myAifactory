---
name: verify-my-aifactory
description: Verify myAifactory's local operator dashboard and execution libraries, while tracking the missing coordinator and live integrations separately.
---

# Verify myAifactory

The callable interfaces are a local HTTP dashboard and a TypeScript library used by the future coordinator. These recipes verify public interfaces and distinguish recorded requests from execution. Native runtime uses macOS `sandbox-exec`; sandbox fixtures cannot establish subscription access or product acceptance.

Read [the feature index](features/README.md) before selecting a recipe.

## Launch

Work from the repository root. Use Node 24.19.x and the locked dependencies from `npm ci`. On the original workstation, select the installed runtime explicitly:

```sh
FACTORY_NODE="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
FACTORY_EVIDENCE=$(mktemp -d "${TMPDIR:-/tmp}/my-aifactory-verification.XXXXXX")
```

On another machine, set `FACTORY_NODE` to its Node 24.19.x executable. Native runtime tests require macOS and working `sandbox-exec`; record blocked or skipped tests rather than claiming coverage. Dashboard HTTP tests bind an ephemeral loopback port. Each test invocation owns its temporary directories and child processes. Use the dashboard feature recipe for interactive browser checks.

## Doctor

Run these read-only checks before driving, and again after a failed drive:

```sh
"$FACTORY_NODE" --version
"$FACTORY_NODE" node_modules/typescript/bin/tsc --noEmit
"$FACTORY_NODE" --input-type=module -e 'import {bundle} from "./src/bundle.ts"; const b=await bundle(); console.log(JSON.stringify({digest:b.digest,roles:Object.keys(b.content)}));'
```

Require successful exits, Node 24.19.x, and all seven role bundles. Missing dependencies or hash mismatches block driving; preserve the failure output. Do not repair a hash mismatch by accepting the current file without reviewing the skill change.

## Drive

Use the commands in the feature files. They run fresh, isolated processes and assert observable output, filesystem state, candidate contents, or child-process liveness. To run all existing library tests:

```sh
"$FACTORY_NODE" --test tests/*.test.ts > "$FACTORY_EVIDENCE/modules.log" 2>&1
FACTORY_EXIT=$?
printf '%s\n' "$FACTORY_EXIT" > "$FACTORY_EVIDENCE/modules.exit"
cat "$FACTORY_EVIDENCE/modules.log"
test "$FACTORY_EXIT" -eq 0
```

Use [the dashboard recipe](features/dashboard.md) for browser verification. Library and HTTP tests do not establish the unimplemented coordinator journey listed in the feature index.

## Evidence

Capture the exact command, Node and macOS versions, stdout, stderr, and exit code under `FACTORY_EVIDENCE`. Record the checked Git revision and whether the checkout was dirty. Retain failed runs alongside subsequent results. Label subprocess fixtures, real local sandbox checks and live subscription calls separately. Never include credential files in evidence.

## Cleanup

The tests await their processes and remove their own disposable directories. Do not kill processes by name or remove unrelated scratch directories. If a test is interrupted externally, inspect its recorded PID and ownership before cleanup. Keep `FACTORY_EVIDENCE` and confirm its logs and exit files still exist after the test process exits.

## Helpers

There are no skill-owned scripts. The maintained harness is `tests/*.test.ts`. Run the feature-map maintenance workflow after adding an operator command or changing a public library interface.
