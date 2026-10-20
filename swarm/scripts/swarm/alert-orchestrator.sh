#!/bin/bash
# Usage: alert-orchestrator.sh [--target lead|orchestrator] [--log] "message"
# Sends the message to the registered pane and confirms that the recipient session
# received it: the message carries a unique "(alert <id>)" marker, and delivery counts
# only once a new typed user message with that marker appears in the recipient's
# transcript. Refuses, without sending, when the composer is not empty or its state or
# the transcript is unknown.
# --log appends the message to questions.md first; a failed delivery reports that the
# entry is already logged, so a retry must drop --log.
# PILOT_RUN defaults to the POC run.json.
set -euo pipefail
run=${PILOT_RUN:-/tmp/factory-fp-0925/swarm/interior-p1-poc/run.json}
wait_s=${ALERT_WAIT_S:-15}
usage="usage: $0 [--target lead|orchestrator] [--log] \"message\""
target=orchestrator
log=0
while [[ ${1:-} == --* ]]; do
  case $1 in
    --target)
      [[ ${2:-} == lead || ${2:-} == orchestrator ]] || { echo "$usage" >&2; exit 2; }
      target=$2
      shift 2 ;;
    --log) log=1; shift ;;
    *) echo "$usage" >&2; exit 2 ;;
  esac
done
[[ $# -eq 1 && -n $1 ]] || { echo "$usage" >&2; exit 2; }
if [[ $target == lead ]]; then
  read -r pane transcript < <(jq -r '(.active_lead.model_session_id // "") as $s
    | [(.active_lead | (.pane_id // .pane // "") | tostring),
       ([.inventory.sessions[]? | select(.session_id == $s) | .transcript_path // empty] | last // "")]
    | @tsv' "$run")
else
  read -r pane transcript < <(jq -r '[.inventory.threads[]?
    | select(.role == "orchestrator" and (.pane_id | type) == "number" and ((.complete // false) | not))]
    | last // {} | [(.pane_id // "" | tostring), (.transcript_path // "")] | @tsv' "$run")
fi
[[ $pane =~ ^[0-9]+$ ]] || { echo "no active $target pane_id in $run" >&2; exit 1; }
wezterm cli list --format json | jq -e --argjson p "$pane" 'any(.[]; .pane_id == $p)' >/dev/null \
  || { echo "$target pane $pane is not open; nothing sent" >&2; exit 1; }
# The Claude Code composer is the box from the last prompt-glyph line to the next border
# line; every line in it is draft text. Without a closing border the state is unknown.
composer() {
  wezterm cli get-text --pane-id "$pane" | awk '
    /^[[:space:]]*❯/ { inbox = 1; text = ""; closed = 0; sub(/^[[:space:]]*❯/, ""); text = $0; next }
    inbox && /^[[:space:]]*(─)+[[:space:]]*$/ { inbox = 0; closed = 1; next }
    inbox { text = text " " $0 }
    END { if (!closed) { print "(composer state unknown)"; exit } gsub(/^[[:space:]]+|[[:space:]]+$/, "", text); print text }'
}
draft=$(composer)
[[ -z $draft ]] || { echo "$target pane $pane composer is not empty; nothing sent or logged: ${draft:0:60}" >&2; exit 3; }
[[ -n $transcript && -f $transcript ]] \
  || { echo "no transcript registered for $target pane $pane; delivery cannot be confirmed, nothing sent" >&2; exit 1; }
offset=$(wc -c < "$transcript")
marker="(alert $(uuidgen | tr A-Z a-z | cut -c1-8))"
message="$1 $marker"
if (( log )); then
  printf '%s\n' "$message" >> "$(dirname "$run")/questions.md"
fi
wezterm cli send-text --pane-id "$pane" --no-paste "$message"
wezterm cli send-text --pane-id "$pane" --no-paste $'\r'
# Delivery means a new transcript record, after the send, that is a typed user message
# (string or text blocks, no tool result) containing the marker.
for (( i = 0; i < wait_s; i++ )); do
  sleep 1
  if tail -c +"$(( offset + 1 ))" "$transcript" | python3 -c '
import json, sys
marker = sys.argv[1]
for line in sys.stdin:
    try:
        row = json.loads(line)
    except ValueError:
        continue
    content = (row.get("message") or {}).get("content") if row.get("type") == "user" else None
    if isinstance(content, list) and all(b.get("type") == "text" for b in content):
        content = "".join(b.get("text", "") for b in content)
    if isinstance(content, str) and marker in content:
        sys.exit(0)
sys.exit(1)' "$marker"; then
    echo "delivered to $target pane $pane $marker"
    exit 0
  fi
done
logged=""
(( log )) && logged=" The entry is already in questions.md; retry without --log."
echo "not confirmed: $target pane $pane did not record $marker.$logged" >&2
exit 1
