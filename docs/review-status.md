# Factory review status

Reviewed on 2026-09-23. The factory remains incomplete and is not approved for operational use.

## Confirmed fixes

Candidate import now preserves NUL-delimited Git filenames. A reproduced leading-space filename bypassed the allowed-path check before the fix; a Unicode filename was incorrectly rejected. Tests now reject the disallowed path and read the approved imported file from candidate storage.

Process logs flush while the worker runs. Split credentials remain withheld until the redactor can distinguish a complete match from a longer shared-prefix secret. Tests read logs during execution and check literal redacted output.

Timeout cleanup kills surviving descendants even after the direct child's pipes close. The regression launches a descendant that ignores SIGTERM and checks process liveness after cleanup.

Source digests now include symlink targets. Persisted state validates nested attempts and evidence before reading or committing them. Recovery tests reject conflicting history and malformed evidence, repair only the final interrupted event, and exercise the state-root reservation.

Before native migration, container ownership and stdin fixes were checked with a Docker fixture. ADR 0002 supersedes that runtime; those historical checks do not establish native isolation.

## Structural changes

Validation schemas now supply the canonical state, evidence, role, and worker-choice types. Bundle configuration rejects missing roles, unsupported tool names, malformed file records, and changed instruction contents. Reservation files are parsed against their schema. The duplicate runtime interface had no callers and was removed. Source formatting exposes statements previously packed onto single lines. No file approaches 1,000 lines.

The factory was moved into its own Git repository. Bundle resolution now starts at that repository's root. All seven locked role bundles load after the move. The package no longer advertises an unimplemented CLI command.

## Remaining blockers

The execution coordinator and acceptance workflow are absent. The local dashboard can inspect records and save setup inputs, but cannot execute a run. Schema-valid state is not proof of independent review, executed mandatory checks, a current artifact, or safe handoff. These gates must be demonstrated before operational acceptance.

ADR 0002 replaces Docker with native macOS execution. Live subscription-worker prompt delivery, attempt-local credential refresh and complete worker recovery remain unverified. Discovery and architecture scratch-write permissions need testing with each provider while preserving candidate read-only access.

Upstream skill provenance and licenses remain unverified. Role tool metadata and native command permissions are not derived from one checked contract. The reviewer has not been calibrated with known-correct and seeded-defect candidates. The design's full acceptance matrix and product evaluation remain outstanding.

## Native macOS migration

[DESIGN] The user authorized removal of Docker. [ADR 0002](adr/0002-native-macos-runtime.md) selects fail-closed `LocalRuntime`, project schema version 2, disjoint canonical job directories and attempt-local HOME/provider state. Whole validated credential files are copied to the attempt home for local refresh; source logins are not updated. Docker image and CPU/memory/PID quota fields are removed, with no parity claim.

[LOCAL-REPRO] The final native suite passes all 34 tests with zero failures and zero skips on macOS using Node 24.19.0. TypeScript typechecking also passes. Actual sandbox subprocesses verify capture and unrelated-file access denial, read-only reviewer source, writable developer source, permitted proposal/scratch writes, symlink and inherited child restrictions, denied networking and permitted loopback, stdin delivery, and attempt-local credential copies. Process tests verify same-group cleanup after success, failure, timeout and cancellation, plus capture-error handling. No authenticated model calls were made.

[FIXED] F1 and F3 now validate copied object data through runner-owned Git metadata, ignoring worker configuration and replacement refs. F2 explicitly fetches each candidate into fresh checkouts. Regression tests cover sequential candidates, original-tree validation, dirty indexes/worktrees and packed repositories. F4 separates runner capture from sandbox-writable output and verifies denied capture access. F5 terminates surviving same-group descendants when the parent exits. Deliberately detached sessions remain outside this guarantee.

[UNTESTED] Live subscription refresh, all native role/provider combinations, detached-session containment, full restart recovery, reviewer calibration and the complete operator acceptance journey remain outside local fixture evidence. Process-group cleanup does not establish containment of deliberately detached sessions. The historical findings below describe the reviewed revision and are not rewritten as proof of the migration's fixes.

## Verification scope

[SOURCE] The follow-up [architecture and implementation review](../agent-factory-design.md#14-architecture-and-implementation-review-2026-09-23) inspected commit `83351370f40583c641d3ed45da0d928d14d7372a`. It recorded the following defects at that revision. The native migration above fixes their reproduced cases:

- [LOCAL-REPRO] Candidate submission can execute a worker-configured Git filter on the host before rejecting a dirty worktree.
- [LOCAL-REPRO] Import succeeds but a fresh verifier clone cannot check out a candidate retained only under `refs/candidates`.
- [LOCAL-REPRO] Worker Git replacement refs can make permitted-path validation inspect a different tree from the imported original commit.
- [TRACED] The writable worker output mount contains runner-captured logs. [LOCAL-REPRO] Same-user direct file writes fabricated captured evidence. Live Docker exploitation was not tested.
- [LOCAL-REPRO] Normal direct-process completion can leave descendants alive beyond the timeout. This does not establish a Docker workload leak.

[LOCAL-REPRO] At the reviewed revision, the existing 23 tests and typecheck passed on Node 24.19.0. That suite omitted these submission/materialization, evidence-tampering, and normal-exit descendant cases. [DESIGN] Repair the existing boundaries first; defer new public architecture until implementing the missing operator journey. [UNTESTED] All previously listed live integration and product-evaluation blockers remain.

The maintained [verification skill](../home/.agents/skills/verify-my-aifactory/SKILL.md) has three feature recipes. Independent source readers checked each map entry, and each recipe was executed locally. The skill explicitly records uncovered runtime paths and unknown usage behavior. It does not claim complete library or operational coverage.

The review used the requested design, rationale, blast-radius, behavioral verification, migration, sequencing, subtraction, and strict maintainability skills. `coupling-analysis` could not be located in the supplied skill directories; coupling was inspected directly without claiming that skill ran.

Historical evidence is limited to the supplied handoff, design, and subscription ADR. The imported factory had no Git history, issue tracker, team chat, production observability, error tracking, or analytics evidence. Do not infer implementation-time intent from code alone.

## Local operator dashboard, 2026-09-24

[LOCAL-VERIFIED] Added a loopback HTTP dashboard for configured roles, recorded attempts, reported token counts, local budgets, private credential storage, auth-home checks, and recovery briefs. It uses Store revision locking for pending suspend/cancel requests and never claims a worker has stopped. The dashboard does not launch agents or advance acceptance state.

[LOCAL-VERIFIED] Node 24.19.0 typechecking and all 45 behavioral tests pass, with zero failures and zero skips. Eleven dashboard API tests cover origin/Host/CSRF rejection, body limits, settings validation, private credential writes and removal, symlink refusal, corrupt record isolation, usage projection, recovery counters, and stale control revisions. JavaScript syntax checking passes. Independent backend/frontend review and the scoped no-comments pass found no remaining findings.

[BROWSER-VERIFIED] Browser checks used disposable synthetic records. Project inputs persisted after reload and invalid repository URLs produced actionable errors. Budgets saved an eight-attempt allowance, three-attempt verification reserve, and a $25 planning allocation. An application key stayed masked after reload and was removed. The recovery file downloaded through Safari preserved three attempts, 35,600 reported tokens, and unknown usage. A suspend request remained pending without changing the running status. Desktop layout and a 400-pixel responsive view were visually inspected.

[UNIMPLEMENTED] Live coordinator supervision, heartbeat collection, automatic context replacement, model escalation, GitHub clone/access checks, application-key injection, provider quota and spend enforcement, and the complete accepted-release workflow remain absent. Credential-file shape validation is not a live authentication test. The macOS Claude Keychain-only login is not imported by the existing runtime.
