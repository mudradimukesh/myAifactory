#!/bin/zsh
# Start the next P1 orchestrator and retire the current one without a gap.
#   orchestrator-spawn.sh <next-number>          from inside the current orchestrator, after its handoff
#   orchestrator-spawn.sh --start <number>       first orchestrator, no predecessor
#   orchestrator-spawn.sh --dry-run <number>     print the launch command
# The detached part registers the successor and waits for its readiness receipt
# (ART/logs/orchestrator-<n>-ready.json naming its session and the handoff hash).
# Only then does it close the predecessor, after verifying the predecessor's exact
# identity and that it has no child processes, and it records final usage only after
# the predecessor has exited. Any failure leaves the predecessor running and is logged.
set -u
REPO=${REPO:-/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer}
ART=${ART:-/private/tmp/factory-fp-0925/swarm/interior-p1-poc}
CAP=${ORCH_CAP:-100000}
PID_WAIT=${SPAWN_PID_WAIT:-60}
READY_WAIT=${SPAWN_READY_WAIT:-900}
EXIT_WAIT=${SPAWN_EXIT_WAIT:-30}
RUN=$ART/run.json
HANDOFF=$REPO/prompts/phase-1/orchestrator-handoff.md
LOG=$ART/logs/orchestrator-spawns.log
LOCK=$LOG.lock
TRANSCRIPTS=$HOME/.claude/projects/${REPO//[\/_.]/-}
DISALLOWED=Agent,Artifact,ArtifactComments,ArtifactData,WebSearch,WebFetch,NotebookEdit,AskUserQuestion,ScheduleWakeup,SendFeedback,ReportFindings,Skill,ListAgents,SendMessage,DesignSync,RemoteTrigger,CronCreate,CronDelete,CronList,EnterWorktree,ExitWorktree,EnterPlanMode,ExitPlanMode,PushNotification

ready_file() { print -r -- "$ART/logs/orchestrator-$1-ready.json"; }
prompt() {
  print -r -- "You are orchestrator $1, session $2. Follow prompts/phase-1/orchestrator-brief.md and read the latest orchestrator handoff (prompts/phase-1/orchestrator-handoff.md). ART=$ART. Command templates: ART/logs/orchestrator-templates.md. First, after reading the handoff, use Write to create $(ready_file $1) containing exactly {\"session_id\":\"$2\",\"handoff_sha256\":\"$3\"}. scripts/swarm/orchestrator-spawn.sh is registering you and, once that file exists, retires your predecessor and records its final usage. These steps are pending, not done: read tail -4 ART/logs/orchestrator-spawns.log together with your next other read, and do by template any step it reports as not done. Context cap $CAP: at your handoff, write the handoff, then run scripts/swarm/orchestrator-spawn.sh $(( $1 + 1 )).

$PACKET"
}
launch_cmd() {
  print -r -- "cd ${(q)REPO} && exec env -u CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 ENABLE_CLAUDEAI_MCP_SERVERS=false claude --session-id $2 --model claude-opus-5-5 --effort medium --permission-mode auto --settings scripts/swarm/orchestrator-settings.json --strict-mcp-config --disable-slash-commands --disallowedTools=$DISALLOWED -- ${(q)$(prompt $1 $2 $3)}"
}
claude_ancestor() {
  local p=$PPID
  while [[ -n $p && $p != 1 ]]; do
    [[ $(ps -o comm= -p "$p" 2>/dev/null) == (*/|)claude ]] && { print -r -- "$p"; return 0; }
    p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
  done
  return 1
}
# Exact identity: Claude's session record for this pid names the session and its start time.
session_field() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$HOME/.claude/sessions/$1.json" "$2" 2>/dev/null; }
same_process() {
  [[ $(session_field "$1" sessionId) == "$2" ]] || return 1
  [[ $(session_field "$1" procStart) == "$(ps -o lstart= -p "$1" 2>/dev/null | sed 's/  */ /g; s/^ //; s/ $//')" ]]
}
# A zombie has exited; its parent shell reaps it.
running() { ps -o stat= -p "$1" 2>/dev/null | grep -qv '^Z'; }
ledger() { python3 "$REPO/scripts/swarm/run_state.py" --run "$RUN" --as orchestrator "$1" --data "$2" >/dev/null; }
log() { print -r -- "$(date '+%FT%T') $*" >> "$LOG"; }

usage_json() {
  (cd "$REPO/scripts/swarm" && python3 -c 'import json,sys;from pathlib import Path;from pilot_guard import claude_usage;print(json.dumps(claude_usage(Path(sys.argv[1]))))' "$TRANSCRIPTS/$1.jsonl" 2>/dev/null)
}

detached() {
  local num=$1 session=$2 pane=$3 hash=$4 old=$5 oldpane=$6 oldsession=$7 pid="" started="" registered=0 i
  trap 'rmdir "$LOCK" 2>/dev/null' EXIT
  # The successor is this exact process: same pid, start time and --session-id.
  successor_alive() {
    running "$pid" && [[ $(ps -o lstart= -p "$pid" 2>/dev/null) == "$started" ]] \
      && ps -o command= -p "$pid" 2>/dev/null | grep -qF -- "--session-id $session"
  }
  # A failed succession stops the successor and marks its inventory complete, so alerts
  # and the watchdog route back to the predecessor, which keeps running.
  fail() {
    log "orchestrator $num: $1; predecessor kept"
    if [[ -n $pid ]] && successor_alive; then
      wezterm cli kill-pane --pane-id "$pane" >/dev/null 2>&1
      for (( i = 0; i < EXIT_WAIT; i++ )); do successor_alive || break; sleep 1; done
      successor_alive && kill -TERM "$pid" 2>/dev/null
      for (( i = 0; i < EXIT_WAIT; i++ )); do successor_alive || break; sleep 1; done
    fi
    # Completion needs a verified exit; a survivor or unknown usage stays an open blocker.
    if (( registered )); then
      local usage=$(usage_json "$session") blocker="SPAWN-${session:0:8}" routed=1
      if [[ -n $pid ]] && successor_alive; then
        ledger add-blocker "{\"id\":\"$blocker\",\"reason\":\"Failed successor $session (pid $pid) survived pane close and TERM; it is still registered as incomplete. Stop it by template, then record its final usage.\"}"
        routed=0
        log "orchestrator $num: RECOVERY NEEDED: successor $session pid $pid is still running after pane close and TERM; NOT marked complete; blocker $blocker"
      elif [[ $usage == \{* ]] && ledger record-run "{\"run_id\":\"orchestrator-$session\",\"thread_id\":\"$session\",\"role\":\"orchestrator\",${usage:1:-1},\"complete\":true}"; then
        log "orchestrator $num: successor $session exited and was marked complete with usage=$usage; routing restored to the predecessor"
      elif ledger record-run "{\"run_id\":\"orchestrator-$session\",\"thread_id\":\"$session\",\"role\":\"orchestrator\",\"usage_status\":\"unknown\",\"complete\":true}"; then
        ledger add-blocker "{\"id\":\"$blocker\",\"reason\":\"Failed successor $session exited with unknown final usage; reconcile its transcript before final accounting.\"}"
        log "orchestrator $num: successor $session exited; routing restored to the predecessor; final usage unknown, blocker $blocker"
      else
        routed=0
        log "orchestrator $num: RECOVERY NEEDED: successor $session exited but is still registered as incomplete; mark it complete by template before dispatch"
      fi
    fi
    (( ${routed:-1} )) && [[ -n $old ]] && PILOT_RUN=$RUN "$REPO/scripts/swarm/alert-orchestrator.sh" \
      "SPAWN FAILED for orchestrator $num: $1. You remain the orchestrator; see ART/logs/orchestrator-spawns.log." >/dev/null 2>&1
    exit 1
  }
  for (( i = 0; i < PID_WAIT; i++ )); do
    pid=$(pgrep -f -- "--session-id $session") && break
    sleep 1
  done
  [[ -n $pid ]] || { pid=""; fail "no process for session $session within ${PID_WAIT}s"; }
  pid=${pid%%$'\n'*}
  started=$(ps -o lstart= -p "$pid" 2>/dev/null)
  wezterm cli set-tab-title --pane-id "$pane" "P1 POC orchestrator $num" >/dev/null 2>&1
  ledger register-inventory "{\"thread_id\":\"$session\",\"session_id\":\"$session\",\"identity_kind\":\"claude_session\",\"provider\":\"claude-headroom\",\"role\":\"orchestrator\",\"pane_id\":$pane,\"pid\":$pid}" \
    || fail "registration NOT done (ledger refused the inventory row)"
  registered=1
  ledger record-run "{\"run_id\":\"orchestrator-$session\",\"thread_id\":\"$session\",\"role\":\"orchestrator\",\"new_tokens\":0,\"cache_reads\":0,\"transcript_path\":\"$TRANSCRIPTS/$session.jsonl\"}" \
    || fail "registration NOT done (ledger refused the accounting run)"
  log "orchestrator $num registration done: session=$session pid=$pid pane=$pane"
  local wpid=$ART/logs/orchestrator-watchdog.pid
  if ! { [[ -f $wpid ]] && kill -0 "$(<$wpid)" 2>/dev/null; }; then
    rm -f "$wpid"
    nohup python3 "$REPO/scripts/swarm/orchestrator_watchdog.py" "$ART" >/dev/null 2>&1 &!
    log "watchdog restarted"
  fi
  local ready=$(ready_file "$num") expect="{\"session_id\":\"$session\",\"handoff_sha256\":\"$hash\"}"
  receipt_valid() { [[ -f $ready && $(tr -d ' \n' < "$ready") == "$expect" ]]; }
  for (( i = 0; i < READY_WAIT; i++ )); do
    successor_alive || fail "successor pid $pid is not running"
    receipt_valid && break
    sleep 1
  done
  (( i < READY_WAIT )) || fail "no valid readiness receipt in ${READY_WAIT}s"
  python3 "$REPO/scripts/swarm/startup_packet.py" "$ART" orchestrator --commit "$session" \
    || log "orchestrator $num: packet commit failed; its entries will be delivered again"
  log "orchestrator $num ready: receipt $ready matches session and handoff"
  [[ -n $old ]] || exit 0
  same_process "$old" "$oldsession" || { log "predecessor pid $old is not session $oldsession; not closed, retirement NOT done"; exit 1; }
  local children
  children=$(pgrep -P "$old" 2>/dev/null | tr '\n' ' ')
  [[ -z $children ]] || { log "predecessor $oldsession still has child processes ($children); not closed, retirement NOT done"; exit 1; }
  successor_alive && receipt_valid || fail "successor $session is no longer running or ready at retirement"
  wezterm cli kill-pane --pane-id "$oldpane" >/dev/null 2>&1
  for (( i = 0; i < EXIT_WAIT; i++ )); do running "$old" || break; sleep 1; done
  if running "$old" && same_process "$old" "$oldsession"; then
    kill -TERM "$old" 2>/dev/null
    for (( i = 0; i < EXIT_WAIT; i++ )); do running "$old" || break; sleep 1; done
  fi
  if running "$old"; then
    log "predecessor pid $old still running after pane close and TERM; final usage NOT recorded"
    exit 1
  fi
  local usage
  usage=$(usage_json "$oldsession")
  if [[ $usage == \{* ]] && ledger record-run "{\"run_id\":\"orchestrator-$oldsession\",\"thread_id\":\"$oldsession\",\"role\":\"orchestrator\",${usage:1:-1},\"complete\":true}"; then
    log "predecessor retirement done: session=$oldsession pid=$old exited, pane $oldpane closed, usage=$usage"
  else
    log "predecessor pid $old exited, pane $oldpane closed; final usage NOT recorded (usage=${usage:-none})"
    exit 1
  fi
}

case ${1:-} in
  --dry-run) [[ ${2:-} == <-> ]] || exit 2; PACKET='(startup packet)'; launch_cmd "$2" "$(uuidgen | tr A-Z a-z)" "(handoff hash)"; exit 0 ;;
  --detached) shift; detached "$@"; exit $? ;;
  --start) [[ ${2:-} == <-> ]] || { print 'usage: orchestrator-spawn.sh --start <number>'; exit 2; }; num=$2; old=""; oldpane=""; oldsession="" ;;
  <->)
    num=$1
    [[ ${WEZTERM_PANE:-} == <-> ]] || { print 'run this from the orchestrator pane in WezTerm'; exit 2; }
    old=$(claude_ancestor) || { print 'no claude ancestor; run this from inside the orchestrator session'; exit 2; }
    oldpane=$WEZTERM_PANE
    oldsession=$(session_field "$old" sessionId) || { print "no session record for pid $old"; exit 2; } ;;
  *) print 'usage: orchestrator-spawn.sh <next-number> | --start <number> | --dry-run <number>'; exit 2 ;;
esac
[[ -f $RUN ]] || { print "no ledger at $RUN; restore run state before succession"; exit 2; }
[[ -f $HANDOFF ]] || { print "no handoff at $HANDOFF"; exit 2; }
mkdir -p "${LOG:h}" || exit 1
mkdir "$LOCK" 2>/dev/null || { print "another orchestrator spawn is in progress; lock: $LOCK"; exit 3; }
session=$(uuidgen | tr A-Z a-z)
hash=$(shasum -a 256 "$HANDOFF" | cut -d' ' -f1)
rm -f "$(ready_file "$num")"
PACKET=$(python3 "$REPO/scripts/swarm/startup_packet.py" "$ART" orchestrator "$session" 2>&1) || PACKET="Startup packet unavailable: $PACKET"
pane=$(wezterm cli spawn --cwd "$REPO" -- /bin/zsh -c "$(launch_cmd "$num" "$session" "$hash")") || { rmdir "$LOCK"; print 'wezterm spawn failed; is WezTerm running?'; exit 1; }
log "orchestrator $num spawned: pane=$pane session=$session predecessor=${old:-none}"
nohup /bin/zsh "$0" --detached "$num" "$session" "$pane" "$hash" "$old" "$oldpane" "$oldsession" >> "$LOG" 2>&1 &!
print -r -- "orchestrator $num starting in pane $pane; this pane closes once it registers and confirms its handoff"
