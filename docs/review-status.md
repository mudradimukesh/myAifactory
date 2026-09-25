# Factory review status

## CLI supervision and pause, 2026-09-25

Steps 14 and 15 use one CLI supervision path for run, resume and supervise, reject dashboard Pause for live unsupervised workers, and update the budget copy and ADRs. The supervisor, dashboard and coordinator suites pass 65 tests on Node 24.19.0 with zero failures or skips. New tests first reproduced missing supervisor events, conflict exit code 1, false pause success and misleading idle copy. TypeScript, JavaScript syntax, bundle loading and diff whitespace checks pass.

The CLI tests use disposable awaiting_input runs without provider calls. Existing process fixtures verify freezing, resuming and cleanup. These results do not establish live subscription pause behavior, provider connections surviving a long pause, or application acceptance. Full-suite verification remains with the orchestrator.

## Token dashboard steps 12 and 13, 2026-09-25

Two focused projection tests pass on Node 24.19.0. They cover saved segment equality, role and run totals, live sidecar context, nullable usage, schema rejection, and exclusion of check jobs and inactive sidecars. TypeScript, browser JavaScript syntax, and diff whitespace checks pass. The full dashboard test invocation crashed at Node's `InternalCallbackScope::Close` assertion after these two tests passed; its remaining HTTP tests were not verified in this sandbox.

The token panel and Playwright assertions are implemented. The screenshot script at `/tmp/factory-fp-0925/swarm/budget/ui/shoot.mjs` was blocked by `listen EPERM` on loopback before Chrome launched. Desktop, narrow layout, and one-second browser refresh remain unverified until the orchestrator runs that script outside the sandbox. No subscription calls or application acceptance were exercised.

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


## Discovery records and operator decisions, 2026-09-24

[LOCAL-VERIFIED] Approved business and domain specifications, architect tickets, and consequential question batches now persist in run state and appear in the dashboard and recovery briefs. Approved revisions and surfaced questions preserve their history. Complete named answer batches record atomically under revision locking without changing acceptance, suspension, or spent allowances. Ticket progress follows linked attempts and affected questions; worker completion alone does not mark delivery accepted.

[LOCAL-VERIFIED] Node 24.19.0 passes all 61 tests with zero failures and zero skips. TypeScript checking, browser JavaScript syntax checking, and `git diff --check` pass. The new tests reproduced the absent collaboration projection and the misleading post-answer prompt before their fixes. GitHub transport tests cover issue creation, owned-issue updates, retry recovery, pull-request exclusion, credential redaction, failed-sync persistence, and rejection of unrelated issue ownership.

[BROWSER-VERIFIED] Native Chrome displayed both approved specification kinds, approval details, revision digests, and a blocked architect ticket using disposable synthetic state. Selecting a constrained answer and entering free text submitted the complete batch. A direct read of persisted state confirmed both answers and operator identity, unchanged 700-token usage, preserved suspension, and `awaiting_input` status. Desktop specification and ticket layout was visually inspected. Responsive layout and the conflict-retry UI were not visually reverified in this pass.

[UNIMPLEMENTED] No execution coordinator consumes answers, reconciles revised acceptance, dispatches workers, or automatically publishes ticket updates. GitHub issue sync requires an explicit operator action and an HTTPS GitHub repository in the run profile. Mock transport evidence does not establish live GitHub permissions or remote delivery; no external issues were written. The scoped comment review found no comments or suppressions to remove, but the required Comment Sicko agent type was unavailable, so full no-comments certification is not claimed.


## Headroom request routing, 2026-09-24

[LOCAL-VERIFIED] Installed free Headroom CLI 0.38.0 runs separately on loopback port 8791. The factory injects the route into every configured Codex invocation, preserving native subscription authentication and the isolated worker home. New CLI runs select this protected proxy by default. Existing run profiles are not silently changed. The effective general profile disables user/system message compression, lossy ML compression, response caching, memory, learning, and telemetry. Proxy state has mode 0700 outside worker-writable directories.

[LOCAL-VERIFIED] A live sandboxed `gpt-5.6-luna` worker completed through the proxy. Headroom reported 651 tokens removed from 26,578 original request tokens, a 2.45% reduction for that synthetic run. The observed transform was tool-schema compaction; this is not a claim of the advertised compression percentages or subscription cost savings. The first two direct-provider startup checks failed before model output; enabling only the outbound macOS DNS socket and the system CA bundle fixed those reproduced integration failures.

[LOCAL-VERIFIED] The full suite passes 68 tests with zero failures and zero skips, including explicit proxy routing, fail-closed health settings, URL rejection, exact stdin, native auth isolation, private state/capture denial, and DNS restrictions. TypeScript checking and diff whitespace checks pass. Live Claude routing remains unverified. Floor-plan work remains paused until the final Headroom content check completes.


## Factory controls, 2026-09-25

[LOCAL-VERIFIED] Worker stall protection records `stall_start` and `stall_idle`, ends stdin on every process path, and exposes provider activity timestamps and recovery recommendations in the dashboard. Focused worker, coordinator, dashboard and type checks cover the local implementation.

[HUMAN-CHECK H2/H3] Live provider startup and resume event behavior remains unverified. Native process-group and browser stall rendering require the orchestrator's host checks. A 600-second idle default is profile-overridable; no live provider call was made here.

[LOCAL-VERIFIED] The integrated suite passes 111 tests with no failures or skips, and TypeScript checking passes. Real subprocess tests cover pause, resume, cancellation, duplicate starts, supervisor loss, orphaned children, and terminal-run cleanup. Failed cleanup retains ownership records and the reservation. Successful cleanup releases the reservation without changing a failed run into a successful one. Legacy profiles without Headroom are refused before a Codex attempt or execution-state transition. All seven worker roles load unslop, bro, guard-the-context-window, and never-block-on-the-human.

[LOCAL-VERIFIED] A dedicated Chrome profile exercised Start, Pause, Resume, and Stop through the HTTP dashboard with owned fixture processes. The browser observed running, paused, running, then cancelled state. The final run had no supervisor or active job. Keyboard focus survived redraws, and the 390-pixel layout had no horizontal overflow. These fixtures do not establish live subscription-worker execution.

[DEFERRED] The operator paused Claude integration after its subscription limit was reached. Partial login code remains in the repository. The real authentication flow is unverified, and no live Claude integration claim is made. Previous failed floor-plan runs, saved browser evidence, and pending human visual approval remain unchanged.

Evidence for this pass is in `/tmp/factory-codex-0925/`: `full-tests.log`, `live-final-results.json`, browser captures, per-agent reports, and `decisions.tsv`. Earlier entries describe their dated revisions.
