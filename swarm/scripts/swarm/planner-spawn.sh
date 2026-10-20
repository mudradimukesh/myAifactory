#!/bin/zsh
set -u
REPO=/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer
BIN=/tmp/factory-fp-0925/swarm/bin
export LEAD_BRIEF=$BIN/planner-brief.md
export BUILDER_LOGS=/tmp/factory-fp-0925/swarm/interior-p1/planner
export LEAD_LOG=$BUILDER_LOGS/planner-spawns.log
mkdir -p "$BIN" "$BUILDER_LOGS" && cp "$REPO"/scripts/swarm/* "$BIN"/ || exit 1
exec /bin/zsh "$BIN/lead-spawn.sh" "$@"
