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

The inherited Claude cap is `166000 * FP_CAP_FRACTION`: the default pilot fraction of 0.50 is 83,000 tokens, plus a 10% handoff reserve. This is a conservative historical setting, not a measured optimum. A proposed increase to 125,000 was withdrawn; no increase was deployed. Select future limits explicitly and record the reason.

## Alerts, idle time and handoffs

The lead must send a pause/completion alert to the verified `ORCHESTRATOR_PANE`. Literal `wezterm cli send-text --pane-id <pane> ...` and its carriage-return submission are permitted for registered leads even while frozen, paused or past the cap. Shell interpolation, redirection and unrelated chained commands are outside this exception. Send complex payloads as literal text, not executable shell fragments.

Test receipt in the actual orchestrator conversation. A WezTerm send succeeding, a terminal reply, or a JSON file being written does not by itself prove that another chat wakes. After delivering an actionable ruling, the orchestrator ends its turn and goes idle until an alert arrives. Do not replace broken notifications with repeated model wakeups, transcript scans or sleep loops. Preserve a structured completion/held record as recovery evidence.

For handoff, use standalone `--handoff-begin`, write the exact token-named snapshot including verbatim current state, then standalone `--handoff-start <token>`. The snapshot records candidate/base, in-flight jobs, findings, failed approaches, debt, usage and one exact next action. Drain reviewer subagents before retiring their parent. Verify successor acknowledgement and process identity before changing ownership. PID reuse or identity uncertainty never authorizes blind killing.

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
