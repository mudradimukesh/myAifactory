# Use restricted native macOS execution

The user requested removal of Docker. Use `LocalRuntime` in `src/runtime.ts` with macOS `/usr/bin/sandbox-exec`; do not add an unrestricted fallback, privileged service, account provisioning or VM. This decision supersedes the container runtime, prepared-image and container-resource assumptions in the original design. ADR 0001's native subscription commands and billing restrictions remain in force. No operator CLI or acceptance coordinator is added.

## Boundary

The runner generates a deny-default sandbox policy and fails closed when it cannot enforce the required capabilities. Canonical `workspace`, `policyDir`, `outputDir`, `captureDir` and `scratchDir` must be disjoint. Developer source may be writable; reviewer source is read-only. Policy is read-only. Proposals and scratch are writable. Runner-owned capture and state are inaccessible to workers. Capture is written by the runner through stdout/stderr pipes, not mounted or exposed as worker output.

Resolve configured tools to narrow executable/dependency paths. Do not grant an entire user home or shared temporary root to make a tool work. Each attempt has an isolated HOME and temporary state. Network policy is explicit: `none`, `loopback` or `outbound`. Outbound access is not a provider-domain allowlist.

Execute generated native worker commands only through `LocalRuntime`. Codex uses `danger-full-access` for its inner command policy because macOS rejects nesting its Seatbelt sandbox inside the runner's sandbox. The outer deny-default policy still enforces source, scratch, capture and network access. Approval remains `never`. These command arguments are not safe to run directly outside `LocalRuntime`.

Validate dedicated subscription credential homes, then copy the entire permitted provider credential file into a fresh attempt-local provider home. This permits the native CLI to refresh credentials locally. It does not extract tokens, copy ordinary user settings/plugins, mount the user home, or fall back to API keys. Refreshed credentials are not written back to the source home. Concurrent or later attempts may therefore require a fresh dedicated login if provider refresh invalidates earlier credentials; live-provider behavior remains to be established. Workers can read the credential they use. Credential files and attempt homes must remain private and must not enter evidence, proposals or handoff artifacts.

## Lifetime and limits

The process runner supervises its process group on success, failure, timeout and cancellation, including children that close or retain output pipes. This closes the reproduced normal-exit leak for same-group descendants. A deliberately detached session can leave that group; no claim of complete adversarial descendant containment is made. Restart recovery must not signal a persisted PID without proving ownership, because PIDs can be reused.

Timeout and log-byte limits remain. Native execution does not enforce the former container CPU, memory or PID quotas. It also depends on the host toolchain rather than a pinned image. Record the host and tool versions in verification evidence. The local manual marks `sandbox-exec` deprecated; Apple states that its sandbox profile language is unsupported for third-party development. Required capabilities need testing on each supported host version, with no unrestricted fallback. [Apple DTS explanation](https://developer.apple.com/forums/thread/661939)

## Profile migration

Project schema version 2 requires:

```ts
runtime: {
  kind: 'macos-sandbox',
  toolPaths: string[],
  network: 'none' | 'loopback' | 'outbound',
  authHomes: { codex: string | null, claude: string | null }
}
```

Remove `image`, `cpus`, `memoryMb`, `pids` and `bridge`. Reject obsolete profile fields instead of silently ignoring limits. Persisted state embeds the project profile, so old runs require an explicit migration decision; completed history is not rewritten automatically. Candidate identity, independent checks and runner-owned acceptance evidence remain required.

## Verification status

The native suite passes 34 tests with zero failures and zero skips; typechecking passes. Real macOS sandbox subprocesses exercise access restrictions, network modes, credential copies and stdin. Process regressions exercise same-group cleanup and capture failures. Git regressions exercise trusted object validation and exact candidate checkout. See [the verification record](../review-status.md) for scope and remaining integration gaps. These checks do not establish live provider compatibility or application acceptance.

## Caller and ownership

```ts
const runtime = new LocalRuntime();
const errors = await runtime.preflight(project);
// Reject the job if preflight reports errors.
const result = await runtime.execute({
  id, project, workspace, policyDir, outputDir, captureDir, scratchDir,
  argv, readOnlySource: true, network: 'none', timeoutMs, maxLogBytes, signal,
});
```

`runtime.ts` owns sandbox policy and directory permissions. `process.ts` owns capture and process-group cleanup. `git.ts` validates original commit objects using runner-owned metadata and materializes exact candidate commits. Stop the producing worker before importing its workspace.

The design comparison retained this small runtime owner. Unrestricted local execution cannot protect runner evidence or private files. A larger Runner abstraction would add missing coordinator responsibilities without improving OS isolation, so it remains deferred.
