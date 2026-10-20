#!/bin/zsh
set -u
export REPO=/tmp/factory-fp-0925/swarm/interior-p25/wt/lead
export LEAD_AGENT=p25-lead
export JEV_LEDGER=/tmp/factory-fp-0925/swarm/interior-p25/ART/jev-agent-spend.jsonl
export LEAD_BRIEF=$REPO/prompts/phase-2.5/lead-brief.md
export BUILDER_LOGS=/tmp/factory-fp-0925/swarm/interior-p25/logs
export LEAD_LOG=$BUILDER_LOGS/lead-spawns.log
[[ $(git -C "$REPO" branch --show-current 2>/dev/null) == interior-p25 && -f $LEAD_BRIEF ]] || { print -u2 "no interior-p25 worktree at $REPO; run prompts/phase-2.5/bootstrap.md first"; exit 2; }
mkdir -p "$BUILDER_LOGS" /tmp/factory-fp-0925/swarm/interior-p25/ART || exit 1
exec /bin/zsh /tmp/factory-fp-0925/swarm/bin/lead-spawn.sh "$@"
