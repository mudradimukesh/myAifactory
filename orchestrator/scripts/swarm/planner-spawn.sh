#!/bin/zsh
set -eu
: ${LEAD_BRIEF:?Set LEAD_BRIEF to the new goal's planning brief}
: ${REPO:?Set REPO}
: ${ART:?Set ART}
export LEAD_AGENT=p1-planner
exec /bin/zsh "${0:A:h}/lead-spawn.sh" "$@"
