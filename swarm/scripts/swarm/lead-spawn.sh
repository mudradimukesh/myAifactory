#!/bin/zsh
set -u
if [[ ${LEAD_AGENT:-} == p1-pilot-lead && ${1:-} != --dry-run ]]; then
  exec python3 "${0:A:h}/pilot_lead.py" "$@"
fi
REPO=${REPO:-/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer}
BIN=${BIN:-/tmp/factory-fp-0925/swarm/bin}
BRIEF=${LEAD_BRIEF:-/tmp/factory-fp-0925/swarm/fp3d/briefs/lead-2.md}
NOTES=/Users/mukeshkumarmudradi/Documents/AI_Projects/github/dotfiles/home/.agents/skills/research/notes
LOG=${LEAD_LOG:-/tmp/factory-fp-0925/swarm/fp3d/logs/lead-spawns.log}
LOCK=$LOG.lock
MAX_PER_DAY=12

launch_cmd() {
  local base="" tag="" env=""
  lsof -nP -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1 && base="ANTHROPIC_BASE_URL=http://127.0.0.1:8787 "
  [[ -n ${1:-} ]] && tag="FP_LEAD_SPAWN=${(q)1} "
  local v; for v in REPO BIN LEAD_AGENT LEAD_BRIEF LEAD_LOG BUILDER_LOGS FP_CAP_FRACTION; do [[ -n ${(P)v:-} ]] && env+="$v=${(q)${(P)v}} "; done
  print -r -- "cd ${(q)REPO} && exec env -u FP_CONTEXT_CAP CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 ${base}${env}${tag}FP_LEAD=1 claude --disable-slash-commands ${LEAD_AGENT:+--agent ${(q)LEAD_AGENT} }--model claude-opus-5-5 --effort medium --permission-mode auto --settings ${(q)BIN}/builder-settings.json --add-dir /tmp/factory-fp-0925/swarm --add-dir ${(q)NOTES} -- \"\$(cat ${(q)BRIEF})\""
}
claude_ancestor() {
  local p=$PPID name
  while [[ -n $p && $p != 1 ]]; do
    name=$(ps -o comm= -p "$p" 2>/dev/null)
    [[ ${name:t} == claude ]] && { print -r -- "$p"; return 0; }
    p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
  done
  return 1
}
new_lead_pid() {
  local pid
  for pid in $(pgrep -x claude); do
    [[ $pid == $1 ]] && continue
    ps -Eww -o command= -p "$pid" 2>/dev/null | grep -q " FP_LEAD_SPAWN=$2\( \|$\)" && { print -r -- "$pid"; return 0; }
  done
  return 1
}
log() { print -r -- "$(date '+%FT%T') $*" >> "$LOG"; }

case ${1:-} in
  --dry-run) launch_cmd; exit 0 ;;
  --start)
    [[ -f $BRIEF ]] || { print "no brief at $BRIEF"; exit 2; }
    mkdir -p "${LOG:h}" || exit 1
    pane=$(wezterm cli spawn --cwd "$REPO" -- /bin/zsh -c "$(launch_cmd)") || { print 'wezterm spawn failed; is WezTerm running?'; exit 1; }
    log "started pane=$pane brief=$BRIEF"
    print -r -- "started pane=$pane"
    exit 0 ;;
  --detached)
    [[ $# == 4 && $2 == <-> && $3 == <-> && -d $LOCK ]] || exit 2
    old=$2
    oldpane=$3
    token=$4
    trap 'rmdir "$LOCK" 2>/dev/null' EXIT
    trap 'exit 1' HUP INT TERM
    pane=$(wezterm cli spawn --pane-id "$oldpane" --cwd "$REPO" -- /bin/zsh -c "$(launch_cmd "$token")") || { log "spawn failed; old lead $old left running"; exit 1; }
    new=""
    for i in {1..30}; do new=$(new_lead_pid "$old" "$token") && break; sleep 1; done
    [[ -n $new ]] || { log "pane=$pane but no new lead within 30s; old lead $old left running"; exit 1; }
    sleep 2
    kill -0 "$new" 2>/dev/null || { log "pane=$pane new=$new exited; old lead $old left running"; exit 1; }
    for i in {1..1800}; do [[ -e $LOG.adopted-$token ]] && break; sleep 1; done
    [[ -e $LOG.adopted-$token ]] || { log "pane=$pane new=$new did not adopt builders within 30 min; old lead $old left running"; exit 1; }
    kill -TERM "$old" 2>/dev/null
    for i in {1..30}; do kill -0 "$old" 2>/dev/null || break; sleep 1; done
    kill -0 "$old" 2>/dev/null && kill -KILL "$old" 2>/dev/null
    if kill -0 "$old" 2>/dev/null; then log "pane=$pane new=$new could not end old=$old"; exit 1; fi
    wezterm cli kill-pane --pane-id "$oldpane" 2>/dev/null || true
    log "spawned pane=$pane new=$new ended=$old"
    exit 0 ;;
  --adopt)
    [[ -n ${FP_LEAD_SPAWN:-} ]] && : > "$LOG.adopted-$FP_LEAD_SPAWN"
    [[ -f ${LOG:h}/builders.tsv ]] || { print 'no builders'; exit 0; }
    while IFS=$'\t' read -r tag pid started out; do
      kill -0 "$pid" 2>/dev/null && print -r -- "running $tag pid=$pid since $started log=$out"
    done < "${LOG:h}/builders.tsv"
    exit 0 ;;
  "")
    [[ ${FP_LEAD:-} == 1 && ${WEZTERM_PANE:-} == <-> ]] || { print 'run this from a guarded lead in a WezTerm tab'; exit 2; }
    old=$(claude_ancestor) || { print 'no claude ancestor; run this from inside the lead session'; exit 2; }
    mkdir -p "${LOG:h}" || exit 1
    mkdir "$LOCK" 2>/dev/null || { print "another rollover is in progress; lock: $LOCK"; exit 3; }
    trap 'rmdir "$LOCK" 2>/dev/null' EXIT
    today=$(grep -c "^$(date '+%F') rollover requested by " "$LOG" 2>/dev/null); today=${today:-0}
    (( today >= MAX_PER_DAY )) && { print "refused: $today lead spawns today (cap $MAX_PER_DAY)"; exit 3; }
    for file in fp-guard.py builder-settings.json; do
      [[ -f $BIN/$file ]] || cp "$REPO/scripts/swarm/$file" "$BIN/$file" || exit 1
    done
    if [[ ! -f $BRIEF ]]; then
      mkdir -p "${BRIEF:h}" && cp "$REPO/scripts/swarm/lead-brief.md" "$BRIEF" || exit 1
    fi
    token=$(uuidgen) || exit 1
    log "rollover requested by $old token=$token"
    nohup /bin/zsh "$0" --detached "$old" "$WEZTERM_PANE" "$token" >> "$LOG" 2>&1 &!
    trap - EXIT
    print 'handing over: next lead opens in a new WezTerm tab, then this session ends'
    exit 0 ;;
  *) print 'usage: lead-spawn.sh [--dry-run|--start|--adopt]'; exit 2 ;;
esac
