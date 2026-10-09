#!/bin/zsh
set -eu
export SWARM_HOME=${SWARM_HOME:-${0:A:h:h:h}}
export REPO=${REPO:-$PWD}
export ART=${ART:-$REPO/.swarm}
export BIN=${BIN:-$ART/bin}
export LEAD_AGENT=${LEAD_AGENT:-p1-pilot-lead}
export FP_CAP_FRACTION=${FP_CAP_FRACTION:-0.50}
export SWARM_STATE=${SWARM_STATE:-$ART/state.md}
export LEAD_BRIEF=${LEAD_BRIEF:-$ART/lead-brief.md}
export BUILDER_LOGS=${BUILDER_LOGS:-$ART/logs}
export LEAD_LOG=${LEAD_LOG:-$BUILDER_LOGS/lead-spawns.log}
if [[ ${1:-} == --dry-run ]]; then
  exec /bin/zsh "$SWARM_HOME/scripts/swarm/lead-spawn.sh" --dry-run
fi
mkdir -p "$BIN" "$BUILDER_LOGS"
for source in "$SWARM_HOME"/scripts/swarm/*(.N); do
  temporary=$(mktemp "$BIN/.${source:t}.XXXXXX")
  cp -p "$source" "$temporary" && mv -f "$temporary" "$BIN/${source:t}"
done
exec /bin/zsh "$BIN/lead-spawn.sh" "$@"
