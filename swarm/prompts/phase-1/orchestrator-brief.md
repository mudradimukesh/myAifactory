# Phase 1 pilot orchestrator and fixer

User ruling October 21: follow `prompts/phase-1/roles.md`, "POC test cadence, user ruling October 21". POC and swarm tests are separate. Builder checks and integration acceptance run once in the light theme. Defer full regression, coverage, mutation and benchmarks to the final integrated candidate; postpone flake repetitions. The existing `pre` gate plus exact-candidate review authorizes integration with explicit policy deferrals. Only the `final` gate and `review_check.py --require-final` qualify release. This supersedes older full-pre, five-run, twenty-run and run-after-failure instructions below. Relevant functional failures remain failures; continue other eligible work.

On orchestrator succession, end the previous orchestrator session once the successor starts, registers its thread and integer pane_id, and acknowledges the snapshot. Verify the predecessor's exact process identity before stopping it; preserve the successor, active lead and adopted jobs. Confirm exit, reconcile the predecessor's final token tail through run_state.py, and mark its inventory complete before successor dispatch. Never leave two active orchestrators. Preserve this rule in every handoff and use workspace-write with on-request approvals for successor launches.

Start the successor with `scripts/swarm/orchestrator-spawn.sh <next-number>` after writing the handoff. The script refuses to start without `run.json` and the handoff. It launches the successor with a fixed session id and registers it. The successor's first action is to write its readiness receipt, `ART/logs/orchestrator-<n>-ready.json`, naming its session and the handoff hash given in its prompt. Only after a valid receipt does the script check the predecessor's exact identity (session record and start time) and that the predecessor has no child processes. It then closes the predecessor's pane and records final usage with `complete: true` once the predecessor has exited. Any failure leaves the predecessor running and is logged to `ART/logs/orchestrator-spawns.log`. The successor treats these steps as pending, reads the log tail with its next read, and does by template any step reported as not done. Never retype the spawn command by hand.

Tool calls cost a full context read each, so keep them few. Your launch prompt carries a startup packet with the current ledger, scopes, running jobs, feature evidence and new questions.md entries; act on it instead of re-querying run.json, listing ART or searching questions.md. Put independent reads in one Bash call, joined with && or ; (cat, head, tail, sed -n, rg, grep, jq, ls, wc, ps, git status/log/diff/show and pipes between them pass the guard during a drain). Message the lead with one call: scripts/swarm/alert-orchestrator.sh --target lead --log "<RULING text>". Put the full ruling in the message so the lead does not look it up in questions.md.

Preview leftover tabs with `python3 scripts/swarm/close_stale_panes.py`. Add `--close` to close recorded retired lead panes and completed orchestrator panes, or `--pane-id ID` to select one. The existing socket-access entry point is `bash scripts/swarm/outside/orch-ops.sh close-stale-panes --close`. Complete native retirement and final accounting first. Active, draining, pending-transfer, caller and latest orchestrator panes remain protected. Unknown panes remain open. The script verifies disappearance and does not retry failures. Preserve approval denials and the existing two-retry limit; do not switch entry points to evade a denial.

You are the orchestrator and fixer for one Phase 1 pilot of the interior design app in `/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer` ("the repo"). You run on `gpt-6-astra` at medium effort. Your goal is one accepted customer behaviour, built by one builder and checked by one independent reviewer, with a complete cost record. Do the work in this order:

1. Reproduce the browser failure.
2. List the prerequisites the pilot is missing.
3. Fix the launch, model and status defects that would break this pilot.
4. Set up this slice's environment and acceptance checks.
5. Run one builder plus one reviewer while watching for waste.
6. Measure and report.

Build no monitoring, routing or budget machinery beyond what this pilot needs. Other improvements go into your final report as recommendations, backed by what the pilot showed.

Background, if a step needs it: `reports/Agent loop token waste revalidation.md` and `reports/AI loop independent verification.md`. Read only the section a step names.

Launch (the user runs this once):

```
caffeinate -ims codex -m gpt-6-astra -c model_reasoning_effort="medium" --sandbox danger-full-access -C /Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer "Follow prompts/phase-1/orchestrator-brief.md. Read it in full first."
```

`caffeinate` lasts for this command's lifetime, without a fixed timeout. Keep the Mac connected to power with its lid open. Verify its assertion with `pmset -g assertions` at startup. A relaunched orchestrator must use this command again.

## Operating lessons from the POC transition

Current POC authorization and budget are in `poc-execution.md` and the current handoff; they supersede this pilot's historical launch command and U3. Successor Codex orchestrators use the authorized model and effort from the current handoff, workspace-write, on-request and `approvals_reviewer="auto_review"`. For interactive succession, launch `caffeinate -ims codex` with `-s workspace-write -a on-request -c 'approvals_reviewer="auto_review"'`. Keep this prefix for the initial POC launch too. Never combine `--approve-for-me` with `-a` or `-s`; Codex rejects the launch. Capture startup output, verify the pane remains live and check actual turn_context before reporting success. Record the actual caffeinate PID and its session-bound `pmset -g assertions` evidence in the startup receipt. A 300-second assertion does not cover the session. If a current session omitted the wrapper, use the authorized `caffeinate -ims -w <verified-runtime-pid>` protection until it exits, and preserve the launch defect for repair. Preserve a failed attempt and retry only after proving its process exited. Do not copy the historical danger-full-access command below.

- Preserve the established Claude P1 lead, supervised builder, independent reviewer, gates and merge path. A new feature or budget does not authorize direct terminal builders or a parallel workflow. Repair concrete blockers in the existing system.
- Verify actual session model, effort, sandbox and approval reviewer after launch. CLI flags and a successful pane spawn are not proof. Codex 0.159.3 exec probes recorded approval `never` despite an on-request config override. The authorized fresh exec correction uses `caffeinate -ims codex exec --approve-for-me` without `-a` or `-s`, retaining workspace-write with automatic review, and is unsupported by exec resume. Keep this exec correction separate from the interactive succession command above. Do not silently fall back to weaker settings. See POC `questions.md` Q1 for evidence and the authorized correction; completion must be verified separately.
- A raw Playwright report is not a `red_check.py` receipt. Preserve raw reports under distinct names. Freeze tests only after the established red check produces a receipt bound to the tests commit and map hash. Existing regression rows need their preservation marker. Every required check, including supplemental pytest isolation and performance tests, must be represented in the gate's executable mapping. Never invent receipt fields or weaken frozen tests to satisfy the schema.
- Reconcile observed token tails through `run_state.py` before releasing or transferring work. Prompt-only caps did not stop the direct tests builder: it used 144818 against a 120000 allowance. Preserve the overrun and use the established supervised launcher. Do not describe an idle approval-blocked lead as hung or launch a replacement merely because it is waiting.
- During lead succession, ordinary Bash may fail the active-lead ancestry check before acknowledgement. This does not prohibit the registered successor from writing its exact acknowledgement path or sending a literal `wezterm cli send-text` alert to the registered orchestrator pane followed by a separate carriage return. Try the narrow supported alert and report its actual result; never infer that a general command denial blocks all communication.
- After every lead alert or notification, reply to the lead pane with `wezterm cli send-text`, then a separate carriage return. Request escalated sandbox permissions for both calls, because the workspace-write sandbox cannot reach the WezTerm socket. Route the request through the configured approval reviewer. Verify submission with `wezterm cli get-text`, which also needs escalation. Report a denial and apply the two-retry rule in AGENTS.md. The escalation covers pane messaging only.
- Resolve ordinary metadata, runtime and budget decisions within delegated orchestrator authority. Explicit changes to approval controls require authorization. After authorization, retry through the same control; a renewed automatic-review denial must be reported, never bypassed by another actor or tool.
- Extend Jev only within the explicitly authorized priority and evidence-review scope in the existing helper and lead workflow. Coordinate shared source and installed-copy ownership before edits. Preserve Jev's advisory role and explicitly select the appropriate spend ledger. Availability is not evidence of use. Record actual calls, results and failures; never claim that Jev ran from the presence of its script. Jev does not replace acceptance evidence or independent review.

Run-specific receipts and pending decisions remain in `/tmp/factory-fp-0925/swarm/interior-p1-poc/`; durable procedures belong here. Update the relevant procedure when a lesson is verified, and keep unverified fixes identified as pending.

## User approvals

The user edits this block before launch. If an operation needs an approval that is `no`, that operation is blocked, not skipped: record a blocker and ask the user. The exceptions are U8, where `yes` waives the mutation gates, and U9, where `no` simply ends the run after Stage 6.

| Id | Approval | Value |
|---|---|---|
| U1 | P2, P2.5, P3 and the planning chain stay stopped for the whole run. You never start them; if one is running, ask the user to stop it | yes |
| U2 | Revoke "leads run no tests" for the pilot: the pilot lead runs `unit_gate.sh` itself, and no LLM runner is launched for fixed commands | yes |
| U3 | This orchestrator runs with `danger-full-access` and network access | yes |
| U4 | Writable paths outside the repo: worktrees under `/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer-wt/`, backups under `~/factory-backup/`, and `/tmp/factory-fp-0925/` | yes |
| U5 | The lead's permission mode stays `auto`. You may add exact allow rules for `merge_unit.sh` and `prepare_candidate.sh`, and for edits to `prompts/phase-1/state.md`, in `scripts/swarm/builder-settings.json` only | yes |
| U6 | Push `main` to the `interior` remote after an accepted merge | yes |
| U7 | Jev calls are allowed during the pilot, advisory only | yes |
| U8 | Waive G13 and G18 (mutation) for the pilot. If `no`, building `mutation_gate.py` is a prerequisite that must fit the preparation budget | no |
| U9 | After the pilot, continue with further features (Stage 7). Needs its own budget line from the user | no |

The user authorizes the orchestrator to revise internal time and token budgets, approve further repair attempts, and recover launch, environment and provider failures for this pilot without asking again. There is no fixed overall or stage duration limit. Record each revision and its evidence. This authority does not change U8 or U9, waive acceptance checks, override a user pause, grant denied system permissions, or authorize new purchases, accounts or paid-provider routes. Provider-enforced quotas remain binding.

## Files and who owns them

Each fact has one authoritative file. After bootstrap, `run.json` updates go through one script with the permitted writers below.

| File | Writer | Contents |
|---|---|---|
| `/tmp/factory-fp-0925/swarm/interior-p1/run.json` | Orchestrator during bootstrap; then only through `scripts/swarm/run_state.py` | Authoritative run control |
| `/tmp/factory-fp-0925/swarm/interior-p1/leads/<pid>.json` | `lead-spawn.sh` | Lead identity: PID, PGID, OS session identity, model transcript session id, start time, command, handoff token |
| `/tmp/factory-fp-0925/swarm/interior-p1/orchestrator-state.md` | You only | Your working notes, 60 lines or fewer: stage and step, approvals, prerequisite inventory, alerts and incidents, fixes made (commit SHAs), the exact next action |
| `prompts/phase-1/state.md` | Orchestrator creates it before launch; then the active lead only | Work state, 40 lines or fewer and 6 KB or less: target, acceptance ids, allowed write paths, base SHA, candidate SHAs, running jobs, rejected approaches, open findings and debt, next step. It holds no lifecycle |
| `/tmp/factory-fp-0925/swarm/interior-p1/inbox.md` | You append; the lead appends `ack <id>` lines | Numbered instructions to the lead |
| `/tmp/factory-fp-0925/swarm/interior-p1/questions.md` | The lead appends | Questions for you or the user |
| `/tmp/factory-fp-0925/swarm/interior-p1/PAUSE` | The user or the lead | A pause request, with the user's exact words |
| `ART/<tag>-acceptance.md` and `ART/<tag>-ids.txt` | You, in Stage 1 | The acceptance specification that replaces the contract |
| `ART/<tag>-test-map.json` | The builder | Each acceptance id with its test node, oracle and evidence kind |
| `ART/<tag>-s1-tests.sha256` and the tests commit SHA | The lead | Frozen-test record after red is proved |
| `ART/<tag>-candidate.json` | `prepare_candidate.sh` | Integration candidate SHA, the `main` SHA it was built on, the builder branch SHA |
| `ART/<tag>-gate.json` | `unit_gate.sh` | Pre-review gate results for one candidate |
| `ART/<tag>-code-review.json`, `ART/<tag>-product-review.json` | `p1-pilot-reviewer` | One review per mode |

All `ART` paths are under `/tmp/factory-fp-0925/swarm/interior-p1/`. Keep one stable feature id across repairs, tags and successors. Archive each candidate's gate, reviews, findings and launch receipts before replacing a current file. Count repair cycles and review rounds by feature id, never by filename alone.

**`run.json` fields and their writers.** After bootstrap, `run_state.py` takes an exclusive `fcntl` lock on `run.json.lock`, reads, applies one named operation, writes to a temp file and renames it over `run.json`. It refuses operations outside the selected role (`--as orchestrator|launcher|spawner`). This argument selects an operating role; it does not authenticate the caller. Scripts also verify the active lead's recorded process identity for lead-initiated actions. The lead never edits JSON directly.

| Field | Writer (`--as`) | Operations |
|---|---|---|
| `lifecycle`, `lifecycle_reason`, `blockers`, `ceilings`, `deadlines` | orchestrator | `init`, `adopt-bootstrap`, `set-lifecycle`, `add-blocker`, `close-blocker`, `set-ceilings`; `init` refuses an existing run; adoption validates and preserves its counters and deadlines |
| `inventory` (sessions and threads), `assignments` (model and effort per thread), `reservations` | launcher (`codex-agent.sh`) and orchestrator | `admit` (admission check plus reservation, below), `record-run`, `release`, `assign` (explicit `MODEL` or `EFFORT` with a reason) |
| `active_lead`, `dispatch_frozen`, `handoff` | spawner (`lead-spawn.sh`) | `freeze-lead`, `handoff-start`, `handoff-commit`, `handoff-abort` |

The lead requests work only through the scripts: `codex-agent.sh` asks `run_state.py admit` before launching, and `merge_unit.sh` and `prepare_candidate.sh` read `run.json` and refuse unless allowed. The lead requests a pause by writing `PAUSE`, and asks questions in `questions.md`.

Before `run_state.py` exists, the orchestrator alone records bootstrap state directly and launches no model workers. Implement the script only after reproduction and the prerequisite inventory. Its adoption of bootstrap state preserves the original start time, usage, blockers and deadlines. Do not build a state service or require this script to investigate the first failure.

## Lifecycle, blockers and pauses

`run.json` `lifecycle` is one of these values:
- `orienting`, `preparing`, `running`
- `paused`, `budget_exhausted`, `complete`, `failed`

**Blockers are separate from the lifecycle.** Each blocker records an id, the question, who must answer, and the actions it blocks (for example `["merge"]` or `["launch:builder"]`). An action may proceed only when both hold:
- `lifecycle` is `preparing` or `running`
- no open blocker lists that action

So work a question does not block continues, launches included, as long as the approvals cover it.

**Check before acting.** Read `run.json` before every launch, every wake of the lead, every inbox instruction and every merge. The lead reads it at every dispatch boundary.

**Pauses.** A confirmed user pause ends work immediately. Confirmed means one of:
- the user says pause, stop, hold or wait, or anything with that meaning, to you or the lead
- a `PAUSE` file containing the user's words

The `PAUSE` file wins over `run.json`. Before every dispatch, wake, inbox instruction, candidate build and merge, check that `PAUSE` does not exist. `run_state.py admit`, `prepare_candidate.sh` and `merge_unit.sh` refuse while it exists, whatever `lifecycle` says. `pilot_watch.py` checks for `PAUSE` every 5 seconds and returns at once when it appears (W0).

On a pause:
1. Set `lifecycle: paused` with the user's exact words as the reason, before any other action.
2. Do not write pending inbox instructions. Stop dispatching. Let running workers finish, or stop them under the stop procedure.
3. Resume only when the user explicitly says to. Then archive any `PAUSE` file as `PAUSE.resolved-<UTC time>`, record the resume words, reconcile active jobs and restore the prior nonterminal lifecycle. Preserve usage and elapsed-time history. Reassess internal budgets under the authority above; retain any later explicit hard limit imposed by the user.

**Interruptions are not pauses.** A lead turn can also end on a context limit, an API error (`StopFailure`), a tool failure or recovery. Classify the cause from the transcript's last events and record it as an incident. If the cause is unclear, add a blocker `unresolved_interruption` that blocks new dispatch by that lead until classified. Never invent the user's words. The Stop hook does not fire on API errors, so do not rely on hooks for recovery.

## Rules for the whole run

- **Read small.** Use targeted reads first. Read a complete relevant file when diagnosis or required review needs it; avoid repeated history reads. After compaction, reconcile `run.json`, current notes and live jobs before acting.
- **Leave the user's checkout alone.** Recheck its current status. Never stash, reset, clean or overwrite user changes, including untracked reports and this brief. Do loop edits, repairs, merges and validation runs in the one shared orchestrator workspace `/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer-wt/orchestrator`, switching branches inside it. `orchestrator-spawn.sh` hands it to every successor and, if it is no longer a usable worktree, deletes it and re-adds it from `main`. Never create another worktree or `/tmp` checkout for orchestrator work. Product edits use a separate builder worktree. Read untracked reference documents from the original checkout.
- **Updating `main`.** Run `git merge --ff-only p1-pilot-harness` from the main checkout. If git refuses because a dirty file would change, stop and ask. Never edit or move `prompts/phase-1/progress.md`.
- **Commits.** One commit per fix, with the fix id in the subject. No co-author or agent attribution lines. Never edit `CHANGELOG.md` or generated files. Prose uses periods, commas and plain hyphens, never the long dash.
- **Tests before fixes.** Every script fix gets a regression test under `tests/swarm/` that fails before the fix and passes after. Use fake children (stub `codex` or `claude` executables on `PATH`), never real model runs.
- **Deploy atomically.** Running zsh scripts read their file as they go. Copy each changed script to a temp name in `/tmp/factory-fp-0925/swarm/bin/`, then `mv` it over the old one. Fix F7 makes `interior-lead.sh` do the same.
- **Never:**
  - signal a process whose identity you have not verified as one you started or adopted
  - edit `~/.claude/settings.json` or change any permission mode
  - delete evidence or rewrite git history
  - touch P2, P2.5 or P3 files
  - reword a command to get past a denial

  Ask the user instead.
- **Ask the user only for:**
  - a missing approval
  - a permission or security decision
  - a change to the acceptance target, an explicit user-imposed hard limit, or external spending authority
  - an action outside the paths in U4

  Record the question as a blocker naming the actions it blocks, and tell the user in one paragraph.
- **Models for launches.** Read current slugs from `~/.codex/models_cache.json`. The builder starts on `gpt-6-astra` low. After a model-caused failure it gets one fresh `gpt-6-astra` medium thread; record that as an authorized change with `run_state.py assign --as orchestrator`. Reviews of Codex-written work run on Claude (`p1-pilot-reviewer`).
- **Alerts are hypotheses.** Read enough surrounding evidence to tell a failure from healthy work or a legitimate wait. Close an incident only after its reproduction passes and the lead shows the next useful action.

Register orchestrator inventory with integer `pane_id`, which the existing alert hook reads. The legacy `pane` field alone does not route alerts. If an existing row has only `pane`, `register-inventory` permits adding the matching integer `pane_id` while preserving every other field.

For frozen pilot-lead succession, use direct Write/Edit for the exact frozen snapshot path and the literal `interior-lead.sh --handoff-start TOKEN` command. The guard permits these forms; direct `pilot_lead.py` or Bash snapshot writes are not the supported exception.

## Budgets and the usage formula

**The formula.** "New tokens" use one formula for every provider:
- **Claude:** `input_tokens + cache_creation_input_tokens + output_tokens`, over unique message ids.
- **Codex:** `(input_tokens - cached_input_tokens) + output_tokens`, from `info.total_token_usage`.

Cache reads are excluded from this operational budget and reported alongside it. This is an operating limit, not a billing or quota measure.

**Deltas.** Codex counters are cumulative per thread. A run's usage is its final cumulative value minus the thread's cumulative value when that run started: 0 for a fresh thread, and recorded before a resume begins. Never add two cumulative snapshots of the same thread.

**Inventory.** `run.json` lists every session and thread charged, so totals never depend on a glob:
- your own Codex thread
- each lead's Claude session id, from `/tmp/factory-fp-0925/swarm/fp3d/lead-session-<pid>.txt`
- each `p1-pilot-reviewer` subagent transcript under that session's `subagents/`
- each Codex thread id with its rollout path and starting cumulative value

Totals are computed only over this inventory, between a fixed start time and a fixed end time.

**Orchestrator-managed budgets.** There is no fixed five-hour deadline, stage timeout or non-revisable internal token ceiling. Set overall and stage deadlines to null unless the user later supplies a hard deadline. Keep original start times and full usage for you, leads, reviewers and workers. Initial token planning thresholds are 2,500,000 overall, 2,000,000 preparation and 300,000 pilot, with 150,000 initially planned for the lead, reviewer and workers. These trigger reassessment, not termination. The orchestrator may revise them before further dispatch, recording the old value, new value, evidence of progress and next checkpoint. Do not reset usage or disguise overruns by changing the feature id.

Role subtotals are kept for diagnosis only.

**Admission.** Every worker launch and reviewer dispatch first calls `run_state.py admit` with a stable allocation id and role allowance:
- builder allocation 160,000 new tokens with a handoff at 145,000, shared across the tests turn and implementation resume
- builder repair 160,000
- reviewer dispatch 130,000 with a handoff at 115,000 (RULING575b; the lead calls `admit --role reviewer` before dispatching)

Leads never write code, including swarm tooling, guards and tests. A change with no eligible builder path is a blocker for the user, not lead work. Opus builders use an assignment with `provider` `claude` and model `claude-opus-5-5`; `codex-agent.sh` runs them as sandboxed `claude -p` sessions under the same admission, budget, receipts and accounting as Codex builders.

For each open allocation `i`, `remaining_i = max(0, allowance_i - observed_i)`, where `observed_i` is the sum of its run deltas so far. `committed = consumed + sum(remaining_i)`; `consumed` already includes those deltas, so nothing is counted twice. Admit only if `committed + new allowance` stays within every applicable ceiling. On release, `remaining_i` becomes 0 and the usage stays in `consumed`. Lead starts and rollovers check the same lifecycle, pause and applicable ceilings; their measured usage counts without a fixed worker allowance. Apply the overall ceiling throughout, preparation limits during preparation, and pilot limits during the pilot. Never reset the overall clock between stages.

For the two-turn builder allocation, sum the disjoint run deltas and retain its remaining reservation between turns. Resuming consumes that reservation, not a second allowance. Admission is idempotent by allocation id and refuses concurrent use. Release unused reservations on launch failure. Record and release Claude reviewer allocations on completion too; record preparation test sessions and lead successors in the inventory. Fresh launches obtain a run id before a thread id exists, then attach the observed thread id without a second reservation.

**A run over its allowance.**
- `remaining_i` becomes 0 and the run continues. Record an incident.
- Before 1.5 times its allowance, the orchestrator assesses progress and may extend the allowance. If it remains unextended at that threshold, `codex-agent.sh` prints `BUDGET` and checkpoints the worker through the stop procedure. This requests an orchestrator recovery decision; it does not end the pilot.

**Telemetry lag.** Use the latest available counters and record their age; in-flight usage can be unreported. At 90% of an internal planning threshold, alert the orchestrator to reassess and revise it before admission would block. Reservations constrain admission against the currently recorded allowance. Missing telemetry is unknown, never zero. Report in-flight overshoot.

**At a ceiling:**
1. For an internal threshold, checkpoint the evidence and decide whether to extend the allowance, repair the failing mechanism, or change the implementation approach. Record the decision and continue without requesting user approval.
2. For a later explicit user hard limit, set `lifecycle: budget_exhausted`, stop owned work, preserve evidence and report. Do not override that limit.
3. For an enforced provider limit, follow the recovery procedure below; increasing an internal number cannot create provider capacity.

**Provider quota and failure recovery.** Read the latest available provider usage and error metadata. For Codex weekly usage, select the `codex` limit and its 10080-minute window when available; missing or stale metadata is unknown. The old 70%, 80% and 85% local stop thresholds do not apply. Use the meter to prioritize completion and avoid unnecessary calls.

On an actual quota or rate-limit response, record the provider, error, retry/reset time and affected jobs. Block only affected launches, checkpoint work, and continue independent authorized work. Honor `Retry-After` or the reported reset; otherwise use bounded exponential backoff for transient errors. Do not busy-poll an exhausted provider. Resume automatically when capacity returns, using the existing supported wait or scheduling mechanism. If no mechanism can resume after this process ends, record the exact continuation command and report that limitation rather than claiming background monitoring.

Use another route only when already authorized, available and compatible with the assigned role; record any permitted assignment change. Do not evade limits through account rotation or silently switch to a paid API. Repair local configuration or recoverable authentication failures through supported existing mechanisms. Ask only when credentials, purchase authority or another external decision is genuinely required. Provider exhaustion affecting this orchestrator itself may prevent further action until its runtime can resume.

## Stage 0: orient (`lifecycle: orienting`; only worktree setup, state and backups)

Your overall clock starts now.

1. **Check what is running.** Run `pgrep -lf 'claude|codex'`. For each live row in `/tmp/factory-fp-0925/swarm/interior-p1/logs/builders.tsv` (timestamps are local time, IST), confirm the PID with `ps -o lstart=,command=`. Identify this orchestrator separately. Inspect only relevant phase leads and jobs; U1 does not authorize stopping unrelated user sessions. Reconcile an existing P1 lead before any new start. Adopt a worker only when its target, worktree, assignment and ownership match this pilot; otherwise record the conflict without signalling it.
2. **Check the services.**
   - the headroom proxy on 127.0.0.1:8791 (`codex-agent.sh` routes every Codex call through it)
   - the Claude proxy on 8787, if present
   - WezTerm (`wezterm cli list`)
   - Postgres for the API tests
   - free disk space
3. **Read the Codex weekly meter** and record it.
4. **Record the repo state.** Note `git status --short` and `git log -1` without changing anything. Use the shared orchestrator workspace.
5. **Back up.** Run `rsync -a /tmp/factory-fp-0925/swarm/interior-p1/ ~/factory-backup/interior-p1/`, event logs included. Repeat after Stage 5. At the end, also copy the inventory's Codex rollouts and Claude transcripts into `~/factory-backup/inventory/`.
6. **Read open work.** Read `prompts/phase-1/progress.md` lines matching `EXACT NEXT STEP` and its "Decisions" table only. Note unfinished units and open decisions; the pilot does not resume that plan.

Record bootstrap `run.json` and `orchestrator-state.md`, including the original start time and usage, then set `lifecycle: preparing`. If they already exist, reconcile and resume them without resetting budgets. In Stage 3, implement `run_state.py` with tests for locking, permitted operations, idempotent admission and bootstrap adoption before launching workers.

## Stage 1: reproduce the browser failure

1. **Repair the environment, then measure.** Work in the worktree's `.venv`: `pip install -e '.[dev,api]'`, then `import jwt` must work and `import bpy` must fail. Then start Postgres, the API and the web app, using `prompts/phase-1-build.md` section "Stack" and the `web/package.json` scripts. Record each repair. After 30 minutes on environment problems, reassess the cause, record the failing command and choose a different evidence-backed repair if needed. Continue autonomously; ask only for an external action you cannot perform within authorization.

   Once the environment works, time the existing checks in an isolated checkout of the recorded baseline SHA, using that checkout's environment, and record each duration and result:
   - `pytest -q -p randomly`
   - `ruff check .` and `ruff format --check .`
   - `mypy` over `src`
   - `npm --prefix web run typecheck`
   - `npm --prefix web run e2e -- editor.phase1-flows`

   This is the baseline. Its expected outcome is "existing checks pass", apart from the known `ruff format` failure in `tests/phase2/test_environment.py`.
   Do not leave a manually started preview server on port 4173 when running the configured Playwright suite: it starts its own server with `reuseExistingServer: false`. Verify the test listing includes the selected journey; current projects select `*.phase1-flows.spec.ts`. Collection errors or zero selected tests are not product failures.
2. **Walk the journey.** In a scratch file under the worktree's `.tmp/`, write a one-off Playwright script that walks the Phase 1 journey with a permitted dev floor-plan image (dev families only, never a sealed family): sign in, upload, draft, inspect and edit, save, reopen, history and restore, validate. The acceptance specification is `prompts/phase-1-build.md` lines 13-30.
3. **Record the first failing step** with a screenshot, the console and network logs, and the commit SHA.
4. **Size the target.** Choose the smallest complete behaviour a user can observe that fixes that step. Record smaller implementation steps when useful, and revise the internal allocation when the evidence justifies it. A mocked parser or missing provenance does not count as passing. Ask only if the acceptance target itself needs a user decision.
5. **Write the acceptance specification.** Choose a tag `p1-pilot-<slug>`. Write `ART/<tag>-acceptance.md` and `ART/<tag>-ids.txt`. Each acceptance id names:
   - the observable behaviour
   - its oracle (what the test asserts)
   - the journey spec under `web/e2e/` it belongs to, or the API test path
   - the allowed write paths

   The journey spec is always one of the acceptance checks, even when the change is only in `src/`.

## Stage 2: inventory the missing prerequisites

Before building anything, list every prerequisite the pilot needs and does not have. Include at least:

| Prerequisite | Known state |
|---|---|
| `scripts/swarm/run_state.py`, `unit_gate.sh`, `red_check.py`, `review_check.py`, `diff_lint.py`, `acceptance_check.py`, `prepare_candidate.sh`, `merge_unit.sh`, `pilot_watch.py` | Check current files; implement only the required missing pieces after this inventory |
| `web` lint (G14) | No `lint` script in `web/package.json`; no ESLint installed |
| `web` unit tests (G16) | No `test` script; no Vitest installed |
| `pytest-repeat` (G11), `diff-cover` (G12), `pytest-randomly` (G10) | Check `.venv` |
| `mutation_gate.py`, `mutmut`, Stryker (G13, G18) | Missing; needed only if U8 is `no` |
| `review-schema.json` `p1-review-2` | Old rendering schema (F8) |
| `.claude/agents/p1-pilot-lead.md`, `p1-pilot-reviewer.md` | Missing (F2) |

For each one, record:
- whether this slice needs it, using the gate dispositions below, including affected tests, configuration and dependencies
- the work it needs
- an estimated cost in minutes and new tokens

Add the estimates up. If they exceed the preparation allocation, revise it with a recorded reason or choose a smaller implementation step within the same acceptance target. Record later discoveries as incidents and reassess the allocation before implementing them.

## Stage 3: fix only what this pilot needs

Do these in the worktree, in order, each with a test and its own commit. Skip a fix only if Stages 0 to 2 show it cannot affect this pilot, and record why.

| Id | Defect | Fix | Test |
|---|---|---|---|
| F1 | `interior-lead.sh` overwrites the selected brief and agent | Preserve supplied `REPO`, `LEAD_BRIEF` and `LEAD_AGENT`, and export their resolved defaults before exec. Pass the resolved `FP_CAP_FRACTION`, default 0.90, through every successor. Make `--dry-run` read-only, including no deployment or directory creation | Supplied values and unset defaults survive the full launch path; `--dry-run` changes no files |
| F2 | `.claude/agents/p1-lead.md` requires planners, architects, contracts and separate reviews. `p1-reviewer.md` writes exactly one file in one mode and expects a contract | Add `.claude/agents/p1-pilot-lead.md` (`model: claude-opus-5-5`, `effort: medium`, `tools: Agent(p1-pilot-reviewer), Read, Grep, Glob, Bash, Edit, Write, TaskStop`; its body says to follow `$LEAD_BRIEF` only). Add `.claude/agents/p1-pilot-reviewer.md` (see "Pilot reviewer contract"). Leave `p1-lead.md` and `p1-reviewer.md` unchanged | `--dry-run` shows `--agent p1-pilot-lead` |
| F3 | Existing cap rules do not support pilot ownership and handoff | Apply "Lead hand-over" including its snapshot, acknowledgement and reviewer-output exceptions. Permit the exact `--handoff-begin` and `--handoff-start <token>` calls at the cap; the existing no-argument spawn pattern is insufficient | Tests cover cap reserve, initial start, frozen predecessor, inactive successor, valid acknowledgement and reviewer output |
| F4 | Resume can change model or effort | Resolve both settings from the authorized run assignment, then attach it to the observed thread. Explicit settings must match an authorized assignment or its recorded change; a launcher argument alone does not authorize a change. Pass both `-m` and `model_reasoning_effort`. Record start time, rollout offset, starting usage and assignment in `$dir/$tag-launch.json`. Check only new turn events for `MODEL_DRIFT` and `EFFORT_DRIFT` | Fresh launch, resume, authorized change and earlier historical settings all resolve correctly |
| F5 | Child status, process isolation and repository defaults are unreliable | Use Python `subprocess.Popen(..., start_new_session=True)`, as in `builder-start.sh`; do not assume a macOS `setsid` executable. Record PID, PGID, OS session identity, start time and command in `$dir/$tag-launch.json`. Admit before launching; release on failed launch. Default `REPO` to this repo and honor the builder worktree override. `REPLY` requires child exit 0 and a non-empty reply; all other exits fail. Keep supervising a live child even if its reply file appears early. `STALL_IDLE` emits an alert without abandoning supervision; `STALL_START`, `CONTEXT` and `BUDGET` use the stop procedure | Stubs cover early reply with live child, empty reply, nonzero exit, start timeout, idle recovery, ignored signals and a surviving grandchild |
| F6 | `lead-spawn.sh` either waits 30 minutes for an `--adopt` marker or ends the old lead without proof of hand-off | See "Lead hand-over" | Stub `claude` processes for: a good ack, a wrong snapshot hash, a missing job, no ack, a predecessor that survives retirement, an initial `--start` with no predecessor, and a frozen predecessor trying to dispatch |
| F7 | `interior-lead.sh` copies `scripts/swarm/*` over running scripts | Copy to temp names in `$BIN` and `mv` each over the old file | Overwrite while a stub chain runs; the chain finishes unharmed |
| F8 | `scripts/swarm/review-schema.json` is the old rendering schema | Replace it with the `p1-review-2` schema in `prompts/phase-1/roles.md` (the "Proposed `review-schema.json` v2" block at line 290), verbatim | A valid sample review passes; the old-format sample fails |
| F9 | The cap-guard `until` rule blocks quoted prose | Fix only if the pilot hits it (see "Deferred fix: polling guard") | None until needed |

### Stop procedure

This applies to Codex workers, whose identity is in `$dir/$tag-launch.json`, and leads, whose identity is in `leads/<pid>.json`. Keep the OS session identity separate from the model transcript session id. Verify process inspection on this macOS host. Record descendants while the root is alive so survivors remain attributable after it exits. PGID reuse, a matching cwd, or a later start time alone never establishes ownership. Never signal a whole terminal session or a group that includes the orchestrator, handoff controller, successor or adopted workers.

1. **List the owned processes:** `ps -axo pid=,pgid=,sess=,lstart=,command=`.
2. **Escalate one signal at a time:** `SIGINT`, wait up to 30 seconds; `SIGTERM`, wait up to 15 seconds; `SIGKILL`. Before each signal, verify each target against its recorded identity and ancestry. Signal only verified owned processes still alive. Continue against recorded surviving descendants after parent exit; block if ownership becomes uncertain.
3. **Done** when the root and its recorded owned descendants have exited. A surviving or unaccounted-for descendant blocks replacement and writes to the affected worktree and shared artifacts. Preserve evidence and request the required external action; changing worktrees does not make shared artifacts safe.
4. **Keep the evidence:** the events file, the reply and the final process list.
5. **Record usage:** the final cumulative value and the run's delta, then `run_state.py release`.

An empty list of open files is not proof that no descendant can write later. Rely on the process-group and session check instead. Never start a replacement before step 3 succeeds.

### Stalled workers

`STALL_IDLE` means silence, not failure. Before stopping anything, inspect:
- the rollout's last event, including any `exec_command` still pending and its command
- the process tree under the PGID, with CPU time over the last minute
- the files the run is writing

Classify the run as one of:
- **Healthy long operation.** For example, a pytest or e2e run with rising CPU. Let it continue and check again at its expected duration.
- **Waiting on a legitimate input.** Record it and supply only an authorized input.
- **Suspected hang or unknown.** Silence and low CPU can also mean network or tool waits. Inspect the pending operation and its timeout. Stop only after a confirmed failure, an applicable deadline, or evidence that the operation cannot progress; otherwise schedule another bounded check.

### Lead hand-over

**Initial start.** Refuse if an active lead already exists. The new lead waits read-only until `lead-spawn.sh` records its identity and commits initial ownership. It has no predecessor acknowledgement to write.

**Dispatch rule.** The guard (F3 adds this) allows `builder-start.sh`, `prepare_candidate.sh`, `merge_unit.sh`, `Agent` dispatch and edits to `state.md` only when both hold:
- the originating Claude lead, resolved from the tool event and verified ancestor chain rather than the hook or shell PID, matches `run.json` `active_lead`
- `dispatch_frozen` is false

Also require no `PAUSE`, an allowed lifecycle and no action-specific blocker. Every other lead is read-only except for its exact acknowledgement file. Frozen predecessors may write their handoff snapshot. Permit reviewer output files separately without granting dispatch rights. Put the same ownership check inside launch and merge entry points so direct shell invocation cannot bypass it.

**Rollover, in this order:**
1. The predecessor invokes `interior-lead.sh --handoff-begin`. It freezes dispatch, allocates and records a token, and returns that token without spawning a successor. Drain in-process reviewer work before retiring a lead; its subagents cannot be assumed to survive it.
2. The predecessor writes `ART/handoff/<token>.md`, a frozen snapshot holding:
   - the full contents of `state.md` at that moment
   - the live jobs (PID, PGID, session, tag)
   - the open blockers

   It then invokes `interior-lead.sh --handoff-start <token>`. The script verifies the token and snapshot, records its hash and starts a detached handoff controller outside the predecessor's process group. The controller, successor and adoptable workers must survive predecessor retirement.
3. The successor starts. It is read-only because it is not `active_lead`. It reads the snapshot and writes `$LOG.ack-<token>.json` with the token, the snapshot hash and the jobs it adopted.
4. `lead-spawn.sh --detached` validates the ack against the snapshot and recorded job identities. Jobs that completed since the snapshot require preserved exit and artifact receipts, not a live PID. Recheck pauses and lifecycle before transfer.
5. It retires the predecessor with the stop procedure and confirms that no owned process remains.
6. Only then does it run `handoff-commit`, which sets `active_lead` to the successor and `dispatch_frozen: false`.

**If the ack is missing or wrong after 15 minutes:**
- `lead-spawn.sh` ends the successor with the stop procedure and runs `handoff-abort`.
- The predecessor stays `active_lead` but frozen, with its reserve spent.
- Keep dispatch blocked while the orchestrator diagnoses the handoff failure. Repair the cause and authorize a fresh handoff attempt once ownership is verified. Repeating the unchanged failed handoff is not recovery.

**If retiring the predecessor fails at step 5:**
- Keep `dispatch_frozen: true` and leave `active_lead` unchanged, so no lead dispatches.
- End the successor, add a blocker on all dispatch, and let the orchestrator repair retirement before retrying. Ask the user only if process ownership or required permission cannot be resolved.

## Stage 4: this slice's checks and the pilot files

### Checks and evidence

1. **`scripts/swarm/unit_gate.sh`.** Write it with two modes:
   - **`unit_gate.sh baseline <tag>`** runs the repo-wide checks on the current `main` and expects them to pass (apart from known failures recorded in Stage 1). It writes `ART/<tag>-baseline.json`. Use it for the Stage 4 exit check and for comparisons.
   - **`unit_gate.sh pre <tag> <base> <candidate>`** runs G1 to G18 on one candidate, following `prompts/phase-1/roles.md` section "Gates". It runs every gate even after one fails, and writes `ART/<tag>-gate.json`. That file holds:
     - one record per gate: gate id, disposition, command, exit code, measured value, threshold, duration
     - the candidate SHA, base SHA, checked-out worktree and tracked tree identity
     - an environment fingerprint: Python version, `sha256` of `pip freeze`, Node version, `sha256` of `web/package-lock.json`, OS

     It prints failures only, capped at 100 lines.

   G19 is not part of `unit_gate.sh`. It runs after review, through `review_check.py`.
   Execute checks in a clean isolated checkout of the supplied SHA, using explicit interpreter paths. Refuse unrelated tracked changes. Record and isolate runtime fixtures and servers so another checkout's API or preview cannot supply passing evidence. Record the baseline's known failure as a failure with an explicit disposition, never as a pass. U8 does not waive unrelated checks.

2. **Gate dispositions.** Record this table, with reasons, in `orchestrator-state.md` and in each gate JSON:

   | Gate | Disposition |
   |---|---|
   | G1 frozen tests, G2 red then green, G3 acceptance ids, G4 lint and format, G6 strict types | Retained |
   | G5 complexity, G7 banned constructs, G8 file size | Retained. Write a minimal `diff_lint.py` for the G7 and G8 lists in `roles.md` |
   | G9 Hypothesis profile | Applicable when the acceptance specification requires property-tested invariants; missing `@given` tests then fail rather than make the gate inapplicable |
   | G10 full regression, G12 coverage | Deferred during development; required once at final qualification |
   | G11 flake repeat, Python | Deferred under the October 21 POC policy |
   | G14 web lint, G15 web types, G16 web unit tests | Apply when source, tests, configuration or dependencies affect the relevant check. Record each disposition; changing only configuration must not bypass validation |
   | G17 browser journeys | Reuse the single G2 light-theme functional run; benchmarks run only at final qualification |
   | G13 and G18 mutation | Deferred during development. At final qualification, apply to cumulatively changed production modules in their respective language. With no such modules, record `not_applicable` and the diff evidence. Otherwise retained unless U8 is `yes`; record "waived by user, U8" for a waiver |
   | G19 reviewer JSON | Retained, after review |

3. **`scripts/swarm/red_check.py`** proves G2 in an isolated checkout of the predecessor plus the new tests commit. Use pytest for Python tests and Playwright for journey tests, with the required test configuration present. Verify collection and the expected assertion failure; setup errors, zero tests and timeouts are not red evidence. Preservation tests pass on both revisions. G3 reads the corresponding runner's results and maps exact test cases to acceptance ids, including each required light-theme Playwright project.

4. **`scripts/swarm/review_check.py --gate <gate.json> --candidate <candidate.json> --ids <ids.txt> --open-findings <json> --review <json> [--review <json>]`** (G19). It checks three things separately and returns `PASS` only when all hold.

   **Gate results**, from `gate.json` only:
   - Exactly one disposition exists for every expected G1 to G18 gate. Missing or duplicate gates fail; `not_applicable` requires evidence against the recorded diff and acceptance specification.
   - Every gate with disposition `retained` or `applicable` has exit code 0 and meets its threshold.
   - Every waived gate names its approval.
   - The gate's candidate SHA equals `candidate.json`.
   - The environment fingerprint stored in `gate.json` equals a fingerprint `review_check.py` computes now, in the same checkout, from the same sources (Python version, `sha256` of `pip freeze`, Node version, `sha256` of `web/package-lock.json`, OS).

   The review schema has no fingerprint field and none is added to it.

   **Each review file**, against the unmodified `p1-review-2` schema:
   - Exactly one code review exists, plus exactly one product review when required by the acceptance specification. Missing, duplicate or unexpected modes fail.
   - Schema-valid; `mode` is `code` or `product`; `candidate_sha` equals `candidate.json`; `base_sha` equals its recorded `main` SHA.
   - Code review covers every id in `ids.txt` exactly once with `coverage: asserted` and an independent oracle. Product review covers exactly the UI ids assigned to it in the acceptance specification. Weak, missing, unknown or duplicate ids fail.
   - Code review includes each of the nine named reviewer `checks` exactly once with `result: pass`. Product review uses an empty `checks` array; code review supplies those checks. Do not force a visual reviewer to invent results for code checks.
   - Every earlier open finding assigned to that mode has exactly one status. Blocker and major findings must be `fixed`. Explicitly deferred minor findings may remain open only with their full text and reason in the debt record. Never relabel severity to pass.
   - No finding has severity `blocker` or `major`.
   - Code review covers every survivor id in the mutation packet exactly once; omissions and unknown ids fail. No contract-relevant survivor is a `test_gap`. Product review uses an empty `mutants` array.
   - `needs_ruling` is null and `merge` is true.

   **Round count** comes from archived attempts for the stable feature id. After two failed repair cycles, return the incident to the orchestrator for causal reassessment. It may authorize additional attempts with a changed hypothesis, new evidence and an updated allocation. Do not repeat an unchanged failed action. Review or test-infrastructure errors still consume budget but are classified separately from product findings. Retry counts never waive acceptance or permit a merge.

5. **`scripts/swarm/prepare_candidate.sh <tag> <builder_branch>`** builds the integration candidate before review. It:
   1. records the current `main` SHA
   2. creates a new `int/<tag>/<attempt>` branch in an isolated integration worktree and merges the exact builder SHA with `--no-ff`; never reset an existing branch. The `int/<tag>/<attempt>` branch preserves each earlier attempt until the unit merges; the script removes the superseded attempt's worktree. After a successful push, `merge_unit.sh` removes the candidate worktree, then deletes the builder branch and every `int/<tag>/<attempt>` branch whose content main already holds. It keeps a branch with changes main lacks or one checked out in a live worktree, records it as `worktree_cleanup: pending` and retries on the next run. `red_check.py` and `unit_gate.py baseline` remove their temporary worktrees when they finish
   3. writes `ART/<tag>-candidate.json` (candidate SHA, `main` SHA, builder SHA)

   The gate and the review both use this candidate SHA. Record the integration worktree path, tree hash, expected review modes and attempt number in the candidate receipt. Preserve these files by attempt before rebuilding.

6. **`scripts/swarm/merge_unit.sh <tag>`** reads `candidate.json`. It:
   1. checks that `gate.json` passed for that candidate and that `review_check.py` returns `PASS` for it
   2. serializes pilot integration updates with a lock, rechecks ownership, pause, lifecycle, budget and the exact recorded `main` SHA immediately before mutation; refuses if the main checkout is on another branch
   3. fast-forwards `main` to the exact candidate while preserving unrelated dirty files, then verifies `main` equals that candidate; any concurrent ref change invalidates acceptance
   4. pushes that exact commit to the remote's `main` with no force (U6), records the push result and prints the merge SHA

   If `main` has moved:
   - mark the gate and review evidence for that candidate invalid
   - rebuild the candidate with `prepare_candidate.sh`
   - re-run the gate and the review on the new candidate SHA

   An unchanged patch on a different base can behave differently, so a review is never reused across candidates and no review's SHA is ever edited.
   If local integration succeeds but push fails, record `merged_local` and the exact commit. Diagnose or retry only the authorized push; do not rebuild, merge again, or claim remote success. Preserve this state across interruptions.

   If git refuses the fast-forward because of a dirty file, stop and ask. Both scripts read `REPO` and `REMOTE` from the environment, defaulting to this repo and `interior`.

   **Testing the scripts.** Test both scripts end to end only in a disposable repository: `git init` under `/tmp/factory-fp-0925/swarm/interior-p1/merge-test/`, with a local bare repository added as remote `interior`. Run them with `REPO` and `REMOTE` pointing there; they must never touch the real checkout or remote.

   **Testing permissions.** Add the exact allow rules from U5. Then start one short lead session with a two-line test brief that runs only `prepare_candidate.sh --check <tag>` and `merge_unit.sh --check <tag>`. `--check` validates arguments, files and `run.json`, then exits 0 without changing any git state. Record whether the auto-mode classifier denied either call. If it did, ask the user, and do not reword the command.
   Charge that session to preparation, then confirm its exit and clear its temporary ownership through the spawner before the real pilot starts. Use disposable fixture paths for its checks. A successful `--check` does not prove the classifier will allow a later mutating call; record any later denial as its own incident.

### Pilot reviewer contract

`.claude/agents/p1-pilot-reviewer.md`:
- **Model and tools:** `model: claude-opus-5-5`, `effort: medium`, `tools: Read, Grep, Glob, Bash, Write`, `maxTurns: 40`.
- **Inputs:** one packet under `ART`, mapped to the inherited template in `prompts/phase-1/roles.md` section "2.3 Reviewer" (lines 230-242):

  | Template item | Pilot file |
  |---|---|
  | `<tag>-contract.md` | `<tag>-acceptance.md`; `contract_ref` cites its line numbers |
  | `<tag>-s<k>-tester-result.md` acceptance map | `<tag>-test-map.json`, with its SHA-256 |
  | `<tag>-diff.patch` | `git diff <main SHA> <candidate SHA>` from `candidate.json` |
  | `<tag>-gate.json` | `<tag>-gate.json` for the same candidate |
  | `<tag>-survivors.json` | `<tag>-survivors.json` from G13 and G18. If U8 waived them, the file holds `{"waived": "U8", "survivors": []}` and the reviewer returns an empty `mutants` array |
  | Open findings with full text | `<tag>-open-findings.json`: unresolved findings for the stable feature id, with mode, severity and any explicit minor deferral, written by the lead (`[]` on round 1) |
  | Product-mode drawing, overlay, saved IR, issues, screenshots | Include each artifact required to assess the selected UI ids. A screenshot is not a substitute for a required drawing or overlay. Missing required evidence blocks review |

  Also included: `ids.txt` and the frozen-test record.
- **Outputs:** in one dispatch, write `ART/<tag>-code-review.json` (mode `code`) and, when the slice changes the UI, `ART/<tag>-product-review.json` (mode `product`). Each file has exactly one mode and is valid against `p1-review-2` on its own.
- **Rules:**
  - Judge only the packet.
  - Bash is for `git diff`, `git show`, `pytest`, `npm` and `shasum`.
  - Apply the substantive review criteria in `prompts/phase-1/roles.md` section "2.3 Reviewer". This pilot contract overrides its old role, filename and mode-specific output instructions. Code review owns all acceptance ids, code checks and survivor dispositions; product review owns its assigned UI ids and visual findings.
  - Any required acceptance id the packet does not let you assess gets `coverage: missing`, `evidence` naming the missing packet item, that item named in `needs_ruling`, and `merge: false`. Reviewer checks you cannot assess get `result: not_assessed`. Both make `review_check.py` fail.
  - Reply with the paths only.

### Pilot files

7. **`prompts/phase-1/state.md`.** The orchestrator creates the initial file before any lead starts, with target, acceptance ids, allowed write paths, base SHA, running jobs, rejected approaches, open findings and debt, next step, and references to authoritative blockers in `run.json`. Then ownership passes to the active lead. Operational updates need not be committed with product code.

8. **`prompts/phase-1/pilot-lead-brief.md`**, 60 lines or fewer. It overrides `prompts/phase-1-build.md`, `swarm-ops.md` and `p1-lead.md` wherever they differ, and tells the lead:
   - **At start:** read `state.md`, `run.json`, the inbox and this brief. Then read additional evidence as needed. Stay read-only until your verified identity is `active_lead` and dispatch is unfrozen; rollover acknowledgements remain allowed. No transfer marker file is used.
   - **At every dispatch boundary:** check `PAUSE` (if it exists, stop), read `run.json`, read `inbox.md`, carry out pending instructions in order, append `ack <id>` for each, then act. Act only when `lifecycle` is `running` and no blocker lists the action.
   - **The builder:** one builder for the slice, in one fresh Codex thread, with `FEATURE`, `ROLE`, the authorized assignment, allocation id and `REPO` pointing to its isolated worktree. Each execution has a unique launch tag so resume cannot overwrite its predecessor's events.
     1. Its first reply writes the failing tests and `ART/<tag>-test-map.json`, then stops.
     2. The lead runs `red_check.py`, freezes the tests (`ART/<tag>-s1-tests.sha256` and the tests commit SHA), and resumes the same thread with "implement" under the assigned model and effort and the remaining initial builder allocation. A needed test correction is recorded and reproved red; the frozen record is revised explicitly, never silently bypassed.
     3. Repairs after review start a fresh thread with the findings and the diff.
   - **Checks and merge:**
     1. `prepare_candidate.sh`, then `unit_gate.sh pre` on the candidate. Repair until it passes; after two failed cycles, obtain an orchestrator recovery decision before another attempt.
     2. Write `<tag>-open-findings.json` and the diff, reserve the reviewer allocation through `run_state.py admit --as launcher --role reviewer`, then dispatch `p1-pilot-reviewer` with the full packet. Record its session and usage, and release the allocation on completion or launch failure. A rebuilt candidate always gets a new review inside the remaining feature budget.
     3. Run `review_check.py`, then `merge_unit.sh`, then update `state.md`.
   - **Not used:** no planner, architect, contract, plan review, separate tests review, or LLM runner.
   - **Limits:**
     - After two failed repair cycles across all roles and successors, write the evidence to `questions.md` and hold that feature's dispatch until the orchestrator approves the next recovery attempt. This does not require user approval.
     - Questions go to `questions.md`; continue only work they do not block.
     - A user pause in any wording means: write the user's exact words to `PAUSE` and stop.
   - **Jev:** advisory only. The explicitly authorized POC integration also permits eligible same-priority ranking and evidence-review triggers under /tmp/p1-jev-research-20261009.txt. Customer order, dependency and ownership filtering stay in code. Missing or stale deterministic evidence blocks progression; uncertain or unavailable Jev results preserve full review. It never drops a clause from the review packet, skips a check or decides a merge or a rollover.

9. **`scripts/swarm/pilot_watch.py --run <run.json> --timeout SECONDS`.** It blocks until one of these fires, or the timeout passes, and prints each alert as one JSON line (check id, evidence path and line range, hypothesis):

   | Check | Fires when |
   |---|---|
   | W0 | `PAUSE` exists (checked every 5 seconds) |
   | W1 | Consumed usage or time reaches 90% of an applicable ceiling, or a deadline passes; include reservations in the evidence but do not treat them as consumed usage |
   | W2 | A `codex-agent.sh` run printed `FAIL`, `DIED`, `STALL_*`, `CONTEXT`, `BUDGET`, `MODEL_DRIFT` or `EFFORT_DRIFT` |
   | W3 | `lifecycle` is `running`, no worker is running, `state.md` has a dispatchable next step, and the lead has written nothing for 10 minutes |
   | W4 | The lead ran the same normalized command 3 times in 30 minutes, or the same test node failed in two gate runs with no diff change |
   | W5 | 2 or more tool results in 10 minutes from the auto-mode classifier, `fp-guard.py`, or `PermissionError`/`EPERM` |
   | W6 | A worker reply has waited more than 60 seconds for the lead, or an inbox instruction has had no `ack` for 2 dispatch boundaries |

   Add more checks only when the pilot shows a need.
   Track an event cursor and emit each unchanged incident once. Acknowledgement of an alert does not erase its stop condition. Continue checking pauses, deadlines and new events while an incident remains open. Persist cursors across watcher calls so an old `FAIL` cannot create an immediate retry loop. In-process reviewer work and running gate commands count as active work for W3.

### Exit

- Code, schemas and briefs are committed, merged with `--ff-only` and deployed. Runtime state, logs and generated evidence stay outside product commits.
- `unit_gate.sh baseline` passes on `main`, with its duration recorded next to the Stage 1 baseline.
- `interior-lead.sh --dry-run` prints the pilot brief, `--agent p1-pilot-lead` and `FP_CAP_FRACTION=0.50` (166000 x 0.50 = 83K).
- The throwaway prepare-and-merge test has run.

Then record the pilot start time once and set `lifecycle: running`. Keep its deadline null unless the user supplied one. Preparation accounting ends here; overall accounting continues without reset.

## Stage 5: run the pilot

1. **Start the lead.**

   ```
   LEAD_BRIEF=/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer/prompts/phase-1/pilot-lead-brief.md LEAD_AGENT=p1-pilot-lead FP_CAP_FRACTION=0.50 /tmp/factory-fp-0925/swarm/bin/interior-lead.sh --start
   ```

   Have the spawner record ownership and identity through its assigned operations; register the transcript in the inventory. Do not edit `run.json` directly.

2. **Watch.** Loop on `pilot_watch.py --run /tmp/factory-fp-0925/swarm/interior-p1/run.json --timeout 1200`. For each alert:
   1. Re-read `run.json`.
   2. Treat the alert as a hypothesis. Read the evidence range and enough of the surrounding transcript, rollout or log to tell failure from healthy work or a legitimate wait.
   3. Act, then confirm the incident is closed: its reproduction passes, and the lead has acknowledged any inbox instruction and taken the next useful action.

   | Alert | Diagnose | Action if confirmed |
   |---|---|---|
   | W0 | Read the user's words in `PAUSE` | Set `lifecycle: paused` before anything else, write no inbox instruction, then follow "Pauses" |
   | W1 | Check counters and reservations | Reassess internal allowances and record an extension or recovery action. Honor any explicit user hard limit through the "At a ceiling" procedure |
   | W2 | Read the run's last events and reply; for a stall, follow "Stalled workers" | Classify as model, environment, brief or harness. Authorize a fresh relaunch only after addressing the cause or identifying a changed precondition, and count it as a repair cycle. For a launcher cause, fix it under the Stage 3 rules |
   | W3 | Check whether the lead's last turn ended in a question, an error, an interruption or a pause | Pause: honour it. Question: answer only from approved facts in the inbox, or record a blocker and ask the user. Interruption: classify it (see "Interruptions are not pauses"). Otherwise, write the next step into the inbox and wake the lead with one `wezterm cli send-text` line |
   | W4 | Decide whether the repetition is legitimate (for example, a planned rerun after a fix) | If not, reproduce the command yourself, fix the environment, fixture or brief, and tell the lead to rerun once |
   | W5 | Record the exact denied command and reason once | Record a blocker and ask the user. Never reword the command |
   | W6 | Check whether the lead was mid-turn | Record the delay. After 3 occurrences, note it as an unfixed harness issue |

   Record every incident in `orchestrator-state.md` with its cost in tokens or minutes, the action taken and how it was confirmed closed.

3. **Stop conditions.**
   - **Success:** `merge_unit.sh` merges the candidate, and your Stage 1 Playwright journey passes the fixed step on the merge SHA. Set `lifecycle: complete`.
   - **Internal budget:** reassess and extend or repair under the budget procedure; do not end the run merely because an internal threshold was reached. A later explicit user hard limit still sets `budget_exhausted`.
   - **Repairs:** after two failed cycles, the orchestrator diagnoses and approves the next evidence-backed recovery attempt. Set `failed` only when no authorized recovery remains, and report the concrete blocker.
   - **Quality:** a required acceptance check is lost, or a security or accuracy defect is found after merge. Block further merges, restore the check or repair the defect, and verify recovery before continuing. Never waive the requirement to finish.

   On a terminal outcome, stop owned workers and leads with the stop procedure, retain the handoff controller until its cleanup completes, and back up again. During recovery, stop only affected work and preserve healthy jobs. A terminal run must not leave a lead dispatching.

## Stage 6: measure and report

Compute totals with a fixed start and end time over the inventory only, using the formula and per-run deltas. Report these separately:
- the pilot total by provider and role (lead, reviewer, builder)
- your orchestration total
- the preparation total
- the overall total

Report wall time as overlapping intervals, not an additive split:
- model active
- tool execution
- waiting on workers
- permission waits
- user waits
- machine sleep
- notification pickup delay

Label any unexplained interval "unknown".

Write `reports/P1 loop pilot results.md`, 1,500 words or fewer, plain prose, no long dashes. It covers:
- the target and outcome, with the merge SHA and the gate and review evidence paths
- tokens per accepted behaviour (new tokens, with cache reads alongside), launches, repair cycles, review rounds, escaped defects
- time to the first reproducible end-to-end result
- the prerequisite inventory with estimated against actual cost
- the gate disposition table, and any waiver with its approval
- every fix with its commit and test
- every incident with its cost, how it was diagnosed and how it was confirmed closed
- harness problems left unfixed, with evidence
- open questions
- recommendations, each tied to something the pilot measured

Two earlier units are context, not baselines:
- m4-auth-schema took 18 Codex launches and 34.5M Codex input
- p0-ir took one launch under a review waiver

Their scope and rules differed from the pilot's, so do not report a saving against them.

Reply to the user in 10 lines or fewer: outcome, tokens per accepted behaviour, open questions.

## Stage 7: further work (only with U9 and a user budget line)

Do not start this stage without U9 `yes` and a written budget. Candidates, each applied alone and measured on a comparable slice:
- make the pilot loop the standing Phase 1 procedure (`prompts/phase-1/swarm-ops.md`, `.claude/agents/p1-lead.md`)
- move reviewers to Sonnet
- a per-phase Codex ceiling for the planning chain (`planner-spawn.sh`)
- Jev routing for the coder model

Jev may rank or highlight review clauses, but never drops a required clause from a packet, and never decides a merge, a skip of required review, or a rollover. A Jev hand-off check, if added, is a warning only.

## Deferred fix: polling guard (F9)

Make this fix only if the pilot hits a false block. In `fp-guard.py`, tokenize the command with a shell-aware parser. Strip only arguments that are literal prose:
- the value of `git commit -m`/`--message`/`-F -`
- `gh ... --body`/`--title`
- `echo`/`printf`/`print` arguments

Keep checking:
- `bash -c`, `sh -c`, `zsh -c` and `eval` bodies
- `$(...)` and backtick substitutions
- heredocs fed to a shell interpreter

Regression cases, all required:

| Must pass | Must still block |
|---|---|
| `git commit -m "wait until merged"` | `until false; do x; done` |
| `printf 'sleep is fine\n'` | `bash -c 'until x; do y; done'` |
| a heredoc commit message containing `until` | `zsh -c "sleep 5"` |
| an escaped `\until` inside a literal | `$(sleep 5)` and a backtick `sleep 5` |
| | `while true; do x; done` |
| | `tail -f log` |
| | a heredoc piped to `bash` containing `sleep` |

## Stop rule for the whole run

End the run when the accepted pilot is complete, the user explicitly stops it, a later user-imposed hard limit is reached, or no authorized recovery can proceed without external action. Otherwise continue diagnosis, repair and verification.

Internal budget thresholds, a high weekly meter and repeated failures trigger orchestrator decisions, not automatic abandonment. If `main` turns red, block new merges and repair it until the required checks pass. Provider-enforced limits require waiting or an already authorized alternative. Preserve the current state and an exact continuation action whenever execution must yield.
