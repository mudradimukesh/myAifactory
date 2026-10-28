import glob
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path("/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer")
ART = REPO / "research_notes/Non-Phase-2 integration-20261021"
RUN = ART / "run.json"
PROJECTS = Path.home() / ".claude/projects"
STATE = Path(sys.argv[1])
SUMMARY_S = 900


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def codex_context(path):
    last = None
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 400000))
            for line in f.read().splitlines():
                if b'"token_count"' in line:
                    last = line
    except OSError:
        return None, None
    if not last:
        return None, None
    try:
        info = json.loads(last)["payload"]["info"]
        total = info["total_token_usage"]
        new = total["input_tokens"] - total.get("cached_input_tokens", 0) + total["output_tokens"]
        return info["last_token_usage"]["input_tokens"], new
    except (ValueError, KeyError, TypeError):
        return None, None


def claude_context(session):
    paths = glob.glob(str(PROJECTS / "*" / (session + ".jsonl")))
    if not paths:
        return None
    try:
        with open(paths[0], "rb") as f:
            f.seek(max(0, os.path.getsize(paths[0]) - 400000))
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            u = json.loads(line)["message"]["usage"]
            return u["input_tokens"] + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        except (ValueError, KeyError, TypeError):
            continue
    return None


def reconcile(run_id, run):
    # Records a run's usage as known only under the launcher's own rules for a trustworthy final transcript.
    import subprocess

    sys.path.insert(0, "/tmp/factory-fp-0925/swarm/bin")
    import codex_launch
    from pilot_guard import claude_usage

    receipt = load(ART / (run_id + "-launch.json"), None)
    if not receipt or not run.get("complete") or not run.get("allocation_id"):
        return f"UNKNOWN USAGE {run_id}: no complete allocated receipt, needs a look"
    if (
        not receipt.get("ended_at")
        or receipt.get("status") not in {"REPLY", "HANDOFF", "FAIL", "CAPACITY"}
        or "stop_reason" not in receipt
    ):
        return f"UNKNOWN USAGE {run_id}: receipt not finalized by a completed supervisor, needs a look"
    reservation = load(RUN, {}).get("reservations", {}).get(run["allocation_id"], {})
    if not reservation.get("released") or reservation.get("active_run") not in (None, run_id):
        return f"UNKNOWN USAGE {run_id}: allocation not released, needs a look"
    rollout = Path(receipt.get("rollout_path") or "")

    claude = receipt.get("assignment", {}).get("provider") == "claude"
    reasons = [
        why
        for bad, why in (
            ("remaining_processes" not in receipt or receipt["remaining_processes"], "processes survived the launcher's stop"),
            ("supervision_error" in receipt, "supervision error"),
            (receipt.get("stop_reason") not in (None, *codex_launch.SUPERVISOR_STOPS), "stop " + str(receipt.get("stop_reason"))),
            (receipt.get("stop_reason") is None and receipt.get("child_exit") != 0, "exit " + str(receipt.get("child_exit"))),
            (not rollout.is_file(), "no transcript"),
        )
        if bad
    ]
    if not reasons:
        offset = receipt.get("rollout_offset", 0)
        selected = receipt.get("assignment", {})
        if not codex_launch.transcript_complete(rollout):
            reasons.append("truncated transcript")
        elif (codex_launch.claude_drift if claude else codex_launch.drift)(rollout, offset, selected):
            reasons.append("model drift")
        elif codex_launch.ended_in_error(ART / (run_id + "-events.jsonl"), rollout, offset, claude):
            reasons.append("error result")
    if reasons:
        return f"UNKNOWN USAGE {run_id}: {', '.join(reasons)}, needs a look"
    final = claude_usage(rollout) if claude else codex_launch.usage(rollout)
    start = receipt.get("starting_usage")
    if not final:
        return f"UNKNOWN USAGE {run_id}: unreadable usage, needs a look"
    if claude:
        start = start or {"new_tokens": 0, "cache_reads": 0}
        new, cache = final["new_tokens"] - start["new_tokens"], final["cache_reads"] - start["cache_reads"]
    else:
        start = start or {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        count = lambda u: u["input_tokens"] - u.get("cached_input_tokens", 0) + u["output_tokens"]
        new = count(final) - count(start)
        cache = final.get("cached_input_tokens", 0) - start.get("cached_input_tokens", 0)
    data = {
        "run_id": run_id,
        "allocation_id": run["allocation_id"],
        "thread_id": run.get("thread_id") or receipt.get("thread_id"),
        "new_tokens": new,
        "cache_reads": cache,
        "complete": True,
        "transcript_path": str(rollout),
        "reason": "Monitor auto-reconciliation: complete drift-free transcript, no error result, no live process",
    }
    out = subprocess.run(
        ["python3", "/tmp/factory-fp-0925/swarm/bin/run_state.py", "--run", str(RUN), "--as", "orchestrator", "record-run", "--data", json.dumps(data)],
        capture_output=True,
        text=True,
    )
    if out.returncode:
        return f"UNKNOWN USAGE {run_id}: record-run refused ({out.stderr.strip()[:160]}), needs a look"
    return f"RECONCILED {run_id} usage {new} new / {cache} cache"


def retry_handoff(s, unknown, seen, events):
    # A controller that refused only on unknown usage is retried once that usage is known.
    import subprocess

    token = (s.get("handoff") or {}).get("token")
    if not token or unknown:
        return
    log = ART / "handoff" / (token + "-controller.log")
    try:
        with open(log) as stream:
            identity = os.fstat(stream.fileno())
            # The controller may follow its refusal with a JSON detail line.
            last = [line for line in stream.read().strip().splitlines() if not line.startswith("{")][-1]
    except (OSError, IndexError):
        return
    key = f"{token}:{identity.st_ino}:{identity.st_mtime}"
    if not last.startswith("REFUSED: Usage is unknown") or seen.get("handoff_retry") == key:
        return
    current = load(RUN, {})
    lead = current.get("active_lead") or {}
    if (
        (current.get("handoff") or {}).get("token") != token
        or not current.get("dispatch_frozen")
        or lead.get("model_session_id") != (s.get("active_lead") or {}).get("model_session_id")
        or any(r.get("usage_status") == "unknown" for r in current.get("runs", {}).values())
    ):
        return
    try:
        if os.stat(log).st_ino != identity.st_ino or os.stat(log).st_mtime != identity.st_mtime:
            return
    except OSError:
        return
    pane = str(lead.get("pane"))
    message = (
        f"Monitor: usage is now known for every run, so the controller refusal for handoff {token} is cleared. "
        f"Rerun /tmp/factory-fp-0925/swarm/bin/interior-lead.sh --handoff-start {token} now."
    )
    if seen.get("handoff_text") != key:
        sent = subprocess.run(["wezterm", "cli", "send-text", "--pane-id", pane, "--no-paste", message], capture_output=True)
        if sent.returncode:
            events.append(f"HANDOFF RETRY text to lead pane {pane} for {token} failed; retrying next tick")
            return
        seen["handoff_text"] = key
        time.sleep(1)
    entered = subprocess.run(["wezterm", "cli", "send-text", "--pane-id", pane, "--no-paste", "\r"], capture_output=True)
    if entered.returncode:
        events.append(f"HANDOFF RETRY enter in lead pane {pane} for {token} failed; retrying Enter next tick")
        return
    seen["handoff_retry"] = key
    events.append(f"HANDOFF RETRY sent to lead pane {pane} for {token}")


def main():
    seen = load(STATE, {})
    now = time.time()
    s = load(RUN, None)
    if s is None:
        print("RUN.JSON unreadable", flush=True)
        return
    events = []
    orch = [t for t in s["inventory"].get("threads", []) if t.get("role") == "orchestrator" and not t.get("complete")]
    orch_ctx = orch_new = None
    if orch:
        orch_ctx, orch_new = codex_context(orch[-1].get("transcript_path", ""))
    lead = s.get("active_lead") or {}
    lead_id = lead.get("model_session_id")
    lead_ctx = None
    if lead_id and lead.get("provider") == "codex":
        row = next((r for r in s["inventory"].get("sessions", []) if r.get("session_id") == lead_id), {})
        lead_ctx = codex_context(row.get("transcript_path", ""))[0]
    elif lead_id:
        lead_ctx = claude_context(lead_id)
    reservations = s.get("reservations", {})
    active = {k: r for k, r in reservations.items() if r.get("active_run")}
    builders = [k for k, r in active.items() if r.get("role") == "builder"]
    reviewers = [k for k, r in active.items() if r.get("role") == "reviewer"]
    consumed = s.get("consumed", {})
    ceilings = s.get("ceilings", {})

    for name, ctx, limits in (("orchestrator", orch_ctx, (150000, 200000)), ("lead " + str(lead_id)[:8], lead_ctx, (230000, 250000))):
        if ctx is None:
            continue
        for limit in limits:
            key = f"ctx:{name}:{limit}"
            if ctx > limit and not seen.get(key):
                events.append(f"CONTEXT {name} at {ctx} tokens, above {limit}")
                seen[key] = now
    if lead_id != seen.get("lead"):
        if lead_id:
            events.append(f"LEAD now {lead_id[:8]} pid {lead.get('pid')} pane {lead.get('pane')}")
        seen["lead"] = lead_id
        seen["lead_since"] = now
    for k, r in reservations.items():
        if r.get("active_run") and r.get("observed", 0) >= 0.9 * r.get("allowance", 1):
            key = "budget:" + k
            if not seen.get(key):
                events.append(f"ALLOWANCE {k} at {r['observed']} of {r['allowance']}")
                seen[key] = now
    unknown = [k for k, r in s.get("runs", {}).items() if r.get("usage_status") == "unknown"]
    for run_id in list(unknown):
        result = reconcile(run_id, s["runs"][run_id])
        if result != seen.get("rc:" + run_id):
            events.append(result)
            seen["rc:" + run_id] = result
        if result.startswith("RECONCILED"):
            unknown.remove(run_id)
    retry_handoff(s, unknown, seen, events)
    if sorted(unknown) != seen.get("unknown", []):
        if unknown:
            events.append("UNKNOWN USAGE " + ", ".join(unknown))
        seen["unknown"] = sorted(unknown)
    for key in ("overall", "pilot"):
        if key in ceilings and consumed.get(key, 0) > 0.85 * ceilings[key] and not seen.get("ceil:" + key):
            events.append(f"BUDGET {key} {consumed[key]} of {ceilings[key]}")
            seen["ceil:" + key] = now
    draining = s.get("draining_leads", {})
    for sid, d in draining.items():
        first = seen.setdefault("drain:" + sid, now)
        if now - first > 600 and not seen.get("drainwarn:" + sid):
            events.append(f"DRAIN {sid[:8]} still draining after {int((now - first) / 60)} min, status {d.get('status')}")
            seen["drainwarn:" + sid] = now
    if s.get("dispatch_frozen"):
        first = seen.setdefault("frozen", now)
        if now - first > 900 and not seen.get("frozenwarn"):
            events.append(f"FROZEN dispatch frozen for {int((now - first) / 60)} min, handoff {bool(s.get('handoff'))}")
            seen["frozenwarn"] = now
    else:
        seen.pop("frozen", None)
        seen.pop("frozenwarn", None)
    if lead.get("pid"):
        try:
            os.kill(int(lead["pid"]), 0)
            seen.pop("leaddead", None)
        except OSError:
            if not seen.get("leaddead"):
                events.append(f"LEAD DEAD {str(lead_id)[:8]} pid {lead['pid']} pane {lead.get('pane')} exited")
                seen["leaddead"] = now
    if lead.get("pane"):
        import subprocess
        text = subprocess.run(["wezterm", "cli", "get-text", "--pane-id", str(lead["pane"])], capture_output=True, text=True).stdout
        if "esc to interrupt" in text[-3000:] or re.search(r"…\s*\(\d+[smh]", text[-3000:]):
            seen.pop("leadquiet", None)
        else:
            first = seen.setdefault("leadquiet", now)
            if now - first > 300 and now - seen.get("leadquietwarn", 0) > 900:
                events.append(f"LEAD STOPPED {str(lead_id)[:8]} pane {lead['pane']} not working for {int((now - first) / 60)} min")
                seen["leadquietwarn"] = now
    if lead_id and not s.get("dispatch_frozen") and len(builders) < 8:
        first = seen.setdefault("idle", now)
        if now - first > 1200 and now - seen.get("idlewarn", 0) > 1800:
            events.append(f"IDLE SLOTS only {len(builders)} of 8 builders running for {int((now - first) / 60)} min")
            seen["idlewarn"] = now
    else:
        seen.pop("idle", None)
    blockers = [b["id"] for b in s.get("blockers", []) if b.get("status", "open") == "open"]
    new_blockers = [b for b in blockers if b not in seen.get("blockers", [])]
    if new_blockers:
        events.append("BLOCKER opened " + ", ".join(new_blockers))
    seen["blockers"] = blockers
    if s.get("lifecycle") != "running" and seen.get("lifecycle") != s.get("lifecycle"):
        events.append(f"LIFECYCLE {s.get('lifecycle')}: {s.get('lifecycle_reason', '')[:120]}")
    seen["lifecycle"] = s.get("lifecycle")
    try:
        size = (ART / "questions.md").stat().st_size
    except OSError:
        size = 0
    if size != seen.get("qsize"):
        if seen.get("qsize"):
            with open(ART / "questions.md", "rb") as f:
                f.seek(seen["qsize"] if size > seen["qsize"] else 0)
                added = f.read().decode(errors="replace")
            flagged = [line.strip()[:200] for line in added.splitlines() if any(w in line for w in ("BLOCKER", "REFUSED", "needs_ruling", "user decision", "denial"))]
            reported = seen.setdefault("qreported", [])
            fresh = [line for line in flagged if line not in reported]
            for line in fresh[:3]:
                events.append("QUESTIONS " + line)
            seen["qreported"] = (reported + fresh)[-200:]
        seen["qsize"] = size
    if orch:
        tid = orch[-1]["thread_id"]
        flagged = False
        try:
            flagged = any(
                line.startswith("ORCH-HANDOFF") and tid in line
                for line in (ART / "questions.md").read_text().splitlines()
            )
        except OSError:
            pass
        if (flagged or (orch_ctx or 0) > 135000) and seen.get("handed") != tid:
            seen["handed"] = tid
            import subprocess
            out = subprocess.run(["/bin/zsh", str(STATE.parent / "orch_handoff.sh"), "--retire-only"], capture_output=True, text=True)
            events.append((out.stdout.strip() or out.stderr.strip())[-300:] + (" (requested)" if flagged else " (backstop at %s)" % orch_ctx))
    if now - seen.get("summary", 0) > SUMMARY_S:
        events.append(
            "SUMMARY consumed overall {}/{} pilot {}/{} | orchestrator ctx {} new {} | lead {} ctx {} | builders {} reviewers {} | draining {} | frozen {}".format(
                consumed.get("overall"), ceilings.get("overall"), consumed.get("pilot"), ceilings.get("pilot"),
                orch_ctx, orch_new, str(lead_id)[:8], lead_ctx, len(builders), len(reviewers), len(draining), s.get("dispatch_frozen"),
            )
        )
        seen["summary"] = now
    STATE.write_text(json.dumps(seen))
    for e in events:
        print(time.strftime("%H:%M ") + e, flush=True)


if __name__ == "__main__":
    main()
