#!/bin/zsh
set -eu
if [[ -z "${ART+x}" && -n "${PILOT_RUN+x}" ]]; then
    export ART="${PILOT_RUN:h}"
fi
exec python3 "${0:A:h}/integration.py" prepare "$@"
