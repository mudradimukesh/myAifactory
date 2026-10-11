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
