---
name: verify-my-aifactory
description: Verify myAifactory's implemented process, candidate, state, and worker library interfaces using Node behavioral tests, while tracking the missing operator CLI and live integrations separately.
---

# Verify myAifactory

The current callable interface is a TypeScript library used by the future coordinator. There is no operator CLI yet. These recipes verify the real library through its public functions. They cannot establish a working factory, container isolation, subscription access, or product acceptance.

Read [the feature index](features/README.md) before selecting a recipe.

## Launch

Work from the repository root. Use Node 24.19.x and the locked dependencies from `npm ci`. On the original workstation, select the installed runtime explicitly:

```sh
FACTORY_NODE="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
FACTORY_EVIDENCE=$(mktemp -d "${TMPDIR:-/tmp}/my-aifactory-verification.XXXXXX")
```

On another machine, set `FACTORY_NODE` to its Node 24.19.x executable. There is no server or shared port. Each test invocation owns its temporary directories and child processes.

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

Do not substitute these tests for the unimplemented CLI journey listed in the feature index.

## Evidence

Capture the exact command, Node version, stdout, stderr, and exit code under `FACTORY_EVIDENCE`. Record the checked Git revision and whether the checkout was dirty. Retain failed runs alongside subsequent results. Docker fixture evidence must retain its fixture label.

## Cleanup

The tests await their processes and remove their own disposable directories. Do not kill processes by name or remove unrelated scratch directories. If a test is interrupted externally, inspect its recorded PID and ownership before cleanup. Keep `FACTORY_EVIDENCE` and confirm its logs and exit files still exist after the test process exits.

## Helpers

There are no skill-owned scripts. The maintained harness is `tests/*.test.ts`. Run the feature-map maintenance workflow after adding an operator command or changing a public library interface.
