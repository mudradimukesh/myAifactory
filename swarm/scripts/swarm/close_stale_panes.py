#!/usr/bin/env python3
"""Close recorded retired lead and completed orchestrator panes. Preview by default.

Finish native retirement and final accounting first. This command does not retire
sessions, change the ledger, or retry denied operations.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

RUN = Path("/private/tmp/factory-fp-0925/swarm/interior-p1-poc/run.json")


def pane_id(value):
    return int(value) if value is not None else -1


def eligible_panes(run):
    keep = {pane_id((run.get("active_lead") or {}).get("pane"))}
    keep.add(pane_id(os.environ.get("WEZTERM_PANE")))
    for drain in (run.get("draining_leads") or {}).values():
        keep.add(pane_id(drain.get("lead", {}).get("pane")))
    # A pending transfer must never lose either participant.
    transfer = run.get("handoff") or {}
    for key in ("predecessor", "successor"):
        keep.add(pane_id((transfer.get(key) or {}).get("pane")))
    candidates = set()
    for row in run.get("drain_history", []):
        if row.get("retired_at") and row.get("receipt"):
            candidates.add(pane_id(row.get("lead", {}).get("pane")))
    for row in run.get("retirement_history", []):
        if row.get("completed_at"):
            candidates.add(pane_id(row.get("predecessor", {}).get("pane")))
    threads = run.get("inventory", {}).get("threads", [])
    orchestrators = [row for row in threads if row.get("role") == "orchestrator"]
    if orchestrators:
        keep.add(pane_id(orchestrators[-1].get("pane_id")))
    for row in threads:
        pane = pane_id(row.get("pane_id"))
        if row.get("role") == "orchestrator" and row.get("complete") is True:
            candidates.add(pane)
        elif row.get("complete") is not True and (
            row.get("role") != "lead" or pane not in candidates
        ):
            keep.add(pane)
    return candidates - keep - {-1}


def wezterm(*args):
    return subprocess.run(
        ["wezterm", "cli", *args], check=True, capture_output=True, text=True, timeout=15
    ).stdout


def listing():
    return {int(row["pane_id"]): row for row in json.loads(wezterm("list", "--format", "json"))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=RUN)
    parser.add_argument("--pane-id", type=int, action="append", help="restrict to these panes")
    parser.add_argument("--close", action="store_true", help="close eligible panes")
    args = parser.parse_args()
    try:
        candidates = eligible_panes(json.loads(args.run.read_text()))
        if args.pane_id:
            refused = set(args.pane_id) - candidates
            if refused:
                raise ValueError(
                    f"Panes lack completed retirement or are protected: {sorted(refused)}"
                )
            candidates &= set(args.pane_id)
        panes = listing()
        for pane in sorted(candidates & panes.keys()):
            if args.close:
                # Recheck ownership immediately before each destructive operation.
                if pane not in eligible_panes(json.loads(args.run.read_text())):
                    raise ValueError(f"Pane {pane} is no longer eligible")
                wezterm("kill-pane", "--pane-id", str(pane))
                if pane in listing():
                    raise ValueError(f"Pane {pane} still exists after close; no retry attempted")
            verb = "closed" if args.close else "would close"
            print(f"{verb} pane {pane} tab {panes[pane]['tab_id']}")
        if not candidates & panes.keys():
            print("No eligible retired panes are open.")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Close blocked: {error}\n")


if __name__ == "__main__":
    main()
