# Orchestrator and lead loop lessons

Recorded on 2026-10-11 from the FloorInteriorDesignAndRenderer Phase 1 POC run (orchestrator generations 480 to 488) and its earlier audits. Read this before you start or succeed an orchestrator or lead. Each entry names what went wrong, the evidence, and the rule that avoids it.

## Succession and handshakes

**Do not send keystrokes to a pane whose agent is mid-turn.** Successor 488 sent the literal `ACK484` plus a carriage return to pane 484 while the Codex predecessor was still in a turn. The text landed on Codex's "Stop this attempt and retry? 1. Keep waiting / 2. Stop and retry" stall dialog, and the predecessor transcript never recorded the ACK as a user message. Before sending, check the predecessor transcript. Its last `task_started` must have a later `task_complete`. A predecessor that launches its successor should end its own turn right after the launch.

**Plan retirement as a self-exit, not a cross-agent kill.** The Claude Code auto-mode classifier denied a Claude orchestrator's `kill -TERM` on its Codex predecessor ("Interfere With Workloads"). It denied the same kill again after the user authorized it in chat. It also denied writing the permission rule to `.claude/settings.local.json` ("Self-Modification"). The predecessor only exited when the user closed its tab. The predecessor should write its relinquishment file and then exit by itself (`/quit` or end of the process). If a kill is part of the design, put the permission rule in place before launch.

**Wait loops are blocked, so handshakes must not need them.** `fp-guard.py` rejects `sleep` and polling loops. A successor cannot wait for a receipt inside one turn. Use completion events: the next user or pane message re-invokes the agent, and the agent then checks once.

**Relinquishment needs a file, not only a pane message.** The handshake only became verifiable once the predecessor wrote `logs/orchestrator-484-explicit-relinquishment.json` with both PIDs, panes, session IDs and `dispatch_relinquished: true`. Make that file the first step of every succession.

**Enforce the context cap where the tokens are counted.** Orchestrator 484 reached 264,417 new tokens over 131 response IDs, against a 100,000 prepare threshold and a 125,000 stop threshold. The breach was found only after the user reported it. The guard that counts usage must trigger succession at 100,000 tokens.

**Write the handoff file before the cap guard locks the session.** Orchestrator 488 passed 125,000 tokens before it wrote a handoff file. The cap guard then blocked its spawn of a successor, its ledger writes and its `questions.md` writes. Its handoff survived only as chat messages, and the user had to start successor 489 by hand. The user started it twice: a second session, `b49d606b`, began 11 seconds after 489 and exited after 13 seconds. Write the handoff file and launch the successor at the 100,000 prepare threshold, while the guard still permits both. A successor must check for a live duplicate before it registers.

**Lifecycle tools must accept a lead that has already exited.** After the user closed every lead pane, `run.json` still named lead 773d0639 as active, and `interior-lead.sh --start` refused with "An active lead or handoff already exists". `--stop` refused too, because it required the lead process to be alive. The fix in FloorInteriorDesignAndRenderer `scripts/swarm/pilot_lead.py` lets `stop_active` retire a lead whose PID is gone (or a zombie with the same identity) and skip the signal step. `--drain-retire` has the same gap. It needs a completion report that only a live draining lead can write, so leads 4694205f and 03636def stayed in `draining_leads` after the user closed them. Every retirement path needs a branch for an exited owner and a test that covers it.

**Write permission rules that match the command shapes the tools run.** Orchestrator 489 needed three classifier denials and two `/permissions` approvals before it could start a lead. `/permissions` approved the action, and the next retry was still denied. What worked were `.claude/settings.local.json` allow rules written for the exact command prefixes, such as `PILOT_RUN=... LEAD_AGENT=p1-pilot-lead ... /tmp/factory-fp-0925/swarm/bin/interior-lead.sh *`, plus `wezterm cli send-text *` and `wezterm cli spawn *`. Run each step as its own command, so that every command matches a rule.

## Ledger and accounting

**Always pass `--run`.** `scripts/swarm/run_state.py` defaults to `DEFAULT_RUN = /tmp/factory-fp-0925/swarm/interior-p1/run.json`, an older run. The handoff called the live ledger `ART/run.json`, but the POC ledger is `<run dir>/run.json`. A call without `--run` writes to the wrong ledger.

**Codex final accounting formula.** Deduplicate `token_usage_record` rows by `payload.response_id`. Then take new tokens = `input_tokens - cached_input_tokens + output_tokens` and cache reads = `cached_input_tokens`. This formula reproduced the recorded 480 receipt exactly (97 response IDs, 140,729 new tokens). Record it through `run_state.py record-run` with `complete: true` and `usage_status: known`, and only after the process has exited and no process holds the transcript open.

**Register a Claude orchestrator as a routing alias.** Orchestrator routing reads `inventory.threads`, so register the Claude session UUID as both `thread_id` and `session_id`, with `identity_kind: claude_session` and `provider: claude-headroom`. Also record the PID, pane, transcript and runtime (model, effort, permission mode, route).

## Guards

**The concurrent-drain guard blocked every real builder launch.** While a predecessor lead drained, `pilot_guard.py` accepted only bare commands under `/tmp/factory-fp-0925/swarm/bin`. Real launches start with environment fields (`REPO=... PILOT_RUN=... FEATURE=... ROLE=... ASSIGNMENT_ID=... ALLOCATION_ID=... ALLOWANCE=... KEEP_ALLOCATION=... codex-agent.sh`). The guard also blocked `.venv/bin/python` running `run_state.py` or `jev_triage.py`, and read-only `jq`. The guard denied launch 35578 twice, and builder slots sat idle. Any new guard branch needs tests built from the exact commands the lead runs, copied from a real transcript.

**Never send a retry pointer before the fix is installed.** The lead retried 35578 after a pointer that cited a ruling (484g) for which no repaired guard hash existed, and the guard denied it again. A retry pointer must cite the installed file hash, and you must verify that hash on disk first.

**Red tests must fail at the line under test.** The tests-first cases for the drain fix failed red for the right reason. After the parser fix, three of them still failed, now at the later ancestry check ("Caller is not a descendant of the active lead"), because the fixture never set up real lead ancestry. Stubbing that check in a security test is also denied by the auto-mode classifier ("Security Test Removal"). Build fixtures that pass every unrelated precondition genuinely, then confirm that the red failure points at the code being fixed.

## Writing handoffs

**Write handoffs in whole sentences.** The 484 handoff fused numbers to words ("ONLYPID22144", "dispatchlead50142/pane487", "NEW484264417breach"), and the successor spent turns decoding them. Write "PID 22144", "lead 50142 in pane 487". Put live identities in a JSON receipt, and the reasoning in prose.

**Keep one current-state record.** Every successor re-read a 600 KB `questions.md` of layered rulings. Keep the active identities, open blockers and next action in one short state file, and link the history instead of carrying it.

## Spend findings from the earlier audits

These come from `reports/Agent loop token waste revalidation.md` in FloorInteriorDesignAndRenderer, which covers 2026-10-07T07:00Z to 2026-10-08T13:36Z.

- The factory used 825.2M tokens and landed 2,227 product lines on main, or 3.95 lines per million tokens. Codex coder and fixer runs used only 6% of factory tokens.
- Opus leads re-read a context that grew to 149K tokens on every call, for 327.9M Claude tokens across 37 leads. Succeed early and keep the startup reads short.
- Leads sat blocked on the user for 20.7 hours, including a 5.8 hour overnight stall, while other units could have been dispatched. Record the question and keep dispatching independent work.
- LLM runners wrapped around a 61 second pytest used 18.3M Codex tokens. Run fixed checks directly.
- Resuming a Codex thread at 127K to 170K context for a small repair cost about 1M tokens each. Start fresh at 17K to 35K.
- Auto-mode classifier denials cost 21.7 minutes and about 624K tokens of retries in one P1 lead. Settle permission rules before launch, not mid-run.
- The `lead-spawn.sh` adoption timeout left two P2 leads running in parallel for 6.5 hours. Retire the predecessor before the successor dispatches.

## Changes from orchestrators 490 to 570 (2026-10-11 to 2026-10-13)

These are the tooling and process changes the same run made after the entries above. The rulings cited live in `questions.md` of the run directory; the command templates live in `logs/orchestrator-templates.md`.

**Merge into a pre-main branch and gate there.** Reviewed units merge with `git merge --no-ff` into `pre-main`. The gates, including G17, run on `pre-main`, and `main` fast-forwards only to a `pre-main` sha that passed them (RULING567b). Leads never wait on a gate, review or merge (RULING567e). Until a pre-main gate path exists, `main` cannot advance, and any tool installed from `main` keeps its old behavior. One reviewer admission fix sat on `pre-main` while the launcher kept running the `main` copy (RULING569d).

**Refill every builder slot at every result.** At each completion the lead fills all builder slots from eligible disjoint scopes before anything else. The user raised the count from two to six and then to eight. When a drain blocks on a merge, move the unblocking unit to the front of the merge queue instead of letting the drain wait (RULING550a).

**Concurrent handoff carries running jobs.** `pilot_lead.py --handoff-start` in concurrent mode records running Codex jobs as `kept_jobs`, so a lead never waits for a job or gate to exit before handing off. Fix the refusal cause and rerun at once (RULING563d). The exception is an in-process Claude reviewer subagent: it has no `-launch.json` receipt, cannot be carried, and dies with the lead, so the handoff must start right after that review returns (RULING570d).

**Caps were raised and split by role.** The orchestrator hands off at 160,000 context tokens (RULING567h), the lead cap is 180,000, builders get 160,000 with handoff at 145,000, and reviewers get 120,000 with handoff at 105,000. A builder allowance is never lowered to save tokens. This replaces the 100,000 threshold above. Measure an orchestrator's context from the last assistant usage in its transcript (input plus cache read plus cache creation).

**Launch with the minimum tool surface.** Lead and orchestrator panes start with `--disable-slash-commands`. `codex_launch.py` disables bundled skills, plugins, `find-skills` and `typesafe-ai`, which cut Codex prompt input from 24,930 to 20,117 characters (RULING559f). An orchestrator started with the template flags has about 11,500 tokens of fixed startup context, against about 23,300 without them.

**A new permission rule needs a new session.** Allow rules added to the orchestrator settings file load only at session start. When the user adds rules, hand off to a fresh orchestrator at once (RULING569c).

**Run gates detached.** A gate started as a Bash background task dies with its pane and keeps old orchestrator panes open. Start it with `nohup ... & disown`, record the pid in the handoff, and let the successor check it with `kill -0`.

**Pass full shas to `unit_gate.py pre`.** The gate compares its arguments as strings with `candidate_sha` and `main_sha` in `ART/<tag>-candidate.json`. Short shas fail with "Candidate arguments differ from receipt" (RULING570a).

**Deliver a drain notification before closing its target.** `--drain-retire` requires the exact `DRAIN_COMPLETE` message as a user message in every target transcript named in the completion record. Two leads, d09c2fc2 and e70f5787, could not be retired because one target was an orchestrator whose pane had already closed (BLOCKER567n, BLOCKER570b). Deliver and verify the notification before the predecessor orchestrator closes, or give the tool a branch for a closed target.

**A kept allocation cannot move to a new lead.** Move the worktree with `run_state.py release-drain-scope` and register a fresh allocation instead (RULING567p).

**s1 accepts tests-only commits.** When a builder's red commit also touches production code, build a tests-only commit with `git commit-tree` on the same parent and write s1 with `evidence_write.py ... --correction` (RULING568f).

**Never delete run files.** A cleanup at 2026-10-13 00:00 IST removed interior-p2 artifacts and worktrees. No snapshot or git history held them, so they were lost (RULING557h). No file of any phase may be deleted; archive under a new name instead.

**Small traps.** The Bash tool runs zsh, which does not word-split `$x`, so wrap id loops in `bash -c`. A Claude session id can change on the same pane (567 went from 1c9d9f40 to 68d7cae1); register the new id at once. To submit text to a pane, clear the composer with `$'\x05\x15\x15'`, send the text, send a separate `$'\r'`, and send a second `$'\r'` after 3 seconds.

## Later lessons from orchestrator 570 (2026-10-13)

**Record an in-process reviewer by session id.** `run_state.py record-run` with a `thread_id` creates a threads row and leaves the reviewer's sessions row incomplete, so `--drain-ready` refuses with "Drain in-process reviewer work before handoff". Pass the reviewer's `session_id` (`<lead session>/<agent id>`). A repeat record-run with the same counters adds a zero delta. For Claude usage, deduplicate assistant messages by message id and take new tokens = input + cache creation + output.

**A brief cannot stop an in-process reviewer.** Three reviewers in one morning overran the 120,000 allowance (134,985 to 170,237) although their briefs said to stop at 105,000. Enforcement belongs in the guard. Until then, cap reviewers by turn count and split large packets.

**A lead acts only on pane pointers.** Rulings written to `questions.md` without a pointer left a lead idle while zero builders ran. When a pane holds another sender's unsubmitted draft, the auto-mode classifier denies the orchestrator's carriage return ("Auto-Mode Bypass"), so the draft blocks both the drain notification and all pointers until a human presses Enter.

**One owner per gate.** A lead and the orchestrator each started the same unit gate 9 seconds apart, and both wrote the same artifacts. Only the orchestrator starts unit gates, detached.

**Retries must be idempotent.** An aborted `prepare_candidate` left a partial attempt archive, and the retry refused because the evidence had changed since. Move the partial archive aside under a new name, never delete it, and make the archive step do that itself.

## Repair-cycle speed changes from orchestrator 573 (2026-10-13)

**Builders never stop to ask (RULING573j).** Four of seven builders that finished in one session stopped with a REPLY on a scope or frozen-test question. Each stop cost a full lead, orchestrator, brief, registration and launch round trip. Builders now log each conflict under "Open questions" in the handoff with evidence and a proposed answer, skip only the blocked item and keep working. Leads check every brief against the frozen tests, allowed paths and acceptance before dispatch, and answer open questions in the next brief. Only three early stops remain: a preflight mismatch before any edit, the handoff cutoff, or a guard denial after two retries. Do not rely on leads copying the rule; the launcher should append it to every builder prompt.

**Capacity errors look like failures (RULING573i).** Three builders ended with exit 1 below their cutoff and no stop reason. Each rollout's final task_complete carried "Selected model is at capacity" (server_overloaded). The launcher must copy task_complete.error into the receipt's stop_reason, and must not count an overload as a work failure. Relaunch from the handoff on the same model and retry once after 5 minutes; record a second overload as a blocker.

**Script the task environment (RULING573l item 2).** One builder spent its whole 160,000 allowance largely on starting the API, a wrong JWKS port (the default 127.0.0.1:8765 collides across parallel builders) and three hand-made databases. Planned design: one script brings up the API, IdP and web preview with an isolated database. Ports come from a locked registry with bind checks, and the script prints one JSON line. The environment lives until the task ends, not the turn: continuations reuse it after a health check. Teardown runs when the feature's last allocation is released and touches only its own pids and database. It uses pidfiles, never ps, because the builder sandbox blocks ps.

**Repair turns rerun only what failed (RULING573l item 3).** Rerun the failing node plus the test-map nodes covering the touched files. Coverage, mutation, the full browser matrix and benchmarks run once, in the gate. The gate must actually work on the integration branch, or the full run never happens.

**Stack dependent units instead of waiting (RULING573l item 4).** A unit whose fix needs unmerged units starts on a stack branch (git merge --no-ff of their heads), and its reviewer gets the stack base sha. Candidate tooling must accept a base other than main, record the review base and invalidate the receipt when that base moves. Expect conflicts each time a base unit moves. Resolve them in the stacked unit's first step and never abort.

**Leads register their own continuations (RULING573k).** Registration was orchestrator-only and cost 5 to 10 minutes per continuation. Same-unit continuations (same feature, worktree, model, effort and allowance, recorded predecessor accounting, fresh thread) move to the lead. This goes through a narrow run-state role and a guard allowance for that one operation, with dedupe and admission ceilings still enforced. A lead must never borrow the orchestrator role to get there.
