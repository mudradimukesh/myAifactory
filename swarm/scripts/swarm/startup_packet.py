"""Print the current swarm state a new orchestrator or lead needs at startup.

The packet goes into the successor's launch prompt, so the successor does not spend
dozens of tool calls querying run.json, listing ART or searching questions.md.
Each packet is generated for one successor KEY (its session id or handoff token) and
records the questions.md entries it carried in packet-<role>-pending-<KEY>.json. The
launcher commits exactly that packet with --commit KEY after the successor acknowledges
it, so a failed launch or a later packet never marks undelivered entries delivered.
Entries are tracked by content hash, so an edited entry counts as new.
Usage: startup_packet.py ART orchestrator|lead KEY | ART orchestrator|lead --commit KEY
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

IDS = r"RULING\w+|Q-LEAD-[\w-]+|Q-lead-[\w-]+|BLOCKER\w+|DRAIN_COMPLETE\S*|CAP\S*"
ENTRY = re.compile(r"^(?:#{1,6} |- )?(" + IDS + ")")
QUESTION = re.compile(r"^Q-(?:LEAD|lead)-[0-9a-f]{8}-\d+")
ANSWERS = re.compile(r"\banswers\s+(Q-[^)]*)")
QUESTION_ID = re.compile(r"Q-(?:LEAD|lead)-[0-9a-f]{8}-\d+")
RECENT = 48 * 3600
ENTRY_CHARS = 400
MAX_ENTRIES = 60
EVIDENCE = (
    "acceptance.md",
    "ids.txt",
    "test-map.json",
    "s1-tests.json",
    "red.json",
    "candidate.json",
    "gate.json",
    "code-review.json",
    "product-review.json",
    "open-findings.json",
)


def clip(text, limit):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def present(pid):
    # PID presence only; exact identity and exit are verified by the owning tools.
    try:
        return subprocess.run(["ps", "-p", str(int(pid))], capture_output=True).returncode == 0
    except (TypeError, ValueError):
        return False


def worktree(path):
    if not Path(path).exists():
        return "missing"

    def git(*args):
        result = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else "?"

    dirty = git("status", "--porcelain", "--untracked-files=no")
    changes = len(dirty.splitlines()) if dirty != "?" else "?"
    branch = git("branch", "--show-current") or "detached"
    return f"{git('rev-parse', '--short', 'HEAD')} {branch}, {changes} tracked changes"


def ledger(art):
    run = json.loads((art / "run.json").read_text())
    lines = ["## Ledger (pid flags show PID presence only, not verified identity)"]
    lead = run.get("active_lead") or {}
    lines.append(
        f"- Active lead: session {lead.get('model_session_id')}, pid {lead.get('pid')} "
        f"({'present' if present(lead.get('pid')) else 'absent'}), pane {lead.get('pane')}"
    )
    orchestrators = [t for t in run["inventory"]["threads"] if t.get("role") == "orchestrator"]
    if orchestrators:
        o = orchestrators[-1]
        lines.append(f"- Newest orchestrator: pane {o.get('pane_id')}, pid {o.get('pid')}")
    lines.append(
        f"- lifecycle {run.get('lifecycle')}, dispatch_frozen {run.get('dispatch_frozen')}, "
        f"handoff {'open' if run.get('handoff') else 'none'}, deadlines {run.get('deadlines')}, "
        f"consumed {run.get('consumed')}, ceilings {run.get('ceilings')}"
    )
    lines.append("")
    lines.append("## Lead scopes")
    for session, scopes in (run.get("lead_scopes") or {}).items():
        if session != lead.get("model_session_id") or not scopes:
            continue
        for scope in scopes:
            lines.append(f"- {scope}: {worktree(scope)}")
    for session, drain in (run.get("draining_leads") or {}).items():
        jobs = [j.get("pid") for j in drain.get("jobs", [])]
        lines.append(
            f"- Draining lead {session[:8]} (pid {(drain.get('lead') or {}).get('pid')}): "
            f"status {drain.get('status')}, scopes {drain.get('scopes')}, job pids {jobs}, "
            f"completion {'yes' if drain.get('completion') else 'no'}"
        )
    lines.append("")
    lines.append("## Running allocations")
    running = {k: r for k, r in run["reservations"].items() if not r.get("released")}
    for key, r in running.items():
        receipt = art / f"{r.get('active_run') or key}-launch.json"
        pid = json.loads(receipt.read_text()).get("pid") if receipt.is_file() else None
        lines.append(
            f"- {key}: {r.get('role')}, run {r.get('active_run')}, observed {r.get('observed')}/"
            f"{r.get('allowance')}, pid {pid} ({'present' if present(pid) else 'absent'}), "
            f"worktree {r.get('worktree')}"
        )
    if not running:
        lines.append("- none")
    lines.append("")
    lines.append("## Open blockers")
    open_blockers = [b for b in run.get("blockers", []) if b.get("status", "open") == "open"]
    lines += [f"- {b.get('id')}: {clip(b.get('reason', ''), 240)}" for b in open_blockers] or [
        "- none"
    ]
    return lines


def processes():
    rows = subprocess.run(["ps", "-axo", "pid=,etime=,command="], capture_output=True, text=True)
    hits = [
        line.strip()
        for line in rows.stdout.splitlines()
        if re.search(
            r"unit_gate\.py|red_check\.py|merge_unit|codex_launch\.py|orchestrator_watchdog", line
        )
    ]
    return ["## Swarm processes"] + ([f"- {clip(h, 220)}" for h in hits] or ["- none"])


def evidence(art):
    # Unfinished candidates appear whatever their age; other features only when recent.
    # Other JSON artifacts can share the suffix (a diff-lint findings list); only an
    # object is a candidate record.
    now = time.time()
    features = []
    for path in sorted(art.glob("*-candidate.json")):
        try:
            record = json.loads(path.read_text())
        except ValueError:
            record = {"status": "unreadable"}
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        if status != "pushed" or now - path.stat().st_mtime < RECENT:
            features.append(path.name[: -len("-candidate.json")])
    features += sorted(
        p.name[: -len("-acceptance.md")]
        for p in art.glob("*-acceptance.md")
        if now - p.stat().st_mtime < RECENT and p.name[: -len("-acceptance.md")] not in features
    )
    lines = ["## Unfinished or recent features (ART/<feature>-<file>)"]
    for feature in features:
        has = [name for name in EVIDENCE if (art / f"{feature}-{name}").is_file()]
        missing = [name for name in EVIDENCE[:3] if name not in has]
        detail = ""
        candidate = art / f"{feature}-candidate.json"
        try:
            c = json.loads(candidate.read_text()) if candidate.is_file() else None
        except ValueError:
            c = {"status": "unreadable"}
        if isinstance(c, dict):
            detail = (
                f" candidate attempt {c.get('attempt')} {str(c.get('candidate_sha'))[:8]} "
                f"status {c.get('status')}, worktree {c.get('worktree')};"
            )
        gate = art / f"{feature}-gate.json"
        try:
            g = json.loads(gate.read_text()) if gate.is_file() else None
        except ValueError:
            g = None
            detail += " gate unreadable;"
        if isinstance(g, dict):
            failed = [r.get("id") for r in g.get("gates", []) if r.get("exit_code")]
            detail += f" gate {str(g.get('candidate_sha'))[:8]} failed {failed or 'none'};"
        lines.append(
            f"- {feature}:{detail} has {', '.join(has) or 'nothing'}"
            + (f"; missing {', '.join(missing)}" if missing else "")
        )
    if not features:
        lines.append("- none")
    for name in ("PAUSE", "inbox.md"):
        path = art / name
        if path.is_file():
            lines.append(f"- ART/{name}: {clip(path.read_text(errors='replace'), 600)}")
    return lines


def entries(path):
    found, current = [], None
    for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        match = ENTRY.match(line)
        if match:
            current = {"line": number, "id": match.group(1), "text": line}
            found.append(current)
        elif current is not None:
            current["text"] += "\n" + line
    for entry in found:
        entry["hash"] = hashlib.sha256(entry["text"].strip().encode()).hexdigest()[:16]
    return found


def write_atomic(path, data):
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data))
    os.replace(temp, path)


def questions(art, role, key):
    found = entries(art / "questions.md")
    logs = art / "logs"
    delivered_path = logs / f"packet-{role}-delivered.json"
    lines = [f"## questions.md ({len(found)} entries; line numbers in brackets)"]
    if delivered_path.is_file():
        delivered = set(json.loads(delivered_path.read_text()))
    else:
        # First packet for this role: entries before the newest MAX_ENTRIES are history.
        history = found[:-MAX_ENTRIES]
        delivered = {e["hash"] for e in history}
        if history:
            lines.append(
                f"Entries up to line {history[-1]['line']} predate packet tracking and are "
                "treated as history."
            )
    # Only a ruling that says "answers Q-..." answers a question; a mention does not.
    answered = set()
    for entry in found:
        if not entry["id"].startswith("RULING"):
            continue
        for clause in ANSWERS.findall(entry["text"]):
            answered.update(QUESTION_ID.findall(clause))
    unanswered = [
        e for e in found if QUESTION.match(e["id"]) and e["id"].rstrip(":,") not in answered
    ]
    lines.append("Unanswered lead questions (no entry says it answers them):")
    lines += [f"- [{e['line']}] {clip(e['text'], ENTRY_CHARS)}" for e in unanswered[-8:]] or [
        "- none"
    ]
    new = [e for e in found if e["hash"] not in delivered]
    shown, held = new[:MAX_ENTRIES], new[MAX_ENTRIES:]
    lines.append(f"New or changed entries since the last acknowledged {role} packet:")
    lines += [f"- [{e['line']}] {clip(e['text'], ENTRY_CHARS)}" for e in shown] or ["- none"]
    if held:
        lines.append(
            f"- {len(held)} more not shown here (lines {held[0]['line']}-{held[-1]['line']}); "
            "they stay unacknowledged and appear in the next packet: "
            + ", ".join(f"[{e['line']}] {e['id']}" for e in held)
        )
    logs.mkdir(parents=True, exist_ok=True)
    # A first packet also carries the history hashes, so committing it starts the record.
    history = set() if delivered_path.is_file() else delivered
    pending = sorted({e["hash"] for e in shown} | history)
    if key:
        write_atomic(logs / f"packet-{role}-pending-{key}.json", pending)
    return lines


def commit(art, role, key):
    """Add the entries of the packet generated for KEY to the delivered set."""
    logs = Path(art) / "logs"
    pending = logs / f"packet-{role}-pending-{key}.json"
    if not pending.is_file():
        raise ValueError("no pending packet for " + key)
    delivered = logs / f"packet-{role}-delivered.json"
    with (logs / f"packet-{role}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        known = set(json.loads(delivered.read_text())) if delivered.is_file() else set()
        write_atomic(delivered, sorted(known | set(json.loads(pending.read_text()))))
        pending.unlink()


def render(art, role, key=None):
    art = Path(art)
    out = [
        f"# Startup packet for the new {role}, generated {time.strftime('%Y-%m-%d %H:%M %Z')}",
        "This is current state. Do not rediscover it with run.json queries, ART listings, "
        "questions.md searches, ps or git status. Read a file only for a detail this packet "
        "does not give, using the exact path or line number shown here. Verify exact process "
        "identity and exit with the owning tools before acting on a pid.",
        "",
    ]
    for section in (ledger, processes, evidence, questions):
        try:
            if section is questions:
                lines = section(art, role, key)
            elif section is processes:
                lines = section()
            else:
                lines = section(art)
        except (OSError, ValueError, KeyError, TypeError) as error:
            lines = [f"## {section.__name__}: unavailable ({error!r}); check it directly"]
        out += [*lines, ""]
    return "\n".join(out)


if __name__ == "__main__":
    usage = "usage: startup_packet.py ART orchestrator|lead KEY | ART ROLE --commit KEY"
    if len(sys.argv) < 4 or sys.argv[2] not in {"orchestrator", "lead"}:
        sys.exit(usage)
    if len(sys.argv) == 5 and sys.argv[3] == "--commit":
        commit(sys.argv[1], sys.argv[2], sys.argv[4])
    elif len(sys.argv) == 4:
        print(render(sys.argv[1], sys.argv[2], sys.argv[3]))
    else:
        sys.exit(usage)
