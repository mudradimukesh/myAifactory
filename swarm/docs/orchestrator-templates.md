# Orchestrator command templates

Run from the repo root with bash. Set R=/private/tmp/factory-fp-0925/swarm/interior-p1-poc, L=$R/logs and N=<your orchestrator number>.

Register yourself (check only exit codes):
  python3 scripts/swarm/run_state.py --run $R/run.json --as orchestrator --data '{"thread_id":S,"session_id":S,"identity_kind":"claude_session","provider":"claude-headroom","role":"orchestrator","pane_id":PANE,"pid":PID}' register-inventory >$L/orch-$N-register.log 2>&1
  ... --data '{"run_id":"orchestrator-S","thread_id":S,"role":"orchestrator","new_tokens":0,"cache_reads":0,"transcript_path":T}' record-run
  wezterm cli set-tab-title --pane-id $WEZTERM_PANE "P1 POC orchestrator N"

Drain a builder TAG with pid P:
  Load $R/TAG-launch.json and assert child_exit==0, an empty remaining_processes and pid==P. Take the last token_count info.total_token_usage in rollout_path: new = input - cached_input + output. Assert that usage_delta matches. The thread id is rollout_path[-42:-6]. Confirm with ps -p P that the process is gone.
  ... --data '{"run_id":TAG,"allocation_id":TAG,"role":null,"thread_id":TID,"new_tokens":NEW,"cache_reads":CR,"complete":true}' record-run
  ... --data '{"run_id":TAG,"allocation_id":TAG,"reason":"drained: ..."}' release
Release a draining lead's scope:
  ... --data '{"session_id":LEAD_FULL_SESSION,"scope":"$R/<scope>","reason":"..."}' release-drain-scope
Register a builder (assign prints all of run.json, so redirect it to a log; first verify the brief sha256, worktree HEAD and status):
  ... --data '{"assignment_id":TAG,"allocation_id":TAG,"brief":"$R/TAG-brief.md","worktree":"$R/<scope>","model":"gpt-6.1-sol","effort":"medium","allowance":160000,"handoff_cutoff":145000,"reason":"RULING..."}' assign
Run a gate detached so your pane can close at handoff (a gate started as a Bash background task dies with its pane, which keeps old orchestrator panes open):
  ART=$R nohup .venv/bin/python /private/tmp/factory-fp-0925/swarm/bin/unit_gate.py pre TAG BASE CAND >$L/orch-$N-TAG-gate.log 2>&1 </dev/null & disown; echo $!
  Record the pid in your handoff. The successor watches it with kill -0 and reads the log on exit.
Retire a draining lead: PILOT_RUN=$R/run.json python3 scripts/swarm/pilot_lead.py --drain-retire <full session>, then wezterm cli kill-pane --pane-id <pane>.
Send a pointer: send-text $'\x05\x15\x15', then send-text "<msg>", then a separate send-text $'\r'. Verify once with get-text | grep.
Measure your context: in your transcript jsonl, take the last assistant usage (input + cache_read + cache_creation).
Spawn your successor: wezterm cli spawn --cwd "$PWD" -- env -u CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 ENABLE_CLAUDEAI_MCP_SERVERS=false claude --model claude-opus-5-5 --effort medium --permission-mode auto --settings scripts/swarm/orchestrator-settings.json --strict-mcp-config --disable-slash-commands --disallowedTools="Agent,Artifact,ArtifactComments,ArtifactData,WebSearch,WebFetch,NotebookEdit,AskUserQuestion,ScheduleWakeup,SendFeedback,ReportFindings,Skill,ListAgents,SendMessage,DesignSync,RemoteTrigger,CronCreate,CronDelete,CronList,EnterWorktree,ExitWorktree,EnterPlanMode,ExitPlanMode,PushNotification" -- "$P"

Keep context low. Fixed startup is about 11500 tokens with the spawn flags above; without them it is about 23300.
- Never print run.json sections such as reservations or drain_history. Query the exact keys you need:
  python3 -c "import json;r=json.load(open('$R/run.json'));print({k[:8]:(v['lead'].get('pid'),v.get('status'),bool(v.get('completion'))) for k,v in r['draining_leads'].items()})"
- Read a lead question with a bounded sed range from its grep line number, not the whole of questions.md.
- Pipe pane reads through grep -c for verification. Read pane state with tail -6 at most.
- Pass the template paths in the startup prompt and keep the handoff a delta of no more than 40 lines.
- Lead identities: pane 29 is ce6d1d69 (pid 38013). Map other panes with wezterm cli list --format json (pane_id and tty_name) and ps -o tty= -p PID.
- The guard denies git for the active lead while any lead drains. The orchestrator runs the git steps a lead asks for.

Startup token-waste check (user rule, October 12; RULING542a item 4). Every new orchestrator runs this once after registering, before other work:
- For each running builder, ps -p PID must show it alive, and its rollout must have grown in the last 10 minutes. Drain anything exited, and report anything stalled or idle to the lead as a blocker.
- Look for any completed run still holding a slot or usage_status unknown, and drain it first.
- Read the lead's context once. If it is near its cap, have it hand off instead of letting it grow.
- Enforce RULING542a item 4 in every pointer and brief: delta only, no rereads, no repeated passed checks, and one- or two-line pointers.
- Strict token rule (RULING542c): every read must change the next action. No rereads, no full log reads, bounded output only. Reviewer allowance 120000/105000.
- The Bash tool runs zsh, which does not word-split $x, so set -- $x passes a single argument. Wrap loops in bash -c, or the ids come out malformed, as with the inert 'tag path' assignment records.
- No waiting handoffs (RULING563d): fix the refusal cause, then have the lead rerun --handoff-start at once. Concurrent mode carries running jobs as kept_jobs; never wait for a job or drain to exit before adoption. An old <token>-controller.log blocks a retry: archive it under a new name, never delete it.
