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
# The recipient's client follows from its transcript: Codex rollouts live under
# .codex/sessions, everything else is Claude Code. The Claude composer is the box from
# the last ❯ line to the next border line; every line in it is draft text, and without
# a closing border the state is unknown. The Codex rule is documented in composer().
codex=0
[[ $transcript == */.codex/sessions/* ]] && codex=1
composer() {
  if (( codex )); then
    # Codex draws its empty-composer placeholder dimmed (SGR 2) and typed text never
    # dimmed. The composer is empty only when its last › line is that dimmed
    # placeholder; any other content there is a draft, so emptiness never depends on
    # parsing draft text. Without a status line below the prompt the state is unknown.
    wezterm cli get-text --pane-id "$pane" --escapes | awk '
      { raw[NR] = $0; plain = $0; gsub(/\033\[[0-9;:]*[A-Za-z]|\033\([A-Z0-9]/, "", plain); line[NR] = plain }
      END {
        for (j = NR; j > 0 && line[j] !~ /^[[:space:]]*›/; j--) {}
        for (i = j + 1; j > 0 && i <= NR && line[i] !~ /^  [^ ?][^·]* · [~\/]/; i++) {}
        if (j < 1 || i > NR) { print "(composer state unknown)"; exit }
        text = line[j]; sub(/^[[:space:]]*›[[:space:]]*/, "", text); sub(/[[:space:]]+$/, "", text)
        if (text == "Ask Codex to do anything" && raw[j] ~ /\033\[(0;)?2m(\033\[[0-9;:]*m)*As/) { print ""; exit }
        for (k = j + 1; k < i; k++) text = text " " line[k]
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", text)
        print (text == "" ? "(draft)" : text)
      }'
    return
  fi
  wezterm cli get-text --pane-id "$pane" | awk '
    { line[NR] = $0 }
    END {
      for (j = NR; j > 0 && line[j] !~ /^[[:space:]]*❯/; j--) {}
      for (i = j + 1; i <= NR && line[i] !~ /^[[:space:]]*(─)+[[:space:]]*$/; i++) {}
      if (j < 1 || i > NR) { print "(composer state unknown)"; exit }
      text = line[j]; sub(/^[[:space:]]*❯/, "", text)
      for (k = j + 1; k < i; k++) text = text " " line[k]
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", text)
      print text
    }'
}
# The live Codex queue is the block between the last "Queued follow-up inputs" heading
# and its "edit last queued message" hint, with only blank lines between the hint and
# the composer. Anything else is history and yields nothing.
queue() {
  wezterm cli get-text --pane-id "$pane" | awk '
    { line[NR] = $0 }
    END {
      for (j = NR; j > 0 && line[j] !~ /^[[:space:]]*›/; j--) {}
      for (h = j - 1; h > 0 && line[h] ~ /^[[:space:]]*$/; h--) {}
      if (h < 1 || line[h] !~ /edit last queued message/) exit
      for (start = h - 1; start > 0 && line[start] !~ /Queued follow-up inputs/; start--) {}
      for (k = start + 1; start > 0 && k < h; k++) print line[k]
    }'
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
# A working Codex turn ignores Enter and keeps the text in the composer; Tab queues it,
# and Codex submits queued input when the turn ends. A queued message is accepted once
# the composer is readable and clear of the marker and the live queue shows it. Both
# the composer and the queue wrap long messages, so marker matches ignore whitespace.
if (( codex )); then
  sleep 1
  compact=${marker// /}
  before=$(composer)
  if [[ ${before//[[:space:]]/} == *"$compact"* ]]; then
    wezterm cli send-text --pane-id "$pane" --no-paste $'\t'
    sleep 1
    after=$(composer)
    if [[ $after != "(composer state unknown)" && ${after//[[:space:]]/} != *"$compact"* ]] \
      && queue | tr -d '[:space:]' | grep -qF -- "$compact"; then
      echo "queued for $target pane $pane $marker"
      exit 0
    fi
  fi
fi
# Delivery means a new transcript record, after the send, that is a typed user message
# containing the marker: a Claude user row (string or text blocks, no tool result) or a
# Codex user message item (input_text blocks).
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
    payload = row.get("payload") if row.get("type") == "response_item" else None
    if isinstance(payload, dict) and payload.get("type") == "message" and payload.get("role") == "user":
        blocks = payload.get("content")
        if isinstance(blocks, list) and all(b.get("type") == "input_text" for b in blocks):
            content = "".join(b.get("text", "") for b in blocks)
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
