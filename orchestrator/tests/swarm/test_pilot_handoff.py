import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lead():
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "pilot_lead", ROOT / "scripts/swarm/pilot_lead.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.pop(0)


def test_ack_requires_hash_token_and_every_job(lead, tmp_path):
    snapshot = tmp_path / "snapshot.md"
    snapshot.write_text("frozen state")
    transfer = {
        "token": "t",
        "snapshot_hash": lead.sha(snapshot),
        "jobs": [
            {
                "pid": 12,
                "pgid": 12,
                "os_session": 0,
                "started_at": "time",
                "command": "fake",
            }
        ],
    }
    ack = {"token": "t", "snapshot_hash": lead.sha(snapshot), "jobs": transfer["jobs"]}
    lead.validate_ack(transfer, ack, {12: transfer["jobs"][0]})
    for bad in [{**ack, "snapshot_hash": "wrong"}, {**ack, "jobs": []}, {}]:
        with pytest.raises(ValueError):
            lead.validate_ack(transfer, bad, {12: transfer["jobs"][0]})


def test_completed_job_requires_preserved_receipt(lead):
    job = {
        "pid": 12,
        "pgid": 12,
        "os_session": 0,
        "started_at": "time",
        "command": "fake",
    }
    transfer = {"token": "t", "snapshot_hash": "hash", "jobs": [job]}
    ack = {**transfer}
    with pytest.raises(ValueError):
        lead.validate_ack(transfer, ack, {})


def test_initial_start_refuses_existing_owner(lead, tmp_path):
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "active_lead": {"pid": 123},
                "lifecycle": "preparing",
                "blockers": [],
                "deadlines": {},
            }
        )
    )
    with pytest.raises(ValueError):
        lead.initial(path, {})


def test_handoff_never_commits_when_retirement_fails(lead, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(lead, "stop", lambda *a, **kw: ([], [123]))
    monkeypatch.setattr(lead, "transact", lambda *a: calls.append(a[2]))
    with pytest.raises(ValueError):
        lead.retire(
            tmp_path / "run.json",
            {
                "token": "t",
                "predecessor": {"pid": 123},
                "owned_processes": {"123": {"pid": 123}},
            },
        )
    assert "handoff-commit" not in calls


@pytest.mark.parametrize("kind", ["valid", "wrong_hash", "missing_job", "no_ack"])
def test_controller_with_stub_claude_processes(lead, tmp_path, monkeypatch, kind):
    import subprocess

    children = []

    def spawn():
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        children.append(child)
        identity = lead.processes()[child.pid]
        identity["model_session_id"] = str(child.pid)
        return identity

    predecessor = spawn()
    snapshot = tmp_path / "snapshot.md"
    snapshot.write_text("state")
    ack_path = tmp_path / "ack.json"
    transfer = {
        "token": "t",
        "predecessor": predecessor,
        "owned_processes": {str(predecessor["pid"]): predecessor},
        "snapshot_path": str(snapshot),
        "snapshot_hash": lead.sha(snapshot),
        "ack_path": str(ack_path),
        "jobs": [],
    }
    if kind == "missing_job":
        transfer["jobs"] = [{**predecessor, "pid": 999999}]
    if kind != "no_ack":
        ack_path.write_text(
            json.dumps(
                {
                    "token": "t",
                    "jobs": [],
                    "snapshot_hash": "wrong"
                    if kind == "wrong_hash"
                    else lead.sha(snapshot),
                }
            )
        )
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "lifecycle": "running",
                "blockers": [],
                "deadlines": {},
                "active_lead": predecessor,
                "dispatch_frozen": True,
                "handoff": transfer,
                "inventory": {"sessions": []},
                "reservations": {},
                "consumed": {"overall": 0, "preparation": 0},
                "ceilings": {"overall": 1000, "preparation": 1000},
            }
        )
    )
    monkeypatch.setattr(lead, "launch", lambda *args: spawn())
    original_stop = lead.stop

    def reap_stop(pid, owned):
        child = next(p for p in children if p.pid == pid)
        return original_stop(pid, owned, 0.1, 0.1, child.poll)

    monkeypatch.setattr(lead, "stop", reap_stop)
    try:
        if kind == "valid":
            lead.controller(path, {"PILOT_ACK_TIMEOUT": "0.1"}, "t")
            assert json.loads(path.read_text())["active_lead"]["pid"] == children[1].pid
            assert children[0].poll() is not None
        else:
            with pytest.raises(ValueError):
                lead.controller(path, {"PILOT_ACK_TIMEOUT": "0.1"}, "t")
            state = json.loads(path.read_text())
            assert state["active_lead"]["pid"] == children[0].pid
            assert state["dispatch_frozen"] is True
            assert children[0].poll() is None
            assert children[1].poll() is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


def test_initial_commit_failure_cleans_started_child(lead, tmp_path, monkeypatch):
    import subprocess

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    identity = {**lead.processes()[child.pid], "model_session_id": "fixture"}
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "lifecycle": "preparing",
                "blockers": [],
                "deadlines": {},
                "active_lead": None,
                "handoff": None,
                "consumed": {"overall": 0, "preparation": 0},
                "ceilings": {"overall": 100, "preparation": 100},
            }
        )
    )
    original = lead.transact

    def fail_commit(path, role, operation, data):
        if operation == "handoff-commit":
            raise ValueError("simulated ownership failure")
        return original(path, role, operation, data)

    monkeypatch.setattr(lead, "transact", fail_commit)
    monkeypatch.setattr(lead, "launch", lambda *args: identity)
    try:
        with pytest.raises(ValueError, match="simulated"):
            lead.initial(path, {})
        child.wait(timeout=2)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


def test_terminal_cleanup_retires_only_verified_owner(lead, tmp_path):
    import subprocess

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    identity = {**lead.processes()[child.pid], "model_session_id": "fixture"}
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "lifecycle": "complete",
                "active_lead": identity,
                "inventory": {"sessions": [{"session_id": "fixture", "role": "lead"}]},
                "reservations": {},
                "handoff": None,
                "dispatch_frozen": False,
            }
        )
    )
    try:
        lead.stop_active(path)
        child.wait(timeout=2)
        saved = json.loads(path.read_text())
        assert saved["active_lead"] is None
        assert saved["dispatch_frozen"]
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


def test_initial_launch_through_fake_wezterm_and_claude(lead, tmp_path, monkeypatch):
    import os
    import shlex

    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    claude = bin_path / "claude"
    claude.write_text(
        "#!/bin/zsh\nexec -a claude "
        + shlex.quote(sys.executable)
        + " -c 'import time; time.sleep(60)' \"$@\"\n"
    )
    claude.chmod(0o755)
    wezterm = bin_path / "wezterm"
    wezterm.write_text(
        "#!"
        + sys.executable
        + "\n"
        + "import os,subprocess,sys\n"
        + "args=sys.argv[1:]; at=args.index('--'); cwd=args[args.index('--cwd')+1]\n"
        + "subprocess.Popen(args[at+1:],cwd=cwd,stdin=subprocess.D"
        "EVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVN"
        "ULL,start_new_session=True)\n" + "print('fixture-pane')\n"
    )
    wezterm.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_path) + os.pathsep + os.environ["PATH"])
    brief = tmp_path / "brief.md"
    brief.write_text("Fixture launch; no model calls.")
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "lifecycle": "preparing",
                "blockers": [],
                "deadlines": {},
                "active_lead": None,
                "handoff": None,
                "consumed": {"overall": 0, "preparation": 0},
                "ceilings": {"overall": 100, "preparation": 100},
                "reservations": {},
            }
        )
    )
    env = {
        **os.environ,
        "REPO": str(tmp_path),
        "BIN": str(bin_path),
        "LEAD_BRIEF": str(brief),
        "LEAD_AGENT": "p1-pilot-lead",
        "FP_CAP_FRACTION": "0.50",
    }
    identity = lead.initial(path, env)
    try:
        assert identity["pane"] == "fixture-pane"
        assert "--permission-mode auto" in identity["command"]
        assert "--model claude-opus-5-5 --effort medium" in identity["command"]
        saved = json.loads(path.read_text())
        assert saved["active_lead"]["pid"] == identity["pid"]
        assert not saved["dispatch_frozen"]
    finally:
        lead.stop_active(path)
