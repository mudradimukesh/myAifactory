#!/bin/zsh
set -eu
repo=${REPO:-$PWD}
exec "${PILOT_PYTHON:-$repo/.venv/bin/python}" "${0:A:h}/unit_gate.py" "$@"
