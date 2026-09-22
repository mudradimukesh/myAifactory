# Process execution

A coordinator launches a bounded process, receives its outcome, and keeps captured evidence. Container management must refuse unrelated containers.

## Sub-features

- Direct argv execution excludes inherited secrets.
- Logs persist while a worker runs and redact split credentials.
- Cancellation and timeouts stop the process group, including descendants with closed output pipes.
- Output limits bound stored evidence.
- Docker ownership checks use an inspected immutable container ID.

## How to get to it (user POV)

The mapped recipe drives `runProcess` and `DockerRuntime.stop/remove`. `DockerRuntime.preflight/execute` and `validateAuthHome` also exist, but this recipe does not cover their diagnostics, credential validation, or full launch behavior. No operator CLI exposes these entries yet.

## Driving it with Node tests

Preconditions: complete the parent skill's doctor checks.

Run `"$FACTORY_NODE" --test tests/process.test.ts tests/runtime.test.ts`, capturing output and exit status under `FACTORY_EVIDENCE`. Require a successful exit. The harness launches real child processes and checks persisted bytes and descendant liveness. The runtime test uses a disposable executable named `docker`, checks refusal of foreign containers, and records the addressed container ID.

## Gotchas

The Docker executable is a fixture. Its passing test does not prove real container stdin delivery, mounts, credential refresh, sandbox behavior, or isolation. Do not launch subscription workers from this recipe.
