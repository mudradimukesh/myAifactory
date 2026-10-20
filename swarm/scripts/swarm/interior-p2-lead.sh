#!/bin/zsh
set -u
export REPO=/tmp/factory-fp-0925/swarm/interior-p2/wt/lead
export LEAD_AGENT=p2-lead
export JEV_LEDGER=/tmp/factory-fp-0925/swarm/interior-p2/ART/jev-agent-spend.jsonl JEV_CAP_USD=inf
export LEAD_BRIEF=$REPO/prompts/phase-2/lead-brief.md
export BUILDER_LOGS=/tmp/factory-fp-0925/swarm/interior-p2/logs
export LEAD_LOG=$BUILDER_LOGS/lead-spawns.log
[[ $(git -C "$REPO" branch --show-current 2>/dev/null) == p2-integration && -f $LEAD_BRIEF ]] || { print -u2 "no p2-integration worktree at $REPO; run prompts/phase-2/bootstrap.md first"; exit 2; }
mkdir -p "$BUILDER_LOGS" /tmp/factory-fp-0925/swarm/interior-p2/ART || exit 1
exec /bin/zsh /tmp/factory-fp-0925/swarm/bin/lead-spawn.sh "$@"
