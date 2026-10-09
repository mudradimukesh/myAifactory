#!/bin/zsh
set -eu
exec python3 "${0:A:h}/integration.py" prepare "$@"
