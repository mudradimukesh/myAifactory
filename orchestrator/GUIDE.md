# Running a new goal with the orchestrator kit

This guide is the maintained incident and operating record. Start here when taking over orchestration. The kit contains no product backlog or acceptance target from its source project. The names `p1-*`, `pilot_*` and schema `p1-review-2` remain compatibility identifiers, not instructions to reproduce that project.

The kit is a separate macOS/WezTerm operational harness under `orchestrator/`. It does not replace the factory's TypeScript runtime, sandbox boundary, credential handling or acceptance coordinator. Its command hooks enforce workflow rules; they are not OS isolation. Do not execute the factory's unrestricted inner worker commands outside their required runtime. See the repository design and ADRs for that runtime's authority.

## What is included

`scripts/swarm/` contains state transactions, token accounting, worker supervision, process ownership, lead handoffs, candidate preparation, red proof, lint/coverage/mutation gates, review validation, exact merge/push, alerts and optional inspection tools. `tests/swarm/` contains their regression suite. `.claude/agents/` contains the generalized lead, reviewer and optional specialist definitions originally used by the P1 chain. `templates/` contains a goal-free lead brief, acceptance format, reviewer criteria and project adapter.

The default gate profile expects Python under `src/`, tests under `tests/`, a project `.venv`, and a TypeScript web project under `web/` with npm scripts `lint`, `typecheck`, `test`, `e2e` and `mutation`. It is not a universal build-system adapter. A different stack needs explicit gate changes and regressions before dispatch. Non-applicable gates must have evidence, not fabricated passes.

The originating run was still held at extraction. Passing harness fixtures and one candidate's gates did not establish accepted product delivery. No full live swarm was launched from this exported branch during packaging; see the verification record below.

## Clone, install and verify

Use a new checkout and keep live run data outside it:

```sh
git clone --branch orchestrator-handoff-kit https://github.com/mudradimukesh/myAifactory.git
cd myAifactory
python3.13 -m venv orchestrator/.venv
orchestrator/.venv/bin/python -m pip install './orchestrator[dev]'
orchestrator/.venv/bin/python -m pytest orchestrator/tests/swarm -q
```

Prerequisites for live work: macOS, zsh, Git, WezTerm, compatible native Claude and Codex CLIs, existing authorized provider logins and the target project's toolchain. Node 24.19.x is the factory baseline. Pin actual model names and effort from available authorized accounts. No account purchase, credential copying, proxy creation or billing fallback is implicit in this guide. `CODEX_BASE_URL` and `ANTHROPIC_BASE_URL` are optional explicit routes; the kit no longer assumes local ports 8791/8787.

Configure a fresh goal. The paths and pane number below are examples to replace:

```sh
export SWARM_HOME="$PWD/orchestrator"
export REPO="/absolute/path/to/target-repository"
export ART="/absolute/path/to/private-run-data/new-goal"
export BIN="$ART/bin"
export PILOT_RUN="$ART/run.json"
export SWARM_STATE="$ART/state.md"
export LEAD_BRIEF="$ART/lead-brief.md"
export LEAD_AGENT=p1-pilot-lead
export FP_CAP_FRACTION=0.50
export PILOT_PYTHON="$SWARM_HOME/.venv/bin/python"
export PILOT_NODE="$(command -v node)"
export ORCHESTRATOR_PANE="replace-with-verified-pane-id"
export SWARM_PROJECT_ADAPTER="$ART/project_adapter.py"
export REMOTE=origin
mkdir -p "$ART" "$REPO/.claude/agents"
cp -n "$SWARM_HOME"/.claude/agents/*.md "$REPO/.claude/agents/"
cp -n "$SWARM_HOME/templates/lead-brief.md" "$LEAD_BRIEF"
cp -n "$SWARM_HOME/templates/project_adapter.py" "$SWARM_PROJECT_ADAPTER"
```

Inspect existing agent definitions instead of overwriting them. Rewrite the brief and acceptance with the new goal, allowed files, authority and independent success criteria. Put that goal's acceptance, ID list and test map under ART using a stable feature tag. Select exactly the product-review IDs required by its UI scope. Do not leave placeholder strings in a live packet.

The starter adapter provisions Python plus web dependencies and assumes tests own any test services. It creates each candidate's `.venv` before installing dependencies. For a service application, implement its `runtime(root, art, tag, env)` context manager to start real disposable services, record identities and stop them in `finally`. `provision(root, log)` returns the environment; `baseline_commands(root)` returns exact argument arrays. Exercise startup, failure and cleanup before spending model tokens. Missing `SWARM_PROJECT_ADAPTER` fails closed; the original application's database, API and identity provider are not bundled.

Initialize state with real UTC time and budgets chosen for this goal. This example is a planning allowance, not a promise that a goal costs this amount:

```sh
"$PILOT_PYTHON" - <<'PY'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(os.environ['SWARM_HOME']) / 'scripts/swarm'))
from run_state import transact
art = Path(os.environ['ART'])
state = dict(
    bootstrap=True, started_at=datetime.now(timezone.utc).isoformat(),
    lifecycle='preparing', blockers=[], deadlines={'overall': None},
    ceilings={'overall': 300000, 'preparation': 50000, 'pilot': 250000},
    consumed={'overall': 0, 'preparation': 0, 'pilot': 0},
    inventory={'threads': [], 'sessions': []}, assignments={}, reservations={},
    active_lead=None, dispatch_frozen=False, handoff=None,
    worktree=os.environ['REPO'], work_state_path=os.environ['SWARM_STATE'],
    approvals={'U6': False, 'U8': False}, runs={})
transact(art / 'run.json', 'orchestrator', 'init', state)
PY
/bin/zsh "$SWARM_HOME/scripts/swarm/interior-lead.sh" --dry-run
```

`init` refuses an existing state file. Never reset an old run to regain budget. Before real launch, record the baseline SHA, explicit merge/push authority, current state, inbox, approved builder assignment and inventory of the orchestrator itself through `run_state.py` operations. `U6` is the imported merge authorization flag; `U8` is an explicit mutation waiver, false by default. Do not infer either from template values.

Once preflight and the user's delegation are satisfied, use `set-lifecycle` with `running`, reason, and `work_state_path`, then invoke `interior-lead.sh --start`. The handoff controller obtains and verifies the lead identity. Native CLI access and notification delivery must be demonstrated in this environment before unattended work. A dry-run only prints the launch command; it does not establish provider access.

## Operating loop

1. Reproduce the user's problem through the real interface. Record the first failing step and exact baseline. Preserve dirty user files in their original checkout.
2. Use one builder and one independent reviewer. Delegate a specialist only when a concrete task warrants it. Run deterministic setup/check commands directly.
3. Provision the isolated worktree before dispatch. Write contract-derived tests first, prove red against the exact current base and freeze all test bytes before implementation.
4. Prepare a fresh integration candidate from that base. Run applicable gates against its exact SHA and current artifacts. A new test freeze, base or configuration requires new evidence.
5. Review the exact packet. Carry every unresolved historical finding with attempt-qualified IDs, for example `attempt-3:CR-1`. Preserve severity and text; require independent evidence for each fixed status.
6. Merge and push only the accepted SHA through the guarded integration path and only with recorded authorization. Rerun the original failing user step on the merged SHA.
7. Reconcile final usage and verified process cleanup, back up evidence and update this guide. Record local merge and remote push separately. A held task is not done.

Default new goals target 80% changed-line coverage. Configure the target project's coverage thresholds consistently before starting. Mutation remains 70% in this imported profile; all relevant survivors still require review. Do not retrospectively lower an active run's threshold to turn a failure into a pass.

The exported Claude cap is `166000 * FP_CAP_FRACTION`: the default pilot fraction of 0.50 is 83,000 tokens, plus a 10% handoff reserve. This is a conservative historical setting, not a measured optimum. The source workflow later received explicit authorization for a 95,000-token pilot-lead cutoff and bounded successor reading. That source change is not installed in this kit. Select future limits explicitly, verify the actual guard and launch configuration, and record the reason.

## Alerts, idle time and handoffs

The lead must send a pause/completion alert to the verified `ORCHESTRATOR_PANE`. Literal `wezterm cli send-text --pane-id <pane> ...` and its carriage-return submission are permitted for registered leads even while frozen, paused or past the cap. Shell interpolation, redirection and unrelated chained commands are outside this exception. Send complex payloads as literal text, not executable shell fragments.

Test receipt in the actual orchestrator conversation. A WezTerm send succeeding, a terminal reply, or a JSON file being written does not by itself prove that another chat wakes. After delivering an actionable ruling, the orchestrator ends its turn and goes idle until an alert arrives. Do not replace broken notifications with repeated model wakeups, transcript scans or sleep loops. Preserve a structured completion/held record as recovery evidence.

For handoff, use standalone `--handoff-begin`, write the exact token-named snapshot including verbatim current state, then standalone `--handoff-start <token>`. The snapshot records candidate/base, in-flight jobs, findings, failed approaches, debt, usage and one exact next action. Wait for builders, reviewers and gates to finish and reconcile their final usage before transferring the lead. A root-only signal does not prove background jobs survive the parent session ending. Verify successor acknowledgement and process identity before changing ownership. PID reuse or identity uncertainty never authorizes blind killing. The exported controller predates the source wait repair described below; validate that repair before relying on automatic draining here.

## Incident and fix ledger

Source implementation snapshot: `FloorInteriorDesignAndRenderer` commit `6d9bda6`. Commit IDs below identify that source repository; regression names identify this exported kit. All personal paths, live transcripts, provider credentials, product goals and run artifacts were excluded. Application-specific fixtures were replaced by a required project adapter; its token-expiry lesson is retained separately.

| Observed issue | Cause and correction | Evidence and prevention |
| --- | --- | --- |
| State/accounting could lose authority across resumes | Locked atomic transactions, inventoried identities, disjoint resume deltas and reservations | `test_run_state.py`; source F1 `830c640`, accounting `2c7a7e7`, `de3b618`, `f5f8e80` |
| Worker environment or model drift | Explicit assignment/model/effort and supervised worker lifetime | `test_pilot_worker.py`; F2 `2c02fe4` |
| Handoff ownership and snapshot failures | Separate freeze, snapshot, acknowledgement, commit and retirement stages | `test_pilot_handoff.py`; F3 `e3c2de8`, `bd7fffc` |
| Candidate or review evidence could become stale | Isolated exact-SHA candidate, hashed packet and guarded merge | Integration/review regressions; F4 `ec846e8`, F5 `31cdb1b`, F6 `56d4654` |
| Weak acceptance and quality tooling | Independent acceptance maps, pinned tools and explicit gate results | Acceptance/dependency/gate regressions; F7 `05bd2df`, F8 `1323968`, `d3c287c`, `a3d31d2` |
| macOS process exit/reaping races | Record complete identity and retain conservative cleanup failures | Owned-process/handoff regressions; `e451fa3`, `df13c1c`; one later identity-change abort still required reconciliation |
| Dry-run unexpectedly changed deployed files | Keep dry-run read-only; replace deployed script inodes atomically | `test_pilot_launch.py` verifies unchanged files and a running old chain surviving deployment |
| Missing npm dependencies consumed a builder turn | Provision the worktree before model dispatch; run fixed checks directly | Repair stopped at 60,329 new tokens; subsequent narrow resume used 19,054; dependency failures were not product acceptance |
| Broad artifact reads and redundant orchestration | Inspect only current alert evidence; use exact paths and compact outputs | Initial builder spent 56,250 tokens before implementation; orchestration itself exceeded its early estimates |
| Automated test rewriting damaged unrelated tests | Restore only the owned worktree changes and inspect the diff | Historical preparation incident; individual cost unknown; no blanket autofix over project tests |
| Baseline mypy/format failures looked like new regressions | Preserve baseline diagnostics and verify affected files are unchanged | 329 errors across 22 pre-existing files were recorded, not silently passed |
| Ruff diagnostic parsing missed file names | Parse the observed output shape; verify unchanged baseline files | `c893191`; diagnostic tests in `test_pilot_unit_gate.py` |
| Frozen tests failed after main advanced | Rebuild tests-first history from current main, not an old branch | Several failed candidates; prepare/freeze regressions; do this before expensive gates |
| Web coverage appeared to pass uncovered lines | Normalize web-relative LCOV paths and include every changed executable file | `d7c8402`; real Git/diff-cover regression fails for an uncovered changed line |
| Expired credential fixture was still valid | Expiry was inside a 30-second verifier allowance; move it beyond allowed skew | `3f1fc7a`; original real API response changed 200 to 401; new projects supply their own equivalent regression |
| Valid snapshot rejected on macOS | `/tmp` and `/private/tmp` compared lexically | `b2e64a7`; symlink snapshot regression |
| Frozen lead could not finish an incomplete snapshot | This recurred after the alert fix: snapshots described next steps but omitted verbatim state; reserve was exhausted after incorrect writes and compound commands | Preserve the original draft, append exact state under existing recovery authority, then standalone handoff-start. Include verbatim state before spending reserve on other calls; automatic snapshot assembly remains an improvement to validate. The guard remains strict for dispatch |
| Reviewer could not hand back after Stop | Completion check rejected handback and repeated Stop lost its allocation | `7962928`; registered reviewer handback and idempotent usage tests |
| Reviewer overshot its initial reservation unnoticed | Start/Stop-only hooks had no in-flight meter | First review used 226,483 new tokens; next used 178,163. Record actual usage, reserve based on evidence, do not claim a hard quota guarantee |
| Tests pinned implementation details | Assertions required private storage keys, module mocks, copy and transient ordering absent from acceptance | Independent review blocked acceptance; use public outcomes and independent account/value oracles |
| Removing entrypoint tests broke coverage | Browser coverage was not credited to unit coverage | Branch coverage fell to 81.81% against 85%; restore real entrypoint execution through browser-compatible dependency resolution |
| Mutation sandbox omitted repository-root fixtures | Stryker copied only the web subdirectory | Explicit in-place mutation in an isolated candidate, then verify the candidate tree was restored; preserve failure log |
| Failed mutation run displayed an old score | Output artifact from the prior attempt survived | Exit failure still blocked acceptance. Remaining issue: invalidate stale output before execution and require candidate/report provenance |
| Historical findings disappeared or IDs collided | Packet reset to `[]`; separate reviews reused CR-1/CR-2 | `3609ac9`; attempt-qualified IDs, unchanged severity/text and ambiguous-status rejection in review-gate tests |
| Alerts were blocked at the moment they were needed | Frozen/cap checks and polling regex treated notification prose as dispatch | `6d9bda6`; real-hook tests cover paused, frozen and 200,000-token contexts, wrong recipients and injected commands |
| Red test timed out instead of failing an assertion | Waiting for a network response that the defective implementation never sends | Assert the public outcome with a bounded expectation before waiting for that response; tests-turn correction was still in progress at extraction |
| Internal budget blocked an authorized repair | Cumulative usage and reservations crossed the recorded ceiling | Retain all usage, record an evidence-backed ceiling revision, and then admit the already-authorized allocation; never reset the run |
| Alert did not wake the orchestrator | External lead replies were not delivered to this conversation | User had to forward alerts. Verify the actual delivery channel before relying on idle operation |
| Saved session was discarded during transient verification failure | Credential cleared before the network result | Review found this; product repair was still pending at extraction. Do not record it as fixed without a regression and new review |

Individual incident costs are unknown unless stated. The originating preparation estimate was 862,000 new tokens; recorded preparation was 1,017,427, about 18% higher. These are one run's observations, not a cost prediction or comparative saving. Source reports also used incompatible historical task sizes; do not compare them as benchmarks.

## Measurement and unresolved limits

Count all inventoried orchestration, lead, builder, reviewer and preparation sessions. Claude new tokens are input plus cache creation plus output over unique message IDs. Codex new tokens are input minus cache reads plus output; resumed runs use disjoint deltas. Report cache reads separately. The parent factory has its own weighted budget unit; never mix the two meters without labeling them. Unknown final usage is not zero. Internal threshold revisions preserve original starts and totals and require evidence; provider quotas and user hard ceilings remain binding.

Report time as overlapping model, tool, worker, permission, user, sleep and notification-delay intervals. Unexplained time is unknown. Tokens per accepted behavior are undefined if no behavior was accepted. No live-provider compatibility, adversarial isolation, complete descendant containment or accepted application outcome is established by subprocess fixtures.

Remaining work before claiming broader readiness: live validation of the exported startup and notification channel; reviewer in-flight metering; stale mutation-output invalidation; independent review of this portability extraction; new-project runtime/fixture validation; and better evidence for context limits. The watcher can match quoted historical failures, so diagnose its evidence rather than treating every alert as a new incident. `cap_sim.py` retains historical illustrative weights; it is not current pricing or billing evidence. Optional Jev triage requires a separately authorized service/key directory and a goal-specific `JEV_UNITS_FILE`; it is not required or automatically enabled.

## Keeping this guide current

After each incident, reproduce through the real boundary, make the narrow fix, run the relevant regression and update this ledger in the same change. Record symptom, cause, exact change, test/commit, known cost and residual limitation. Correct or replace stale guidance instead of appending contradictory rules. Keep detailed private evidence in the run archive and publish only redacted references. Never copy the current user's goal into this reusable guide. `AGENTS.md` makes this maintenance step part of future work.

Verification of this extraction is recorded in `VERIFICATION.md`. Fixture validation is separate from live swarm acceptance.

## Orchestrator succession and failed retirement

Prepare succession by 100,000 new tokens and launch exactly one successor before the 125,000-token orchestrator cap. Record the telemetry source and context usage separately, review the handoff with writing-for-agents and unslop, and preserve the policy in every successor handoff. Late preparation and overruns stay in accounting. This does not change the lead context cap.

Register the successor's actual thread and integer `pane_id` through `run_state`, then verify its actual transcript model, effort, sandbox and approval policy. Send literal READY to the predecessor and submit a separate carriage return. Require an exact ACK relinquishing dispatch before freshly verifying the predecessor's PID, start time, process group, OS session and command. Retire only that verified root PID, preserve the sole lead and every job, verify absence, and reconcile final transcript usage. A launch command or an intended identity alone is insufficient.

After an identity-change refusal, verify every recorded process against fresh process data. Retire absent ownership through supported transactions only after surviving jobs have a safe disposition. Account both predecessor and aborted successor, preserve stop receipts and release completed reservations without resetting counters. A previous recovery does not establish current ownership.

Terminal pane identifiers change between orchestrators. Verify the new alert destination and demonstrate receipt in the actual successor conversation before unattended work. A hardcoded predecessor pane may work during ordinary execution but fail in frozen or over-cap guard exceptions; test both paths.

## Follow-up source incidents, October 2026

These incidents occurred after extraction in the existing source workflow. They document diagnoses, remedies and remaining work; this documentation update imports no executable fixes. Test names below refer to the source suite unless already present in this kit. Its last export verification remains `VERIFICATION.md`. Source/BIN equality, a passing regression or successful operational recovery does not establish full independent review, product acceptance or permission to merge.

| Symptom | Cause and remedy | Evidence and remaining limit |
| --- | --- | --- |
| A frozen lead could not write its snapshot after exhausting reserve | The snapshot exception also required usage below cap plus reserve. Source `pilot_guard.py` removed only that usage bound for the verified active lead's exact frozen token/path, retaining dispatch, pause, deadline and blocker checks | Snapshot and symlink regressions exercised 84,000 and actual 115,885-token contexts; 36 guard/state checks passed. Ordinary over-cap dispatch remains blocked. Source review remains required |
| Handoff entry reinstalled scripts and could discard deployed repairs | Source `interior-lead.sh` now calls the installed lead spawner for begin/start without a blanket reinstall | Shell syntax, source/BIN comparison and diff checks passed. Review the exact wrapper and deployed revision; no installation claim for this kit |
| Successor waited for adoption before acknowledging, or added an ended job to ACK | Startup instructions omitted the pending token details and exact registered jobs. Source launch prompts now include snapshot path/hash, ACK path, successor session and registered jobs JSON, requiring Read/Write before adoption and forbidding historical additions | Launch/handoff tests and actual supported adoption succeeded. Failed ACK/controller logs were archived unchanged before retry. Permissions never establish ownership |
| Valid commands or metadata writes remained blocked despite grants | Actual launch settings, command spelling and classifier decisions differed from assumed configuration. User-authorized corrections added narrowly scoped rules to the actual settings, preserving hooks/defaults; repeated denials were recorded. Direct metadata correction followed explicit user authorization | Existing launch/worker/acceptance checks passed 13. Original maps, IDs and prose were preserved; only report-backed failure labels and preservation entries changed, followed by genuine red proof. Do not infer live reload or bypass a renewed denial |
| Guard rejected the real lead after its model session changed at the same PID | Source `run_state` gained an orchestrator-only `reconcile-lead-session` transaction requiring actual transcript evidence and unchanged live identity, with no transfer, retirement or active worker | Six new cases joined 56 guard/state/handoff/launch checks. Historical identity, inventory, jobs and accounting were retained. Old-session dispatch stays rejected |
| Advisory ranking or receipts could hide failed progression checks | Missing receipts, near ties across old score buckets and incomplete no-call binding needed correction. Source Jev checks preserve customer order within 0.05, enforce mandatory deterministic checks and retain failed gates in diagnostic evidence | Source regressions cover missing candidate receipts, near ties, no-call binding and failed red proof. A helper test or historical paid receipt does not prove genuine result-consumption/pre-dispatch integration; exact call-site and receipt/ledger review remains mandatory |
| Lead retirement killed descendants or a gate disappeared despite root-only retirement | Source `owned_processes.py` and `pilot_lead.py` made lead retirement root-only. Later real gates still died when their parent session ended because builder reservations did not capture them | Builder/gate descendant regression joined 65 checks. Lost gate attempts were archived without a result. Source `drain_jobs` now blocks begin/start/controller launch and pre-retirement while builders, reviewers or live descendant gates remain; real-process tests reproduced failures before the fix and 157 swarm tests passed. Root-only receipts alone never prove gate survival |
| Supervisor identity race escaped finalization and a worker exceeded its allowance | A changing exec identity could raise between survivor discovery and signalling; `ValueError` escaped finalization. The old cutoff used 1.5 times allowance. Source supervision retains uncertain identities without signalling, completes conservative failure accounting, and stops at observed usage reaching allowance | Real-process budget/exec regressions reproduced then passed; source/BIN equality and Ruff were checked. One 40,000-token repair consumed 62,775. Reconciliation preserved FAIL/BUDGET and the unknown actual exit code/time; no invented successful cleanup or reopened released allocation |
| Stale completed-job receipt prevented lead transfer | Frozen jobs referenced an already ended repair before final accounting. Verify every recorded process absent, reconcile/release through existing transactions, preserve failed ACK/controller evidence and retry the same supported transfer | Actual successor adoption followed reconciliation. Unknown exit codes stayed unknown. Refresh registered jobs through the controller; never manufacture ACK, edit ownership or unfreeze directly |
| Missing red artifact caused packet writing to raise after a long failed gate | `artifact_hashes` raised `FileNotFoundError`, hiding the final packet. Source correction records the absent hash as null while retaining failure; the existing fast mode scopes static checks/related tests and defers the mandatory full run | `test_gate_writes_packet_and_fails_when_artifact_is_missing` and fast-mode/provision-marker regressions exist. A later full attempt still failed on missing red evidence and emitted no packet. Fast checks, detached execution and an installed fix do not establish a full gate pass |
| Full gate failed on formatting and lint in changed tooling | Exact Ruff format checks reproduced two failures; source and BIN were formatted together, and 23 guard lint errors were corrected with logic/string preservation checked | 51 existing guard/handoff/launch checks, targeted Ruff and diff checks passed. This final delta must join a refreshed exact candidate and independent review; an earlier candidate cannot cover it |
| Colocated tests were absent from discovery and plan coverage/mutation configuration | The existing acceptance did not own the necessary config changes. Explicit user authorization amended only test discovery, scoped coverage and mutation targets; original acceptance was saved unchanged | Before/after discovery reproduced the omission; 35 related tests and all 76 repository tests passed, with 100% lines and 96% branches. A scoped coverage invocation failed the global branch threshold until the full suite ran. No threshold reduction or full mutation pass |
| Resume ran with approval `never` despite intended on-request policy | Fresh-launch assumptions did not establish resumed CLI configuration. Source `codex_launch.py` corrects resume arguments and automatic review | Source hash prefix `7524bfd9`; `test_resume_workspace_write_keeps_automatic_review`; actual resumed turn confirmed on-request. Verify actual turn context, not only the command, and retain the prior failed launch |
| Approved assignment increase conflicted with its existing reservation | Source `run_state.assign` now updates an inactive, unreleased reservation for unchanged identity after budget checks, instead of allowing assignment/reservation drift | Source hash prefix `9e9aa91a`; increase/resume and active/released/overbudget rejection regressions; swarm/Ruff/diff checks passed. Explicit authorization increased 120,000 to 160,000; later BUDGET at 160,706 remains a 706-token overrun and released. No reopening |
| Handoff aborted because final worker usage was unknown even though counters existed | BUDGET termination marked the completed resume unknown. Verify recorded process cleanup and compare the actual final cumulative transcript counters with the launch receipt, then subtract the recorded starting counters and reconcile via existing `record-run` | Exact final resume delta was 54,803 new tokens and 4,088,832 cache reads. The original unknown/BUDGET/exit-1 launch receipt remains evidence; accounting became known without a new reply, commit or success claim. The same existing controller then completed exact ACK and adoption with the locked snapshot unchanged |
| Budget ended with dirty product residue and changed frozen test bytes | Stryker ran with `inPlace: true` and termination interrupted restoration. Its backup contains 34 source/config/test originals. Independent comparison found 33 current files differ only by added `// @ts-nocheck`; one form also contains mutant instrumentation. Four frozen browser-test backups match the frozen commit byte-for-byte | Full status/diff, 109 dirty files, original failure receipt and actual transcript were archived before restoration. Preserve genuine tracked and untracked product work, configuration changes and generated evidence. Restore only proven source/config/test originals after the ownership transfer and evidence-backed ruling; do not blanket-copy generated files or delete the backup. Restoration and remaining product allowance are pending. Released allocations stay closed |
| Final gate evidence contradicted the frozen snapshot's running status | The gate ended after the hash-locked snapshot was written. Its exit-1 evidence showed G1 frozen-hash mismatch, G2 missing red proof, G3 pytest error, G4 Ruff failure and packet failure | Preserve the locked snapshot byte-exact for ACK. After adoption, archive the attempt through the existing flow and record the correction in current state. Investigate the G3 KeyError-like symptom as a possible gate defect; its cause remains unconfirmed. No gate pass or acceptance |

Each source fix still requires inclusion in the refreshed exact tooling candidate, independent review and all mandatory gates. Retain every prior unresolved finding and the real integration/receipt/settings/guard/session scope. A separately reviewed tooling candidate does not accept unfinished product work. Validate and port a source repair explicitly before describing the exported kit as repaired.

### Recovery evidence and communication

Preserve the failure before retrying. If a failed ACK or controller log prevents the supported restart, archive it byte-exact under an attempt-qualified name. Keep the frozen snapshot locked. Reuse the existing controller only after proving the prior controller and any aborted successor absent, the sole lead unchanged, final usage known and budget sufficient. Never start a competing lead, reset counters or mutate ownership directly. If final telemetry cannot be measured, retain unknown and stop dependent admission.

Ceiling changes use the existing transaction API, preserve preparation and nonpilot consumption, and require explicit authorization. They do not increase individual allowances or reopen released allocations. An authorized second builder still needs a concrete independent slice and disjoint ownership; unused allocation is not a reason to launch it. Register and account a successor promptly so its inventory does not introduce another unknown-usage blocker.

Send only a necessary actionable response to an issue, blocked, paused or hung notification. Stop routine lead status, reminder and ownership messages once the channel is established. A literal READY or correction needs its separate carriage return; sandbox denial of that submission is a failed delivery, not a received message. Use the platform approval path and verify actual receipt before relying on idle operation. Report exact automated-review rejections without retrying through another route.

Broad transcript/process dumps also consume the orchestrator's allowance. Read only identity fields, current turn context, token counters and the failing evidence. Track preparation at 100,000 and the 125,000 cap frequently enough to act before either boundary. Observed preparation missed 100,000 in multiple sessions, and one retired orchestrator reached 176,697, exceeding cap by 51,697. Preserve those overruns. Fix-specific cost is unknown unless explicitly measured above; no accepted behavior cost can be computed while acceptance remains held.
