import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/swarm/orchestrator-spawn.sh"
STUB = (
    "import signal, sys, time\n"
    "if 'ignore' in sys.argv: signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "time.sleep(60)\n"
)


def lstart(pid):
    out = subprocess.check_output(["ps", "-o", "lstart=", "-p", str(pid)], text=True)
    return " ".join(out.split())


@pytest.fixture
def spawn(tmp_path):
    home = tmp_path / "home"
    (home / ".claude/sessions").mkdir(parents=True)
    art = tmp_path / "art"
    (art / "logs").mkdir(parents=True)
    (art / "questions.md").write_text("")
    # A live pid stands in for the watchdog so the script does not start a real one.
    (art / "logs/orchestrator-watchdog.pid").write_text(str(os.getpid()))
    (art / "run.json").write_text(
        json.dumps(
            {
                "lifecycle": "running",
                "blockers": [],
                "inventory": {"threads": [], "sessions": []},
                "reservations": {},
                "runs": {},
                "orchestrator_usage_migrated": "x",
                "consumed": {"overall": 0, "pilot": 0},
                "ceilings": {"overall": 1000000, "pilot": 1000000},
            }
        )
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "wezterm-calls"
    (bin_dir / "wezterm").write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    (bin_dir / "wezterm").chmod(0o755)
    procs = []

    def process(*args):
        proc = subprocess.Popen([sys.executable, "-c", STUB, *args])
        procs.append(proc)
        return proc

    def predecessor(ignore_term=False):
        session = str(uuid.uuid4())
        proc = process("ignore") if ignore_term else process()
        import time

        time.sleep(0.3)
        (home / ".claude/sessions" / f"{proc.pid}.json").write_text(
            json.dumps({"sessionId": session, "procStart": lstart(proc.pid)})
        )
        return proc, session

    def run(session, old=None, old_session="", ready=None, wait=3):
        hash_ = "h" * 64
        if ready is not None:
            (art / "logs/orchestrator-7-ready.json").write_text(ready)
        mkdir = art / "logs/orchestrator-spawns.log.lock"
        mkdir.mkdir(exist_ok=True)
        env = dict(
            os.environ,
            HOME=str(home),
            ART=str(art),
            REPO=str(ROOT),
            PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
            SPAWN_PID_WAIT="3",
            SPAWN_READY_WAIT=str(wait),
            SPAWN_EXIT_WAIT="2",
        )
        result = subprocess.run(
            [
                "/bin/zsh",
                str(SCRIPT),
                "--detached",
                "7",
                session,
                "41",
                hash_,
                str(old.pid) if old else "",
                "40" if old else "",
                old_session,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        log = (art / "logs/orchestrator-spawns.log").read_text()
        closed = calls.exists() and "kill-pane --pane-id 40" in calls.read_text()
        return result.returncode, log, closed

    def good_receipt(session):
        return json.dumps({"session_id": session, "handoff_sha256": "h" * 64})

    yield run, process, predecessor, good_receipt, art
    for proc in procs:
        proc.kill()


def successor(process):
    session = str(uuid.uuid4())
    return process("--session-id", session), session


def test_ready_successor_retires_predecessor(spawn):
    run, process, predecessor, receipt, art = spawn
    _, session = successor(process)
    old, old_session = predecessor()
    projects = art.parent / "home/.claude/projects" / str(ROOT).replace("/", "-").replace("_", "-")
    projects.mkdir(parents=True)
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 5, "output_tokens": 2}
    row = {"message": {"id": "m1", "usage": {**usage, "cache_read_input_tokens": 100}}}
    (projects / (old_session + ".jsonl")).write_text(json.dumps(row) + "\n")
    state = json.loads((art / "run.json").read_text())
    state["inventory"]["threads"].append(
        {"thread_id": old_session, "session_id": old_session, "role": "orchestrator"}
    )
    state["runs"]["orchestrator-" + old_session] = {
        "thread_id": old_session,
        "role": "orchestrator",
        "new_tokens": 0,
        "cache_reads": 0,
        "stage": "pilot",
    }
    (art / "run.json").write_text(json.dumps(state))
    code, log, closed = run(session, old, old_session, ready=receipt(session))
    # The pane close is simulated, so the script must fall back to TERM and see the exit.
    assert closed
    assert old.wait(timeout=10) == -signal.SIGTERM
    assert "registration done" in log and "ready: receipt" in log
    assert "predecessor retirement done" in log, log
    assert code == 0
    final = json.loads((art / "run.json").read_text())["runs"]["orchestrator-" + old_session]
    assert (final["new_tokens"], final["cache_reads"], final["complete"]) == (17, 100, True)


def test_registration_refused_keeps_predecessor(spawn):
    run, process, predecessor, receipt, art = spawn
    _, session = successor(process)
    state = json.loads((art / "run.json").read_text())
    state["inventory"]["threads"].append({"thread_id": session, "role": "lead"})
    (art / "run.json").write_text(json.dumps(state))
    old, old_session = predecessor()
    code, log, closed = run(session, old, old_session, ready=receipt(session))
    assert code == 1 and not closed and old.poll() is None
    assert "registration NOT done" in log


def test_missing_ledger_keeps_predecessor(spawn):
    run, process, predecessor, receipt, art = spawn
    _, session = successor(process)
    (art / "run.json").unlink()
    old, old_session = predecessor()
    code, _, closed = run(session, old, old_session, ready=receipt(session))
    assert code == 1 and not closed and old.poll() is None


@pytest.mark.parametrize("ready", [None, "wrong"])
def test_successor_alive_but_not_ready_keeps_predecessor(spawn, ready):
    run, process, predecessor, _, _ = spawn
    _, session = successor(process)
    old, old_session = predecessor()
    text = None if ready is None else json.dumps({"session_id": "other", "handoff_sha256": "x"})
    code, log, closed = run(session, old, old_session, ready=text, wait=2)
    assert code == 1 and not closed and old.poll() is None
    assert "no valid readiness receipt" in log


def test_wrong_predecessor_identity_is_not_closed(spawn):
    run, process, predecessor, receipt, _ = spawn
    _, session = successor(process)
    old, _ = predecessor()
    code, log, closed = run(session, old, str(uuid.uuid4()), ready=receipt(session))
    assert code == 1 and not closed and old.poll() is None
    assert "is not session" in log


def test_predecessor_ignoring_term_gets_no_final_usage(spawn):
    run, process, predecessor, receipt, art = spawn
    _, session = successor(process)
    old, old_session = predecessor(ignore_term=True)
    code, log, closed = run(session, old, old_session, ready=receipt(session))
    assert code == 1 and closed and old.poll() is None
    assert "still running after pane close and TERM; final usage NOT recorded" in log
    runs = json.loads((art / "run.json").read_text())["runs"]
    assert "orchestrator-" + old_session not in runs


def inventory_row(art, session):
    rows = json.loads((art / "run.json").read_text())["inventory"]["threads"]
    return next((r for r in rows if r.get("thread_id") == session), None)


def test_receipt_then_successor_exit_keeps_predecessor(spawn):
    run, _, predecessor, receipt, art = spawn
    session = str(uuid.uuid4())
    ready = art / "logs/orchestrator-7-ready.json"
    # The successor writes a valid receipt and exits at once.
    code = (
        "import sys, time\n"
        f"time.sleep(0.5); open({str(ready)!r}, 'w').write({receipt(session)!r})\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, "--session-id", session])
    old, old_session = predecessor()
    code, log, closed = run(session, old, old_session)
    proc.wait()
    assert code == 1 and not closed and old.poll() is None
    assert "is not running" in log
    assert inventory_row(art, session)["complete"] is True


def test_timeout_stops_successor_and_restores_routing(spawn):
    run, process, predecessor, _, art = spawn
    proc, session = successor(process)
    old, old_session = predecessor()
    code, log, closed = run(session, old, old_session, wait=2)
    assert code == 1 and not closed and old.poll() is None
    assert proc.wait(timeout=10) == -signal.SIGTERM
    assert "routing restored to the predecessor" in log
    assert inventory_row(art, session)["complete"] is True


def test_partial_registration_failure_is_recoverable(spawn):
    run, process, predecessor, receipt, art = spawn
    proc, session = successor(process)
    state = json.loads((art / "run.json").read_text())
    # An existing accounting run for this identity makes record-run refuse.
    state["runs"]["other"] = {"thread_id": session, "role": "orchestrator"}
    (art / "run.json").write_text(json.dumps(state))
    old, old_session = predecessor()
    code, log, closed = run(session, old, old_session, ready=receipt(session))
    assert code == 1 and not closed and old.poll() is None
    assert "refused the accounting run" in log
    assert "RECOVERY NEEDED" in log
    assert proc.wait(timeout=10) == -signal.SIGTERM


def test_successor_surviving_close_and_term_stays_incomplete(spawn):
    run, process, predecessor, _, art = spawn
    session = str(uuid.uuid4())
    # The fake pane close kills nothing, and this successor ignores TERM.
    proc = process("ignore", "--session-id", session)
    old, old_session = predecessor()
    code, log, closed = run(session, old, old_session, wait=2)
    assert code == 1 and not closed
    assert proc.poll() is None and old.poll() is None
    assert "RECOVERY NEEDED" in log and "NOT marked complete" in log
    assert "routing restored" not in log
    state = json.loads((art / "run.json").read_text())
    assert not inventory_row(art, session).get("complete")
    assert not state["runs"]["orchestrator-" + session].get("complete")
    blocker = next(b for b in state["blockers"] if b["id"] == "SPAWN-" + session[:8])
    assert blocker["status"] == "open"


def test_exited_successor_with_unknown_usage_keeps_a_blocker(spawn):
    run, process, predecessor, _, art = spawn
    proc, session = successor(process)
    old, old_session = predecessor()
    run(session, old, old_session, wait=2)
    proc.wait(timeout=10)
    state = json.loads((art / "run.json").read_text())
    assert inventory_row(art, session)["complete"] is True
    assert state["runs"]["orchestrator-" + session]["usage_status"] == "unknown"
    assert any(b["id"] == "SPAWN-" + session[:8] for b in state["blockers"])
