#!/bin/zsh
set -u
export REPO=/tmp/factory-fp-0925/swarm/interior-p3/wt/lead
export LEAD_AGENT=p3-lead
export P3_ART_ROOT=/tmp/factory-fp-0925/swarm/interior-p3/ART
export JEV_LEDGER="$P3_ART_ROOT/jev-agent-spend.jsonl"
export LEAD_BRIEF=$REPO, prompts and phase-3/lead-brief.md
export BUILDER_LOGS=/tmp/factory-fp-0925/swarm/interior-p3/logs
export LEAD_LOG=$BUILDER_LOGS/lead-spawns.log
[[ $(git -C "$REPO" branch --show-current 2>/dev/null) == interior-p3 && -f $LEAD_BRIEF ]] || { print -u2 "no interior-p3 worktree at $REPO; run prompts/phase-3/bootstrap.md first"; exit 2; }
mkdir -p "$BUILDER_LOGS" /tmp/factory-fp-0925/swarm/interior-p3/ART || exit 1
exec /bin/zsh /tmp/factory-fp-0925/swarm/bin/lead-spawn.sh "$@"
