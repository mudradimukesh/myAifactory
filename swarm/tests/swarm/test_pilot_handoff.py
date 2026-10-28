import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TextIO

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lead(monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "pilot_lead", ROOT / "scripts/swarm/pilot_lead.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_stop = module.stop

        def fixture_stop(pid, owned, int_wait=0.1, term_wait=0.1, reap=None, **kwargs):
            # Background runners can inherit ignored SIGINT. Exercise real escalation
            # and ownership checks without waiting through production grace periods.
            return original_stop(pid, owned, int_wait, term_wait, reap, **kwargs)

        monkeypatch.setattr(module, "stop", fixture_stop)
        yield module
    finally:
        sys.path.pop(0)


def test_ack_requires_hash_token_and_every_job(lead, tmp_path):
    snapshot = tmp_path / "snapshot.md"
    snapshot.write_text("frozen state")
    transfer = {
        "token": "t",
        "snapshot_hash": lead.sha(snapshot),
        "jobs": [{"pid": 12, "pgid": 12, "os_session": 0, "started_at": "time", "command": "fake"}],
    }
    ack = {"token": "t", "snapshot_hash": lead.sha(snapshot), "jobs": transfer["jobs"]}
    lead.validate_ack(transfer, ack, {12: transfer["jobs"][0]})
    for bad in [{**ack, "snapshot_hash": "wrong"}, {**ack, "jobs": []}, {}]:
        with pytest.raises(ValueError):
            lead.validate_ack(transfer, bad, {12: transfer["jobs"][0]})


def test_completed_job_requires_preserved_receipt(lead):
    job = {"pid": 12, "pgid": 12, "os_session": 0, "started_at": "time", "command": "fake"}
    transfer = {"token": "t", "snapshot_hash": "hash", "jobs": [job]}
    ack = {**transfer}
    with pytest.raises(ValueError):
        lead.validate_ack(transfer, ack, {})


def test_handoff_waits_for_real_gate_completion(lead, tmp_path, monkeypatch):
    import subprocess
    import time

    gate_script = tmp_path / "unit_gate.py"
    gate_script.write_text("import time; time.sleep(60)\n")
    child = subprocess.Popen([sys.executable, str(gate_script)], start_new_session=True)
    work_state = tmp_path / "state.md"
    work_state.write_text("Complete fixture state\n")
    path = tmp_path / "run.json"
    owner = lead.processes()[__import__("os").getpid()]
    path.write_text(
        json.dumps({"active_lead": owner, "reservations": {}, "work_state_path": str(work_state)})
    )
    calls = []
    monkeypatch.setattr(lead, "originating_lead", lambda state: owner)
    monkeypatch.setattr(lead, "transact", lambda *args: calls.append(args[2]))
    try:
        time.sleep(0.05)
        with pytest.raises(ValueError, match="gate"):
            lead.begin(path)
        assert calls == []
        assert child.poll() is None
        child.terminate()
        child.wait(timeout=3)
        lead.begin(path)
        assert calls == ["freeze-lead", "handoff-start"]
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


def test_handoff_waits_for_builder_accounting(lead, tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"reservations": {"builder": {"active_run": "builder-1"}}}))
    monkeypatch.setattr(lead, "originating_lead", lambda state: {"pid": 123})
    monkeypatch.setattr(lead, "transact", lambda *args: pytest.fail("froze with active builder"))
    with pytest.raises(ValueError, match="builder"):
        lead.begin(path)


def test_initial_start_refuses_existing_owner(lead, tmp_path):
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {"active_lead": {"pid": 123}, "lifecycle": "preparing", "blockers": [], "deadlines": {}}
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
            {"token": "t", "predecessor": {"pid": 123}, "owned_processes": {"123": {"pid": 123}}},
        )
    assert "handoff-commit" not in calls


@pytest.mark.parametrize(
    "kind", ["valid", "wrong_hash", "missing_job", "no_ack", "unknown_ack", "transcript_error"]
)
def test_controller_with_stub_claude_processes(lead, tmp_path, monkeypatch, kind):
    import subprocess

    children = []

    def spawn():
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
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
                    "snapshot_hash": "wrong" if kind == "wrong_hash" else lead.sha(snapshot),
                }
            )
        )
    transcript = tmp_path / "predecessor.jsonl"
    transcript.write_text(
        json.dumps({"message": {"id": "usage", "usage": {"input_tokens": 1}}}) + "\n"
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
                "inventory": {
                    "sessions": [
                        {
                            "session_id": predecessor["model_session_id"],
                            "role": "lead",
                            "transcript_path": str(transcript),
                        },
                        {
                            "session_id": "reviewer",
                            "role": "reviewer",
                            "parent_session_id": predecessor["model_session_id"],
                            "complete": True,
                        },
                    ]
                },
                "runs": {
                    "review": {"session_id": "reviewer", "complete": True, "usage_status": "known"}
                },
                "reservations": {},
                "consumed": {"overall": 0, "preparation": 0},
                "ceilings": {"overall": 1000, "preparation": 1000},
            }
        )
    )
    monkeypatch.setattr(lead, "launch", lambda *args: spawn())
    original_ack = lead.validate_ack

    def acknowledge(*args):
        original_ack(*args)
        if kind == "unknown_ack":
            state = json.loads(path.read_text())
            state["runs"]["review"]["usage_status"] = "unknown"
            path.write_text(json.dumps(state))

    monkeypatch.setattr(lead, "validate_ack", acknowledge)
    if kind == "transcript_error":

        def duplicate(successor, env):
            raise ValueError("Multiple transcripts for session")

        monkeypatch.setattr(lead, "successor_transcript", duplicate)
    original_stop = lead.stop

    def reap_stop(pid, owned, **kwargs):
        child = next(p for p in children if p.pid == pid)
        return original_stop(pid, owned, 0.1, 0.1, child.poll, **kwargs)

    monkeypatch.setattr(lead, "stop", reap_stop)
    try:
        if kind == "valid":
            lead.controller(path, {"PILOT_ACK_TIMEOUT": "0.1"}, "t")
            assert json.loads(path.read_text())["active_lead"]["pid"] == children[1].pid
            assert children[0].poll() is not None
        else:
            with pytest.raises(
                ValueError, match="reviewer final accounting" if kind == "unknown_ack" else None
            ):
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
    transcript = tmp_path / "fixture.jsonl"
    transcript.write_text(
        json.dumps({"message": {"id": "usage", "usage": {"input_tokens": 1}}}) + "\n"
    )
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "lifecycle": "complete",
                "active_lead": identity,
                "inventory": {
                    "sessions": [
                        {
                            "session_id": "fixture",
                            "role": "lead",
                            "transcript_path": str(transcript),
                        }
                    ]
                },
                "reservations": {},
                "consumed": {},
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


def test_terminal_cleanup_retires_exited_lead(lead, tmp_path):
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    identity = {**lead.processes()[child.pid], "model_session_id": "fixture"}
    child.terminate()
    child.wait(timeout=3)
    transcript = tmp_path / "fixture.jsonl"
    transcript.write_text(
        json.dumps({"message": {"id": "usage", "usage": {"input_tokens": 1}}}) + "\n"
    )
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "lifecycle": "complete",
                "active_lead": identity,
                "inventory": {
                    "sessions": [
                        {
                            "session_id": "fixture",
                            "role": "lead",
                            "transcript_path": str(transcript),
                        }
                    ]
                },
                "reservations": {},
                "consumed": {},
                "handoff": None,
                "dispatch_frozen": False,
            }
        )
    )
    lead.stop_active(path)
    saved = json.loads(path.read_text())
    assert saved["active_lead"] is None
    assert saved["retirement_history"][-1]["predecessor"]["pid"] == child.pid


def drain_fixture(lead, tmp_path, pid_alive):
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    identity = {**lead.processes()[child.pid], "model_session_id": "drained"}
    gate = {**identity, "pid": 999999}
    if not pid_alive:
        child.terminate()
        child.wait(timeout=3)
    transcript = tmp_path / "drained.jsonl"
    transcript.write_text(
        json.dumps({"message": {"id": "m1", "usage": {"input_tokens": 5, "output_tokens": 7}}})
        + "\n"
    )
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "lifecycle": "running",
                "active_lead": None,
                "inventory": {
                    "sessions": [
                        {
                            "session_id": "drained",
                            "role": "lead",
                            "transcript_path": str(transcript),
                        }
                    ]
                },
                "reservations": {},
                "runs": {},
                "consumed": {},
                "draining_leads": {
                    "drained": {"lead": identity, "jobs": [], "gates": [gate], "status": "draining"}
                },
            }
        )
    )
    return child, path


def test_drain_retire_accepts_exited_lead_without_completion(lead, tmp_path):
    _, path = drain_fixture(lead, tmp_path, pid_alive=False)
    lead.retire_draining(path, "drained")
    saved = json.loads(path.read_text())
    assert saved["draining_leads"] == {}
    assert saved["drain_history"][-1]["retirement"]["exited_without_completion"]
    assert saved["runs"]["lead-drained"]["usage_status"] == "known"
    receipt = json.loads((tmp_path / "handoff/lead-drained-drain-retirement.json").read_text())
    assert receipt["exited_without_completion"]
    assert receipt["final_usage"]["new_tokens"] == 12


def test_drain_retire_requires_completion_from_live_lead(lead, tmp_path):
    child, path = drain_fixture(lead, tmp_path, pid_alive=True)
    try:
        with pytest.raises(ValueError, match="Draining completion is not ready"):
            lead.retire_draining(path, "drained")
        assert "drained" in json.loads(path.read_text())["draining_leads"]
    finally:
        child.terminate()
        child.wait(timeout=3)


def test_drain_retire_accepts_lead_that_exited_after_completion(lead, tmp_path):
    _, path = drain_fixture(lead, tmp_path, pid_alive=False)
    report = tmp_path / "handoff/lead-drained-drain.md"
    report.parent.mkdir()
    report.write_text("drained\n")
    message = "DRAIN_COMPLETE drained n1 " + str(report)
    target = tmp_path / "orchestrator.jsonl"
    target.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "orch",
                "uuid": "u1",
                "message": {"role": "user", "content": message},
            }
        )
        + "\n"
    )
    state = json.loads(path.read_text())
    state["draining_leads"]["drained"]["completion"] = {
        "report": str(report),
        "report_sha256": lead.sha(report),
        "targets": [{"identity": "orch", "pane": "1", "transcript_path": str(target)}],
        "message": message,
    }
    path.write_text(json.dumps(state))
    lead.retire_draining(path, "drained")
    saved = json.loads(path.read_text())
    assert saved["draining_leads"] == {}
    assert (
        saved["drain_history"][-1]["retirement"]["notification_receipts"][0]["receipt_id"] == "u1"
    )
    assert saved["runs"]["lead-drained"]["usage_status"] == "known"


def test_drain_retire_accepts_completion_queued_mid_turn(lead, tmp_path):
    _, path = drain_fixture(lead, tmp_path, pid_alive=False)
    report = tmp_path / "handoff/lead-drained-drain.md"
    report.parent.mkdir()
    report.write_text("drained\n")
    message = "DRAIN_COMPLETE drained n1 " + str(report)
    target = tmp_path / "orchestrator.jsonl"
    target.write_text(
        json.dumps(
            {
                "type": "attachment",
                "sessionId": "orch",
                "uuid": "q1",
                "attachment": {
                    "type": "queued_command",
                    "prompt": message,
                    "commandMode": "prompt",
                    "origin": {"kind": "human"},
                },
            }
        )
        + "\n"
    )
    state = json.loads(path.read_text())
    state["draining_leads"]["drained"]["completion"] = {
        "report": str(report),
        "report_sha256": lead.sha(report),
        "targets": [{"identity": "orch", "pane": "1", "transcript_path": str(target)}],
        "message": message,
    }
    path.write_text(json.dumps(state))
    lead.retire_draining(path, "drained")
    saved = json.loads(path.read_text())
    assert saved["draining_leads"] == {}
    assert (
        saved["drain_history"][-1]["retirement"]["notification_receipts"][0]["receipt_id"] == "q1"
    )


def test_handoff_retirement_preserves_builder_and_gate_descendants(lead, tmp_path):
    import os
    import signal
    import subprocess
    import time
    from contextlib import suppress

    pids = tmp_path / "jobs.json"
    code = (
        "import subprocess,sys,time,json,pathlib,os; "
        "jobs=[subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        "start_new_session=detached) for detached in [True,False]]; "
        "target=pathlib.Path(sys.argv[1]); temporary=target.with_suffix('.tmp'); "
        "temporary.write_text(json.dumps([p.pid for p in jobs])); "
        "os.replace(temporary,target); "
        "time.sleep(60)"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(pids)], start_new_session=True)
    jobs = []
    try:
        deadline = time.monotonic() + 3
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        jobs = json.loads(pids.read_text())
        owned = {}
        lead.capture(child.pid, owned)
        assert all(pid in owned for pid in jobs)
        (tmp_path / "run.json").write_text("{}")
        predecessor = {**owned[child.pid], "model_session_id": "fixture"}
        lead.retire(
            tmp_path / "run.json",
            {"token": "jobs", "predecessor": predecessor, "owned_processes": owned},
        )
        child.wait(timeout=2)
        table = lead.processes()
        assert all(pid in table and not table[pid]["status"].startswith("Z") for pid in jobs)
        receipt = json.loads((tmp_path / "handoff/jobs-retirement.json").read_text())
        assert {row["pid"] for row in receipt["signals"]} == {child.pid}
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)
        for pid in jobs:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)


@pytest.mark.parametrize("snapshot", [b"summary only", b"state\n"])
def test_handoff_requires_verbatim_state_bytes(lead, tmp_path, monkeypatch, snapshot):
    state = tmp_path / "prompts/phase-1/state.md"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"state\r\n")
    frozen = tmp_path / "handoff/t.md"
    frozen.parent.mkdir()
    frozen.write_bytes(snapshot)
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"handoff": {"token": "t"}, "dispatch_frozen": True}))
    monkeypatch.setattr(lead, "originating_lead", lambda _: {"pid": 123})
    with pytest.raises(ValueError, match="Snapshot lacks the complete work state"):
        lead.start_handoff(path, {"REPO": str(tmp_path)}, "t")


def test_successor_prompt_reads_only_pid_and_current_delta(lead, tmp_path, monkeypatch):
    import shlex

    path = tmp_path / "run.json"
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    pinned = handoff / "t-state.md"
    pinned.write_text("Completed historical work stays available.")
    transfer = {
        "token": "t",
        "state_path": str(pinned),
        "state_sha256": lead.sha(pinned),
        "snapshot_path": str(handoff / "t.md"),
        "snapshot_hash": "snapshot-hash",
        "ack_path": str(tmp_path / "ack.json"),
        "jobs": [],
    }
    path.write_text(json.dumps({"handoff": transfer}))
    brief = tmp_path / "brief.md"
    brief.write_text("Current startup instructions.")
    captured = {}

    def spawn(command, **kwargs):
        argv = shlex.split(command[-1].removeprefix("exec "))
        captured["prompt"] = argv[-1]
        captured["session"] = argv[argv.index("--session-id") + 1]
        return "fixture-pane"

    monkeypatch.setattr(lead, "check_action", lambda *args: None)
    monkeypatch.setattr(lead, "budget", lambda *args: None)
    monkeypatch.setattr(lead.subprocess, "check_output", spawn)
    monkeypatch.setattr(
        lead,
        "processes",
        lambda: {123: {"pid": 123, "command": "claude --session-id " + captured["session"]}},
    )
    lead.launch(path, {"REPO": str(tmp_path), "BIN": str(tmp_path), "LEAD_BRIEF": str(brief)}, "t")
    prompt = captured["prompt"]
    assert str(handoff / "t-successor.json") + " with offset=4 and limit=1" in prompt
    assert "Do not load it during startup" in prompt
    assert "Do not read full run.json" in prompt
    assert "Run rg --only-matching" in prompt
    assert "Do not Read run.json line ranges" in prompt
    assert "background controller verifies ACK" in prompt
    assert "Do not idle, poll or end your turn" in prompt
    assert pinned.read_text() not in prompt
    assert "interior-lead.sh --handoff-begin with no other arguments" in prompt
    assert "interior-lead.sh --handoff-start <token>" in prompt
    lead.write(handoff / "t-successor.json", {"token": "t", "successor": {"pid": 123}})
    assert (handoff / "t-successor.json").read_text().splitlines()[3].strip() == '"pid": 123'


@pytest.mark.parametrize("invalid", ["missing", "altered", "wrong-path", "missing-hash"])
def test_pinned_state_binding_fails_closed(lead, tmp_path, invalid):
    path = tmp_path / "run.json"
    pinned = tmp_path / "handoff/t-state.md"
    pinned.parent.mkdir()
    pinned.write_bytes(b"complete state\r\n")
    transfer = {"token": "t", "state_path": str(pinned), "state_sha256": lead.sha(pinned)}
    lead.validate_pinned_state(path, transfer)
    if invalid == "missing":
        pinned.unlink()
    elif invalid == "altered":
        pinned.write_text("altered")
    elif invalid == "wrong-path":
        transfer["state_path"] = str(tmp_path / "other.md")
    else:
        transfer.pop("state_sha256")
    with pytest.raises(ValueError, match="Pinned state"):
        lead.validate_pinned_state(path, transfer)


def test_begin_preserves_complete_state_outside_delta(lead, tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    state_file = tmp_path / "prompts/phase-1/state.md"
    state_file.parent.mkdir(parents=True)
    state_file.write_bytes(b"complete historical state\r\n")
    path.write_text(json.dumps({}))
    mutations = []
    monkeypatch.setenv("REPO", str(tmp_path))
    monkeypatch.setattr(lead, "originating_lead", lambda _: {"pid": 123})
    monkeypatch.setattr(lead, "drain_jobs", lambda *args: None)
    monkeypatch.setattr(lead, "transact", lambda *args: mutations.append(args))
    lead.begin(path)
    binding = mutations[-1][-1]
    assert mutations[0][2] == "freeze-lead"
    assert mutations[-1][2] == "handoff-start"
    assert Path(binding["state_path"]).read_bytes() == state_file.read_bytes()
    lead.validate_pinned_state(path, binding)


@pytest.mark.parametrize("invalid", [None, "changed-state", "oversized-delta"])
def test_compact_handoff_validates_configured_complete_state(lead, tmp_path, monkeypatch, invalid):
    from types import SimpleNamespace

    path = tmp_path / "run.json"
    state_file = tmp_path / "current-state.md"
    state_file.write_bytes(b"complete state\r\n")
    pinned = tmp_path / "handoff/t-state.md"
    pinned.parent.mkdir()
    pinned.write_bytes(state_file.read_bytes())
    snapshot = pinned.with_name("t.md")
    snapshot.write_text("current delta")
    transfer = {"token": "t", "state_path": str(pinned), "state_sha256": lead.sha(pinned)}
    path.write_text(
        json.dumps(
            {
                "handoff": transfer,
                "dispatch_frozen": True,
                "work_state_path": str(state_file),
            }
        )
    )
    mutations = []
    monkeypatch.setattr(lead, "originating_lead", lambda _: {"pid": 123})
    monkeypatch.setattr(lead, "drain_jobs", lambda *args: None)
    monkeypatch.setattr(lead, "capture", lambda *args: {})
    monkeypatch.setattr(lead, "processes", lambda: {})
    monkeypatch.setattr(lead, "transact", lambda *args: mutations.append(args))
    monkeypatch.setattr(lead.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=456))
    if invalid == "changed-state":
        state_file.write_text("changed")
    elif invalid == "oversized-delta":
        snapshot.write_bytes(b"x" * 6145)
    env = {"REPO": str(tmp_path), "LEAD_LOG": str(tmp_path / "lead.log")}
    if invalid:
        with pytest.raises(ValueError, match=r"Pinned state differs|delta exceeds"):
            lead.start_handoff(path, env, "t")
        assert mutations == []
        assert not pinned.with_name("t-controller.log").exists()
    else:
        lead.start_handoff(path, env, "t")
        assert mutations[0][2] == "handoff-start"
        assert mutations[0][-1]["snapshot_hash"] == lead.sha(snapshot)
        assert pinned.read_bytes() == state_file.read_bytes()


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
        transcript = tmp_path / "lead.jsonl"
        transcript.write_text(
            json.dumps({"message": {"id": "usage", "usage": {"input_tokens": 1}}}) + "\n"
        )
        saved["inventory"]["sessions"][-1]["transcript_path"] = str(transcript)
        path.write_text(json.dumps(saved))
    finally:
        lead.stop_active(path)


@pytest.mark.parametrize("concurrent", [False, True], ids=["transfer", "drain"])
def test_handoff_registers_jobs_before_detached_controller_launch(
    lead: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrent: bool
) -> None:
    path = tmp_path / "run.json"
    snapshot = tmp_path / "handoff/t.md"
    snapshot.parent.mkdir()
    snapshot.write_text("complete state\njob 789\nblocker b1\n")
    work_state = tmp_path / "state.md"
    work_state.write_text("complete state\n")
    path.write_text(
        json.dumps(
            {
                "handoff": {"token": "t", "mode": "concurrent" if concurrent else "transfer"},
                "dispatch_frozen": True,
                "work_state_path": str(work_state),
                "reservations": {"job": {"parent_session_id": "owner", "active_run": "job"}},
                "blockers": [{"id": "b1", "status": "open"}],
            }
        )
    )
    job = {"pid": 789, "pgid": 789, "os_session": 0, "started_at": "time", "command": "job"}
    receipt = tmp_path / "job-launch.json"
    receipt.write_text(json.dumps(job))
    owned = {123: {"pid": 123}, 789: job}
    events: list[str] = []
    registrations: list[dict[str, object]] = []
    monkeypatch.setattr(
        lead, "originating_lead", lambda _: {"pid": 123, "model_session_id": "owner"}
    )
    monkeypatch.setattr(lead, "drain_jobs", lambda *args: events.append("drain"))
    monkeypatch.setattr(lead, "capture", lambda pid, rows: rows.update(owned))
    monkeypatch.setattr(lead, "processes", lambda: {456: {"pid": 456}})

    def register(target: Path, role: str, operation: str, data: dict[str, object]) -> None:
        assert (target, role, operation) == (path, "spawner", "handoff-start")
        events.append("register")
        registrations.append(data)

    env = {"REPO": str(tmp_path), "LEAD_LOG": str(tmp_path / "lead.log")}
    log = snapshot.with_name("t-controller.log")

    def spawn(
        command: list[str],
        *,
        env: dict[str, str],
        stdin: int,
        stdout: TextIO,
        stderr: int,
        start_new_session: bool,
    ) -> SimpleNamespace:
        assert events == (["register"] if concurrent else ["drain", "register"])
        assert command == [
            sys.executable,
            str(ROOT / "scripts/swarm/pilot_lead.py"),
            "--controller",
            "t",
        ]
        assert env == {"REPO": str(tmp_path), "LEAD_LOG": str(tmp_path / "lead.log")}
        assert stdin == lead.subprocess.DEVNULL
        assert stderr == lead.subprocess.STDOUT
        assert stdout.name == str(log)
        assert start_new_session
        events.append("spawn")
        stdout.write("controller output\n")
        return SimpleNamespace(pid=456)

    monkeypatch.setattr(lead, "transact", register)
    monkeypatch.setattr(lead.subprocess, "Popen", spawn)
    lead.start_handoff(path, env, "t")
    jobs = [{**job, "receipt": str(receipt)}]
    assert registrations == [
        {
            "token": "t",
            "snapshot_path": str(snapshot),
            "snapshot_hash": lead.sha(snapshot),
            "jobs": [] if concurrent else jobs,
            "kept_jobs": jobs if concurrent else [],
            "owned_processes": owned,
            "ack_path": env["LEAD_LOG"] + ".ack-t.json",
        },
        {"token": "t", "controller_pid": 456, "controller": {"pid": 456}},
    ]
    assert events == (
        ["register", "spawn", "register"]
        if concurrent
        else ["drain", "register", "spawn", "register"]
    )
    assert log.read_text() == "controller output\n"


@pytest.mark.parametrize("busy", [None, "builder", "reviewer", "gate"])
def test_begin_drains_only_when_work_is_running(lead, tmp_path, monkeypatch, busy):
    path = tmp_path / "run.json"
    state_file = tmp_path / "prompts/phase-1/state.md"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("state\n")
    scope = str(tmp_path / "scope")
    state = {"reservations": {}, "inventory": {"sessions": []}, "lead_scopes": {"s": [scope]}}
    if busy == "builder":
        state["reservations"] = {"r": {"active_run": "run-1", "parent_session_id": "s"}}
    if busy == "reviewer":
        state["inventory"]["sessions"] = [{"role": "reviewer", "complete": False}]
    path.write_text(json.dumps(state))
    mutations = []
    drained = []
    monkeypatch.setenv("REPO", str(tmp_path))
    monkeypatch.setattr(lead, "originating_lead", lambda _: {"pid": 123, "model_session_id": "s"})
    gate = {"pid": 77, "command": "python unit_gate.py pre"}

    def fake_capture(pid, owned):
        if busy == "gate":
            owned[77] = gate
        return {}

    monkeypatch.setattr(lead, "capture", fake_capture)
    monkeypatch.setattr(lead, "drain_jobs", lambda *args: drained.append(args))
    monkeypatch.setattr(lead, "transact", lambda *args: mutations.append(args))
    lead.begin(path, concurrent=True)
    transfer = mutations[-1][-1]
    if busy:
        assert transfer["mode"] == "concurrent"
        assert transfer["drain_gates"] == ([gate] if busy == "gate" else [])
        assert transfer["drain_scopes"] == [scope]
        assert drained == []
    else:
        assert "mode" not in transfer
        assert len(drained) == 1


def test_begin_with_running_job_still_requires_scopes(lead, tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"reservations": {"r": {"active_run": "run-1"}}}))
    monkeypatch.setattr(lead, "originating_lead", lambda _: {"pid": 123, "model_session_id": "s"})
    monkeypatch.setattr(lead, "capture", lambda pid, owned: {})
    monkeypatch.setattr(lead, "transact", lambda *args: pytest.fail("froze without scopes"))
    with pytest.raises(ValueError, match="requires registered lead scopes"):
        lead.begin(path, concurrent=True)


def test_sequential_retirement_records_predecessor_usage(lead, tmp_path, monkeypatch):
    transcript = tmp_path / "old.jsonl"
    usage = {"input_tokens": 3, "cache_creation_input_tokens": 2, "output_tokens": 5}
    row = {"message": {"id": "m", "usage": {**usage, "cache_read_input_tokens": 9}}}
    transcript.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    path = tmp_path / "run.json"
    session = {"session_id": "old", "role": "lead", "transcript_path": str(transcript)}
    path.write_text(json.dumps({"inventory": {"sessions": [session]}}))
    (tmp_path / "handoff").mkdir()
    predecessor = {"pid": 999999, "pgid": 999999, "started_at": "x", "model_session_id": "old"}
    mutations = []
    monkeypatch.setattr(lead, "transact", lambda *args: mutations.append(args))
    lead.retire(path, {"token": "t", "predecessor": predecessor})
    assert mutations == [
        (
            path,
            "orchestrator",
            "record-run",
            {
                "run_id": "lead-old",
                "session_id": "old",
                "role": "lead",
                "complete": True,
                "new_tokens": 10,
                "cache_reads": 9,
            },
        )
    ]


@pytest.mark.parametrize(
    ("command", "gate"),
    [
        ("python unit_gate.py pre", True),
        ("/usr/bin/python3 -u /repo/scripts/swarm/unit_gate.py pre", True),
        ("/bin/zsh scripts/swarm/unit_gate.sh", True),
        ("/repo/scripts/swarm/red_check.py --unit u", True),
        ("node /opt/codex exec Run scripts/swarm/unit_gate.py pre and report", False),
        ("python3 builder.py --note red_check.py", False),
        ("/bin/zsh -c python3 unit_gate.py", False),
    ],
)
def test_gate_matches_only_the_executable_argument(lead, tmp_path, monkeypatch, command, gate):
    path = tmp_path / "run.json"
    state_file = tmp_path / "prompts/phase-1/state.md"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("state\n")
    state = {"reservations": {}, "inventory": {"sessions": []}, "lead_scopes": {"s": ["x"]}}
    path.write_text(json.dumps(state))
    monkeypatch.setenv("REPO", str(tmp_path))
    monkeypatch.setattr(lead, "originating_lead", lambda _: {"pid": 123, "model_session_id": "s"})
    row = {"pid": 77, "command": command}
    monkeypatch.setattr(lead, "capture", lambda pid, owned: owned.update({77: row}) or {})
    monkeypatch.setattr(lead, "drain_jobs", lambda *args: None)
    mutations = []
    monkeypatch.setattr(lead, "transact", lambda *args: mutations.append(args))
    lead.begin(path, concurrent=True)
    assert len(mutations[-1][-1].get("drain_gates", [])) == int(gate)


@pytest.mark.parametrize(
    "case",
    [
        "found",
        "none",
        "duplicate",
        "foreign content",
        "mixed content",
        "symlink",
        "directory",
        "no identity",
        "project symlink",
    ],
)
def test_successor_transcript_requires_its_own_session_file(lead, tmp_path, case):
    successor = {"pid": 1, "model_session_id": "new"}
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    own = json.dumps({"type": "user", "sessionId": "new"}) + "\n"
    foreign = json.dumps({"type": "user", "sessionId": "other"}) + "\n"
    file = tmp_path / "projects" / "repo" / "new.jsonl"
    file.parent.mkdir(parents=True)
    if case == "directory":
        file.mkdir()
    elif case == "project symlink":
        file.parent.rmdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / file.name).write_text(own)
        file.parent.symlink_to(outside, target_is_directory=True)
    elif case != "none":
        content = {
            "foreign content": foreign,
            "mixed content": own + foreign,
            "no identity": "{}\n",
        }.get(case, own)
        if case == "symlink":
            target = tmp_path / "outside.jsonl"
            target.write_text(own)
            file.symlink_to(target)
        else:
            file.write_text(content)
    if case == "duplicate":
        second = tmp_path / "projects" / "other-repo" / "new.jsonl"
        second.parent.mkdir()
        second.write_text(own)
        with pytest.raises(ValueError, match="Multiple transcripts"):
            lead.successor_transcript(successor, env)
        return
    expected = {"transcript_path": str(file.resolve())} if case == "found" else {}
    assert lead.successor_transcript(successor, env) == expected


def test_drain_ready_cli_reports_incomplete_snapshot_then_succeeds(lead, tmp_path):
    import os
    import subprocess

    path = tmp_path / "run.json"
    owner = {**lead.processes()[os.getpid()], "model_session_id": "old"}
    state = {
        "updated_at": "fixture-snapshot",
        "active_lead": {"model_session_id": "new", "pane": "123"},
        "draining_leads": {"old": {"lead": owner, "jobs": [], "gates": [], "scopes": []}},
        "inventory": {"sessions": [{"session_id": "new", "role": "lead"}], "threads": []},
    }
    path.write_text(json.dumps(state))
    (tmp_path / "handoff").mkdir()
    (tmp_path / "handoff/lead-old-drain.md").write_text("All owned work finished\n")
    env = {**os.environ, "PILOT_RUN": str(path), "DRAIN_READY_WAIT": "0"}
    command = [sys.executable, str(ROOT / "scripts/swarm/pilot_lead.py"), "--drain-ready", "old"]
    refused = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    assert refused.returncode == 2, refused.stderr
    assert '"invalid_fields": ["transcript"]' in refused.stderr
    assert '"updated_at": "fixture-snapshot"' in refused.stderr
    assert json.loads(path.read_text()) == state
    state["inventory"]["sessions"][0]["transcript_path"] = str(tmp_path / "new.jsonl")
    path.write_text(json.dumps(state))
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert (
        json.loads(completed.stdout)
        == json.loads(path.read_text())["draining_leads"]["old"]["completion"]
    )
