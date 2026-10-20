#!/bin/zsh
set -u
export REPO=${REPO:-/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer}
export BIN=${BIN:-/tmp/factory-fp-0925/swarm/bin}
export LEAD_AGENT=${LEAD_AGENT:-p1-lead}
export PILOT_LEAD_OVERLAP=${PILOT_LEAD_OVERLAP:-1}
export FP_CAP_FRACTION=${FP_CAP_FRACTION:-0.90}
export JEV_LEDGER=/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer/ledger/api-spend.jsonl
export LEAD_BRIEF=${LEAD_BRIEF:-$REPO/prompts/phase-1/lead-brief.md}
export BUILDER_LOGS=${BUILDER_LOGS:-/tmp/factory-fp-0925/swarm/interior-p1/logs}
export LEAD_LOG=${LEAD_LOG:-$BUILDER_LOGS/lead-spawns.log}
if [[ ${1:-} == --dry-run ]]; then
  exec /bin/zsh "$REPO/scripts/swarm/lead-spawn.sh" --dry-run
fi
if [[ $LEAD_AGENT == p1-pilot-lead && ( ${1:-} == --handoff-begin || ${1:-} == --handoff-start || ${1:-} == --drain-ready || ${1:-} == --drain-retire || ${1:-} == --drain-stop-gate ) ]]; then
  exec /bin/zsh "$BIN/lead-spawn.sh" "$@"
fi
mkdir -p "$BIN" "$BUILDER_LOGS" /tmp/factory-fp-0925/swarm/fp3d/logs || exit 1
for source in "$REPO"/scripts/swarm/*(.N); do
  temporary=$(mktemp "$BIN/.${source:t}.XXXXXX") || exit 1
  cp -p "$source" "$temporary" && mv -f "$temporary" "$BIN/${source:t}" || exit 1
done
exec /bin/zsh "$BIN/lead-spawn.sh" "$@"
