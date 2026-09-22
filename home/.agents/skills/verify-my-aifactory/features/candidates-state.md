# Candidate and state handling

A coordinator imports only approved changes and retains a consistent state and event history across interrupted writes.

## Sub-features

- Candidate import respects exact filenames, including leading spaces and Unicode.
- Changing a symlink target changes source identity.
- Recovery repairs only a missing or partial final event.
- Malformed evidence and conflicting event history fail validation.
- The state-root reservation excludes a second run until released.

## How to get to it (user POV)

The callable entries are `importCandidate`, `treeDigest`, and `Store`. The CLI and final acceptance coordinator are not implemented.

## Driving it with Node tests

Preconditions: complete the parent skill's doctor checks and have Git installed.

Run `"$FACTORY_NODE" --test tests/git.test.ts tests/store.test.ts`, capturing output and exit status under `FACTORY_EVIDENCE`. Require a successful exit. The harness commits changes in disposable repositories, reads imported contents, mutates event files, and verifies recovered state and rejected writes.

## Gotchas

Schema validity does not establish acceptance. A candidate still needs independent review, current checks, and artifact evidence. The absent coordinator has not demonstrated those gates. Never use the original application repository as a test fixture.
