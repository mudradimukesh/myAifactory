# Process execution

A coordinator launches a bounded process and keeps captured evidence. `LocalRuntime` restricts native workers with a deny-default macOS sandbox. It must fail closed when required capabilities are unavailable.

## Sub-features

- Direct argv execution excludes inherited secrets; logs persist and redact split credentials.
- Timeout, cancellation, failure and normal completion clean up the supervised process group.
- Output limits bound runner-captured evidence.
- Worker stdin always reaches EOF, and provider startup or idle stalls stop the owned group with a distinct reason.
- Canonical workspace, policy, proposal, capture and scratch directories are disjoint.
- Reviewer source and policy are read-only; proposals and scratch are writable; runner capture is inaccessible.
- A fresh HOME receives only the validated dedicated credential file in its provider home, allowing attempt-local refresh.
- Network policy distinguishes `none`, `loopback` and `outbound`.

## How to get to it (user POV)

The callable entries are `runProcess`, `LocalRuntime.preflight/execute` and `validateAuthHome`. No operator CLI exposes them yet. Project schema version 2 selects `runtime.kind: 'macos-sandbox'`; old Docker profiles are not valid native profiles.

## Driving it with Node tests

Complete the parent skill's doctor checks. Run `"$FACTORY_NODE" --test tests/process.test.ts tests/runtime.test.ts`, capturing output and exit status under `FACTORY_EVIDENCE`. Require a successful exit and inspect any skipped tests. The process harness uses real local child processes; native boundary tests require macOS and working `sandbox-exec`.

Keep evidence of attempted capture/state reads and writes, reviewer writes, allowed proposal/scratch writes, symlink traversal, child-process inheritance, credential-copy boundaries and network modes. Exercise success and timeout with descendants that close or retain their pipes. Missing tests remain gaps even when the suite passes; consult `docs/review-status.md` for demonstrated coverage.

## Gotchas

Process groups do not contain deliberately detached sessions. Native runtime provides no hard CPU, memory or PID quotas and no pinned container image. A passing sandbox fixture does not prove installed provider compatibility, subscription access, credential-refresh behavior or full recovery after runner termination. Refreshed credentials stay in the attempt home and do not update the dedicated source login. Do not launch subscription workers from this recipe or copy credentials into evidence.
