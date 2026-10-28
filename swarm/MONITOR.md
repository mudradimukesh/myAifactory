# Monitor runbook

A new Claude Code session follows this file to become the swarm monitor and to start or restart the NP2 pilot. It works from any directory.

## Paths

| Name | Value |
| --- | --- |
| REPO | `/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer` (main checkout, stays on `poc-test-prioritization` with unrelated dirty files) |
| MAIN | `/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer-wt/main-merge` (checkout of interior `main`; merges run here) |
| ART | `REPO/research_notes/Non-Phase-2 integration-20261021` (always quote it: it has spaces) |
| RUN | `ART/run.json`, the ledger. Never read it whole; query fields with `python3 -c` or `jq`. |
| BIN | `/tmp/factory-fp-0925/swarm/bin`, the installed copy of `scripts/swarm` |
| PY | `REPO/.venv/bin/python` |

Lead, builder and reviewer transcripts are under `~/.claude/projects/-Users-mukeshkumarmudradi-Documents-AI-Projects-github-FloorInteriorDesignAndRenderer*/`. The previous monitor is the most recent session in the REPO project folder whose transcript contains `swarm_monitor.py`. Read its last assistant messages for open work.

## 1. Check BIN

macOS clears `/tmp` on reboot and deletes files there that have not been accessed for 3 days. If `BIN/pilot_lead.py` is missing, reinstall BIN from interior main:

```sh
B=/tmp/factory-fp-0925/swarm/bin; mkdir -p $B; cd "$MAIN"; git fetch interior; H=$(git rev-parse interior/main)
for f in $(git ls-tree --name-only $H scripts/swarm/); do n=${f##*/}; git show $H:$f > $B/.$n.tmp; [ "$(git ls-tree $H $f | cut -c1-6)" = 100755 ] && chmod 755 $B/.$n.tmp || chmod 644 $B/.$n.tmp; mv $B/.$n.tmp $B/$n; done
cp -p ~/Documents/AI_Projects/myAIfactory/swarm/scripts/swarm/claude-reviewer-settings.json $B/
cd $B && for m in pilot_lead run_state pilot_guard codex_launch review_check gate_evidence unit_gate red_check; do $PY -c "import $m" || echo "IMPORT FAIL $m"; done
```

## 2. Keep the Mac awake

Rule: the pilot keeps working while the Mac is idle or locked. Sleep suspends every lead, builder and gate process.

- nix-darwin sets `power.sleep.computer = "never"` in the dotfiles `configuration.nix`; it applies after `sudo darwin-rebuild switch`.
- At takeover and after any reboot, check `pgrep -lf 'caffeinate -ims'` and start one if none runs: `nohup caffeinate -ims >/dev/null 2>&1 & disown`. It holds until reboot.
- `-s` only prevents system sleep on AC power, and closing the lid still sleeps the Mac unless an external display and power are attached. Tell the user both before they unplug or close the lid. Lid-closed operation on battery needs `sudo pmset -a disablesleep 1`, which is the user's step.

## 3. Arm the monitor

Use the Monitor tool with a 30 minute timeout and re-arm it on every expiry:

```sh
cd "$ART/monitor" && while true; do find /tmp/factory-fp-0925 -type f -exec touch -a {} + 2>/dev/null; python3 -u swarm_monitor.py monitor-state.json 2>&1 | grep -v --line-buffered SUMMARY || true; sleep 60; done
```

`swarm_monitor.py` (kept in this kit under `monitor/`, run from `ART/monitor`) reports LEAD DEAD, LEAD STOPPED, IDLE SLOTS, UNKNOWN USAGE, BLOCKER and context thresholds. The `touch -a` keeps BIN from ageing out.

## 4. Start or restart the pilot

Check the active lead: `python3 -c "import json;s=json.load(open('$ART/run.json'));print(s.get('active_lead',{}).get('pid'),s.get('active_lead',{}).get('model_session_id'),s['lifecycle'],s['dispatch_frozen'])"`. If that pid is alive and its WezTerm pane exists, the pilot is running; do not start another.

If the lead is dead (reboot or crash):

1. Find its runs that never finished: allocations in `reservations` with `active_run` set and `parent_session_id` equal to the dead lead. For each, confirm no process is left, compute the final usage with `pilot_guard.claude_usage(Path(<rollout_path from ART/<run>-launch.json>))` (import from BIN), and record it:
   `$PY $BIN/run_state.py --run "$ART/run.json" --as orchestrator record-run --data '{"allocation_id":"<id>","run_id":"<id>","thread_id":"<session>","new_tokens":N,"cache_reads":N,"complete":true,"transcript_path":"<rollout>","reason":"Monitor reconciliation: ..."}'`
   Counters are cumulative and cannot decrease.
2. Retire the dead lead: `PILOT_RUN="$ART/run.json" $PY $BIN/pilot_lead.py --stop`.
3. Start a fresh lead from REPO:

```sh
cd "$REPO"; A="$PWD/research_notes/Non-Phase-2 integration-20261021"
env REPO="$PWD" BIN=/tmp/factory-fp-0925/swarm/bin ART="$A" PILOT_RUN="$A/run.json" LEAD_AGENT=p1-pilot-lead LEAD_PROVIDER=claude LEAD_MODEL=claude-opus-5-5 LEAD_EFFORT=medium PILOT_LEAD_OVERLAP=1 LEAD_BRIEF="$A/lead-brief.txt" LEAD_LOG="$A/logs/lead-spawns.log" BUILDER_LOGS="$A/logs" python3 /tmp/factory-fp-0925/swarm/bin/pilot_lead.py --start
```

It opens the lead in a new WezTerm pane and prints its session and pane. Tell the lead what happened (step 6) and to relaunch the jobs the restart killed.

Never use `interior-lead.sh --start`: it copies the main checkout's older `scripts/swarm` over BIN.

## 5. Worktrees

Builder and gate worktrees fill the disk. Remove a worktree only when its HEAD is merged into `main`, RUN does not reference its path, and every changed or untracked file in it matches a blob reachable from a branch or tag. Keep every worktree RUN references and every unmerged one. Run `git worktree prune` first, and remove with `git worktree remove --force --force <path>`.

## 6. Duties

- Message a lead with literal text, then a separate carriage return, each with an 8 second timeout because `wezterm cli` can hang:
  `( wezterm cli send-text --pane-id P --no-paste 'Monitor: ...' & p=$!; sleep 8; kill $p 2>/dev/null; true ); sleep 1; ( wezterm cli send-text --pane-id P --no-paste $'\r' & p=$!; sleep 8; kill $p 2>/dev/null; true )`
- Lead handoff: when a lead's context passes 250,000 tokens, tell it to run `BIN/interior-lead.sh --handoff-start` with `LEAD_PROVIDER=claude LEAD_MODEL=claude-opus-5-5 LEAD_EFFORT=medium`, in the background.
- Idle slots: when fewer than 8 builders run and nothing blocks dispatch, tell the lead to launch builders, reviews and restacks in parallel before more reading.
- Ceilings: raise them with `run_state.py --as orchestrator set-ceilings` and tell the user. The user does not want work to stop on budget.
- Blockers owned by the monitor: fix them, then `run_state.py --as orchestrator close-blocker --data '{"id":"...","reason":"..."}'`. Leads cannot write BIN, so tooling installs into BIN are monitor work.
- Rulings and answers go to `ART/questions.md` as a new line near the top, with an id like `NP2-MONITOR-<topic>-<date>`.
- A sealed-scoring step that needs the `fpeval` user's password is the user's step. Give them the exact command; `monitor/fpeval-inventory.sh` prints counts and hashes only.

## 7. Sync this kit

After swarm tooling merges to interior main, copy `scripts/swarm`, `tests/swarm`, `.claude/agents` and `prompts/phase-1` from that commit into `swarm/` here, update the source commit in `README.md`, and commit and push.
