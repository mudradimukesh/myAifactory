#!/bin/zsh
set -eu
LOGS=${BUILDER_LOGS:-${ART:?Set ART}/logs}
[[ $# == 2 && -f $1 ]] || { print 'usage: builder-start.sh <chain.sh> <tag>'; exit 2; }
zsh -n "$1"
pid=$(python3 -c 'import subprocess,sys; print(subprocess.Popen(["/bin/zsh", sys.argv[1]], stdin=subprocess.DEVNULL, stdout=open(sys.argv[2], "w"), stderr=subprocess.STDOUT, start_new_session=True).pid)' "$1" "$LOGS/$2-chain.log")
print -r -- "$2	$pid	$(date '+%FT%T')	$LOGS/$2.json" >> "$LOGS/builders.tsv"
print -r -- "$2 pid=$pid log=$LOGS/$2.json"
