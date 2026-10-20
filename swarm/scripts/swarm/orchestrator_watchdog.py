#!/usr/bin/env python3
"""Wake the registered orchestrator pane when the swarm stalls.

Runs outside any Claude session (nohup), so a stalled lead or an orchestrator
that ended its turn with nothing armed still gets woken.
Usage: nohup python3 scripts/swarm/orchestrator_watchdog.py RUN_DIR >/dev/null 2>&1 &
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

INTERVAL = 120
LEAD_IDLE = 600
REPEAT = 900
REPEAT_MAX = 7200
HEARTBEAT = 1800
USER_ALERT = 1800
DAEMON_LOG = Path.home() / ".claude" / "daemon.log"
PROMPT = "\u276f "  # Claude Code composer prompt


def wez(*args):
    return subprocess.run(
        ["wezterm", "cli", *args], capture_output=True, text=True, timeout=30
    ).stdout


def pane_tail(pane, lines=10):
    return [line for line in wez("get-text", "--pane-id", str(pane)).splitlines() if line.strip()][
        -lines:
    ]


def draft(tail):
    """Return composer text if the prompt line holds unsent input."""
    # The composer is the last prompt line; earlier ones are submitted history.
    for line in reversed(tail):
        stripped = line.strip()
        if stripped.startswith(PROMPT.strip()):
            return stripped[1:].strip() or None
    return None


def notify_user(message):
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification {json.dumps(message)} with title "P1 swarm stalled"',
        ],
        capture_output=True,
        timeout=30,
    )


def main(run_dir):
    run_path = run_dir / "run.json"
    questions = run_dir / "questions.md"
    log = (run_dir / "logs" / "orchestrator-watchdog.log").open("a", buffering=1)
    pidfile = run_dir / "logs" / "orchestrator-watchdog.pid"
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text()), 0)
            sys.exit(f"watchdog already running as pid {pidfile.read_text().strip()}")
        except (ProcessLookupError, ValueError):
            pass
    pidfile.write_text(f"{os.getpid()}\n")
    lead_hash, lead_since = None, time.time()
    last_draft = None
    last_q = None
    daemon_seen = DAEMON_LOG.stat().st_size if DAEMON_LOG.exists() else 0
    last_reasons, last_sent = None, 0.0
    repeat, stalled_since, alerted, alert_gap = REPEAT, None, 0.0, USER_ALERT
    while True:
        try:
            run = json.loads(run_path.read_text())
            panes = {str(p["pane_id"]) for p in json.loads(wez("list", "--format", "json"))}
            orchs = [
                t
                for t in run["inventory"]["threads"]
                if t.get("role") == "orchestrator" and str(t.get("pane_id")) in panes
            ]
            orch_pane = str(orchs[-1]["pane_id"]) if orchs else None
            lead_pane = str((run.get("active_lead") or {}).get("pane") or "")
            reasons = []

            if not any(not r.get("released") for r in run["reservations"].values()):
                reasons.append("no builder or reviewer slot is running")

            if lead_pane in panes:
                tail = pane_tail(lead_pane)
                digest = hashlib.sha256("\n".join(tail).encode()).hexdigest()
                if digest != lead_hash:
                    lead_hash, lead_since = digest, time.time()
                elif time.time() - lead_since >= LEAD_IDLE:
                    idle = int((time.time() - lead_since) // 60)
                    reasons.append(f"lead pane {lead_pane} unchanged for {idle} min")
                text = draft(tail)
                if text and text == last_draft:
                    reasons.append(f"lead pane {lead_pane} holds unsent text: {text[:80]}")
                last_draft = text
            else:
                reasons.append(f"active lead pane {lead_pane or '?'} is not open")

            count = sum(
                1
                for line in questions.read_text(errors="replace").splitlines()
                if line.startswith(("Q-LEAD", "BLOCKER", "CAP", "DRAIN_COMPLETE"))
            )
            if last_q is not None and count > last_q:
                reasons.append(f"{count - last_q} new lead entry in questions.md")
            last_q = count

            # A slash path can move a live session into a daemon spare; its hooks and
            # shells then lose the launch environment (RULING575i).
            if DAEMON_LOG.exists():
                with DAEMON_LOG.open() as daemon:
                    daemon.seek(daemon_seen if DAEMON_LOG.stat().st_size >= daemon_seen else 0)
                    for line in daemon:
                        if "claimed-spare" in line and "(slash)" in line:
                            reasons.append("session moved into daemon: " + line.strip()[-60:])
                    daemon_seen = daemon.tell()

            now = time.time()
            # Idle minutes change every pass; compare reasons without numbers so REPEAT applies.
            key = [re.sub(r"\d+", "N", r) for r in reasons]
            # An unchanged stall gets a doubling repeat interval: the orchestrator already
            # reported it, and each wake rereads its whole context for a "no change" reply.
            if key != last_reasons:
                repeat = REPEAT
            due = (reasons and (key != last_reasons or now - last_sent >= repeat)) or (
                not reasons and now - last_sent >= HEARTBEAT
            )
            stamp = datetime.now().strftime("%H:%M")
            stalled = any(r.startswith("no builder") for r in reasons)
            if not stalled:
                stalled_since, alert_gap = None, USER_ALERT
            stalled_since = stalled_since or (now if stalled else None)
            if stalled and now - stalled_since >= USER_ALERT and now - alerted >= alert_gap:
                minutes = int((now - stalled_since) // 60)
                notify_user(f"No builder for {minutes} min. Check the orchestrator pane.")
                if alerted:
                    alert_gap = min(alert_gap * 2, REPEAT_MAX)
                alerted = now
                log.write(f"{stamp} notified user: stalled {minutes} min\n")
            if due and orch_pane:
                if draft(pane_tail(orch_pane)):
                    log.write(f"{stamp} skip: orchestrator pane {orch_pane} has typed input\n")
                else:
                    message = (
                        f"WATCHDOG {stamp}: " + "; ".join(reasons) + ". Check and act."
                        if reasons
                        else f"WATCHDOG {stamp}: heartbeat. Run the stall check."
                    )
                    wez("send-text", "--pane-id", orch_pane, "--no-paste", message)
                    wez("send-text", "--pane-id", orch_pane, "--no-paste", "\r")
                    time.sleep(3)
                    wez("send-text", "--pane-id", orch_pane, "--no-paste", "\r")
                    if key == last_reasons:
                        repeat = min(repeat * 2, REPEAT_MAX)
                    last_reasons, last_sent = key, now
                    log.write(f"{stamp} sent to pane {orch_pane}: {message}\n")
            elif due:
                log.write(f"{stamp} no open orchestrator pane: {reasons}\n")
        except Exception as error:  # keep watching through transient failures
            log.write(f"{datetime.now():%H:%M} error: {error!r}\n")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
