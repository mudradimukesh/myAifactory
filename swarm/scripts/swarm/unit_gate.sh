#!/bin/zsh
set -eu
repo=${REPO:-/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer}
exec "${PILOT_PYTHON:-$repo/.venv/bin/python}" "${0:A:h}/unit_gate.py" "$@"
