import importlib
import json
from pathlib import Path

import pytest

pytest_plugins = ("test_pilot_handoff",)


def identity(pid, **fields):
    return {
        "pid": pid,
        "ppid": 0,
        "pgid": pid,
        "os_session": pid,
        "started_at": "time",
        "command": "fixture",
        "status": "S",
        **fields,
    }


@pytest.fixture
def draining(lead, tmp_path, monkeypatch):
    table = {}
    for module in (
        lead,
        importlib.import_module("run_state"),
        importlib.import_module("owned_processes"),
    ):
        monkeypatch.setattr(module, "processes", lambda: table)
    pane_ids = [111, 112, 201, 202]

    def pane_list(command, **kwargs):
        assert command == ["wezterm", "cli", "list", "--format", "json"]
        return json.dumps([{"pane_id": pid} for pid in pane_ids])

    monkeypatch.setattr(lead.subprocess, "check_output", pane_list)
    report = tmp_path / "drain.md"
    report.write_text("Finished work\n")
    message = "DRAIN_COMPLETE drained token " + str(report)
    sessions = []
    threads = []
    targets = []
    for name, pane, role in [
        ("old-orch", 111, "orchestrator"),
        ("old-lead", 112, "lead"),
        ("current-orch", 201, "orchestrator"),
        ("current-lead", 202, "lead"),
        ("drained", 301, "lead"),
    ]:
        transcript = tmp_path / (name + ".jsonl")
        transcript.write_text(
            json.dumps({"type": "system", "sessionId": name})
            + "\n"
            + json.dumps(
                {"message": {"id": "usage", "usage": {"input_tokens": 5, "output_tokens": 7}}}
            )
            + "\n"
        )
        row = {
            "session_id": name,
            "role": role,
            "pane_id": pane,
            "transcript_path": str(transcript),
            "complete": False,
            "started_at": "2026-10-13T00:00:00Z",
        }
        if role == "orchestrator":
            threads.append({**row, "thread_id": name})
        else:
            sessions.append(row)
        if name.startswith("old-"):
            targets.append(
                {"identity": name, "pane": str(pane), "transcript_path": str(transcript)}
            )
    receipt = tmp_path / "job-launch.json"
    receipt.write_text(
        json.dumps({"remaining_processes": [], "child_exit": 0, "owned_processes": {}})
    )
    drain = {
        "lead": identity(900001, model_session_id="drained"),
        "jobs": [{"receipt": str(receipt)}],
        "gates": [],
        "scopes": [],
        "completion": {
            "report": str(report),
            "report_sha256": lead.sha(report),
            "message": message,
            "targets": targets,
        },
    }
    state = {
        "lifecycle": "running",
        "active_lead": identity(900002, model_session_id="current-lead", pane="202"),
        "inventory": {"threads": threads, "sessions": sessions},
        "reservations": {},
        "runs": {"job": {"complete": True, "usage_status": "known"}},
        "consumed": {},
        "draining_leads": {"drained": drain},
    }
    path = tmp_path / "run.json"
    return path, state, table, pane_ids


def deliver(state, recipients, message=None, receipt_id="delivery"):
    completion = state["draining_leads"]["drained"]["completion"]
    for row in state["inventory"]["threads"] + state["inventory"]["sessions"]:
        name = row["session_id"]
        if name not in recipients:
            continue
        with Path(row["transcript_path"]).open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": name,
                        "timestamp": "2026-10-13T01:00:00Z",
                        "uuid": receipt_id,
                        "message": {"role": "user", "content": message or completion["message"]},
                    }
                )
                + "\n"
            )


def retire(lead, path, state):
    path.write_text(json.dumps(state))
    try:
        lead.retire_draining(path, "drained")
    except ValueError as error:
        raise AssertionError("Retirement refused: " + str(error)) from error
    saved = json.loads(path.read_text())
    assert saved["draining_leads"] == {}
    assert saved["runs"]["lead-drained"]["usage_status"] == "known"
    receipt = json.loads((path.parent / "handoff/lead-drained-drain-retirement.json").read_text())
    assert receipt["remaining_processes"] == []
    assert receipt["final_usage"]["new_tokens"] == 12
    return receipt


@pytest.mark.parametrize("pids", [[97748], [87881, 88718]])
def test_absent_remaining_processes_allow_retirement(lead, draining, pids):
    path, state, _, _ = draining
    drain = state["draining_leads"]["drained"]
    receipt_path = Path(drain["jobs"][0]["receipt"])
    receipt = json.loads(receipt_path.read_text())
    receipt["remaining_processes"] = pids
    receipt_path.write_text(json.dumps(receipt))
    deliver(state, ["old-orch", "old-lead"])
    retire(lead, path, state)
    assert json.loads(receipt_path.read_text())["remaining_processes"] == pids


@pytest.mark.parametrize("kind", ["complete", "closed-pane"])
@pytest.mark.parametrize("recipients", ["both", "orchestrator-only", "lead-only", "neither"])
def test_stale_targets_require_delivery_to_current_owners(lead, draining, kind, recipients):
    path, state, _, panes = draining
    if kind == "complete":
        for row in state["inventory"]["threads"] + state["inventory"]["sessions"]:
            if row["session_id"].startswith("old-"):
                row["complete"] = True
    else:
        panes[:] = [201, 202]
    names = {
        "both": ["current-orch", "current-lead"],
        "orchestrator-only": ["current-orch"],
        "lead-only": ["current-lead"],
        "neither": [],
    }[recipients]
    deliver(state, names)
    if recipients == "both":
        receipt = retire(lead, path, state)
        assert {r.get("session_id", r["identity"]) for r in receipt["notification_receipts"]} >= {
            "current-orch",
            "current-lead",
        }
    else:
        path.write_text(json.dumps(state))
        with pytest.raises(ValueError, match="Native completion notification receipt is missing"):
            lead.retire_draining(path, "drained")
        assert "retirement" not in json.loads(path.read_text())["draining_leads"]["drained"]


@pytest.mark.parametrize(
    "case",
    [
        "live-target",
        "wrong-message",
        "wrong-identity",
        "no-receipt-id",
        "no-active-lead",
    ],
)
def test_replacement_delivery_does_not_bypass_recipient_checks(lead, draining, case):
    path, state, _, panes = draining
    if case != "live-target":
        panes[:] = [201, 202]
    deliver(
        state,
        ["current-orch", "current-lead"],
        message="wrong" if case == "wrong-message" else None,
        receipt_id=None if case == "no-receipt-id" else "delivery",
    )
    if case == "wrong-identity":
        transcript = path.parent / "current-orch.jsonl"
        transcript.write_text(transcript.read_text().replace("current-orch", "intruder"))
    if case == "no-active-lead":
        state["active_lead"] = None
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="Native completion"):
        lead.retire_draining(path, "drained")
    assert "retirement" not in json.loads(path.read_text())["draining_leads"]["drained"]


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("live-remaining-pid", "Owned job final receipt is incomplete"),
        ("missing-remaining", "Owned job final receipt is incomplete"),
        ("invalid-pid", "Owned job final receipt is incomplete"),
        ("reused-pid", "Owned job final receipt is incomplete"),
        ("missing-child-exit", "Owned job final receipt is incomplete"),
        ("incomplete-accounting", "Owned job final accounting is incomplete"),
        ("unknown-accounting", "Owned job final accounting is incomplete"),
        ("owned-process", "Owned job process survives"),
        ("owned-gate", "Owned gate survives"),
        ("gate-cleanup", "Stopped gate cleanup is incomplete"),
        ("gate-child", "Stopped gate child survives"),
        ("active-job", "Wait for builder/reviewer completion"),
        ("unknown-job-usage", "Draining job usage is unknown"),
        ("reviewer", "Drain in-process reviewer work"),
        ("lead-identity", "Lead identity changed before handoff"),
        ("running-gate", "Wait for running gate PID"),
    ],
)
def test_verify_refusals_remain(lead, draining, case, message):
    path, state, table, _ = draining
    drain = state["draining_leads"]["drained"]
    receipt_path = Path(drain["jobs"][0]["receipt"])
    receipt = json.loads(receipt_path.read_text())
    if case == "live-remaining-pid":
        receipt["remaining_processes"] = [97748, 87881]
        table[87881] = identity(87881)
    elif case == "missing-remaining":
        del receipt["remaining_processes"]
    elif case == "invalid-pid":
        receipt["remaining_processes"] = ["97748"]
    elif case == "reused-pid":
        receipt["remaining_processes"] = [97748]
        receipt["owned_processes"] = {"97748": identity(97748)}
        table[97748] = identity(97748, started_at="different")
    elif case == "missing-child-exit":
        receipt["child_exit"] = None
    elif case == "incomplete-accounting":
        state["runs"]["job"]["complete"] = False
    elif case == "unknown-accounting":
        state["runs"]["job"]["usage_status"] = "unknown"
    elif case == "owned-process":
        table[97748] = identity(97748)
        receipt["owned_processes"] = {"97748": table[97748]}
    elif case == "owned-gate":
        table[97748] = identity(97748)
        drain["gates"] = [table[97748]]
    elif case in {"gate-cleanup", "gate-child"}:
        gate_receipt = path.parent / "gate-stop.json"
        gate_receipt.write_text(
            json.dumps(
                {
                    "remaining_processes": [97748] if case == "gate-cleanup" else [],
                    "owned_processes": {"97748": identity(97748)},
                }
            )
        )
        drain["gate_stop_receipts"] = {"1": str(gate_receipt)}
        if case == "gate-child":
            table[97748] = identity(97748)
    elif case == "active-job":
        state["reservations"]["a"] = {"parent_session_id": "drained", "active_run": "job"}
    elif case == "unknown-job-usage":
        state["reservations"]["a"] = {"parent_session_id": "drained"}
        state["runs"]["job"].update(usage_status="unknown", allocation_id="a")
    elif case == "reviewer":
        state["inventory"]["sessions"].append({"role": "reviewer", "parent_session_id": "drained"})
    elif case == "lead-identity":
        table[900001] = identity(900001, started_at="different")
    elif case == "running-gate":
        table[900001] = drain["lead"]
        table[97748] = identity(97748, ppid=900001, command="python unit_gate.py")
    receipt_path.write_text(json.dumps(receipt))
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match=message):
        lead.verify_draining(path, drain)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("unregistered", "Lead is not registered as draining"),
        ("live-without-completion", "Draining completion is not ready"),
        ("changed-report", "Completion report changed"),
        ("unknown-usage", "Draining lead final usage is unknown before retirement"),
        ("transcript-identity", "Native completion transcript identity differs"),
        ("missing-notification", "Native completion notification receipt is missing"),
    ],
)
def test_retire_refusals_remain(lead, draining, case, message):
    path, state, table, _ = draining
    drain = state["draining_leads"]["drained"]
    deliver(state, ["old-orch", "old-lead"])
    if case == "unregistered":
        state["draining_leads"] = {}
    elif case == "live-without-completion":
        del drain["completion"]
        table[900001] = drain["lead"]
    elif case == "changed-report":
        Path(drain["completion"]["report"]).write_text("changed")
    elif case == "unknown-usage":
        (path.parent / "drained.jsonl").write_text("{}\n")
    elif case == "transcript-identity":
        transcript = path.parent / "old-orch.jsonl"
        transcript.write_text(transcript.read_text().replace("old-orch", "intruder"))
    elif case == "missing-notification":
        (path.parent / "old-orch.jsonl").write_text(json.dumps({"sessionId": "old-orch"}) + "\n")
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match=message):
        lead.retire_draining(path, "drained")
    assert state == json.loads(path.read_text())


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("changed-identity", "Draining lead identity changed before retirement"),
        ("survived", "Draining lead survived retirement"),
        ("unknown-final-usage", "Retired lead final usage is unknown"),
    ],
)
def test_retire_rechecks_process_and_usage(lead, draining, monkeypatch, case, message):
    path, state, _, _ = draining
    drain = state["draining_leads"]["drained"]
    deliver(state, ["old-orch", "old-lead"])
    calls = 0

    def process_table():
        nonlocal calls
        calls += 1
        if case == "unknown-final-usage":
            if calls == 3:
                (path.parent / "drained.jsonl").write_text("{}\n")
            return {}
        if case == "changed-identity":
            return {} if calls < 3 else {900001: identity(900001, started_at="different")}
        row = {**drain["lead"], "status": "Z"} if calls in {4, 5} else drain["lead"]
        return {900001: row}

    for module in (
        lead,
        importlib.import_module("run_state"),
        importlib.import_module("owned_processes"),
    ):
        monkeypatch.setattr(module, "processes", process_table)
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match=message):
        lead.retire_draining(path, "drained")
    saved = json.loads(path.read_text())
    assert "drained" in saved["draining_leads"]
    assert "lead-drained" not in saved["runs"]
    assert not (path.parent / "handoff/lead-drained-drain-retirement.json").exists()


@pytest.mark.parametrize("pane_result", ["unavailable", "invalid-json"])
def test_pane_inventory_failure_preserves_refusal(lead, draining, monkeypatch, pane_result):
    path, state, _, _ = draining
    deliver(state, ["current-orch", "current-lead"])

    def pane_list(command, **kwargs):
        assert command == ["wezterm", "cli", "list", "--format", "json"]
        if pane_result == "unavailable":
            raise OSError("pane inventory unavailable")
        return "invalid-json"

    monkeypatch.setattr(lead.subprocess, "check_output", pane_list)
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="Native completion notification receipt is missing"):
        lead.retire_draining(path, "drained")
    assert state == json.loads(path.read_text())


def test_retirement_retry_remains_idempotent(lead, draining):
    path, state, _, _ = draining
    deliver(state, ["old-orch", "old-lead"])
    retire(lead, path, state)
    before = path.read_bytes()
    lead.retire_draining(path, "drained")
    assert path.read_bytes() == before


def test_complete_lead_inventory_without_pane_allows_current_delivery(lead, draining):
    path, state, _, _ = draining
    for row in state["inventory"]["threads"] + state["inventory"]["sessions"]:
        if row["session_id"].startswith("old-"):
            row["complete"] = True
        if row["session_id"] == "old-lead":
            del row["pane_id"]
    deliver(state, ["current-orch", "current-lead"])
    retire(lead, path, state)


@pytest.mark.parametrize("delivered", [True, False])
def test_retired_orchestrator_leaves_only_the_successor_recipient(lead, draining, delivered):
    path, state, _, panes = draining
    panes[:] = [202]
    for row in state["inventory"]["threads"]:
        row["complete"] = True
    deliver(state, ["current-lead"] if delivered else [])
    if delivered:
        receipt = retire(lead, path, state)
        assert {r.get("session_id", r["identity"]) for r in receipt["notification_receipts"]} == {
            "current-lead"
        }
    else:
        path.write_text(json.dumps(state))
        with pytest.raises(ValueError, match="Native completion notification receipt is missing"):
            lead.retire_draining(path, "drained")


@pytest.mark.parametrize("orchestrator", [True, False])
def test_drain_ready_targets_the_orchestrator_only_while_one_is_active(
    lead, draining, monkeypatch, orchestrator
):
    path, state, _, _ = draining
    del state["draining_leads"]["drained"]["completion"]
    (path.parent / "handoff").mkdir()
    (path.parent / "handoff/lead-drained-drain.md").write_text("Finished work\n")
    for row in state["inventory"]["threads"]:
        row["complete"] = row["session_id"] != "current-orch" or not orchestrator
    path.write_text(json.dumps(state))
    monkeypatch.setattr(lead, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(lead, "verify_draining", lambda path, drain: None)
    completion = lead.drain_ready(path, "drained")
    expected = ["current-orch", "current-lead"] if orchestrator else ["current-lead"]
    assert [t["identity"] for t in completion["targets"]] == expected


def test_drain_ready_still_requires_the_successor(lead, draining, monkeypatch):
    path, state, _, _ = draining
    del state["draining_leads"]["drained"]["completion"]
    (path.parent / "handoff").mkdir()
    (path.parent / "handoff/lead-drained-drain.md").write_text("Finished work\n")
    state["active_lead"] = None
    path.write_text(json.dumps(state))
    monkeypatch.setattr(lead, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(lead, "verify_draining", lambda path, drain: None)
    monkeypatch.setenv("DRAIN_READY_WAIT", "0")
    with pytest.raises(ValueError, match="Completion recipient identity/transcript is incomplete"):
        lead.drain_ready(path, "drained")
    assert "completion" not in json.loads(path.read_text())["draining_leads"]["drained"]


def wait_for(lead, monkeypatch, path, change):
    # Applies the change to run.json at the first poll, as a handoff landing mid-wait.
    sleeps = []

    def sleep(seconds):
        if not sleeps:
            current = json.loads(path.read_text())
            change(current)
            path.write_text(json.dumps(current))
        sleeps.append(seconds)

    monkeypatch.setattr(lead.time, "sleep", sleep)
    return sleeps


def test_drain_ready_waits_for_the_successor_to_be_recorded(lead, draining, monkeypatch):
    path, state, _, _ = draining
    del state["draining_leads"]["drained"]["completion"]
    (path.parent / "handoff").mkdir()
    (path.parent / "handoff/lead-drained-drain.md").write_text("Finished work\n")
    successor = state["active_lead"]
    state["active_lead"] = {k: v for k, v in successor.items() if k != "pane"}
    path.write_text(json.dumps(state))
    monkeypatch.setattr(lead, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(lead, "verify_draining", lambda path, drain: None)
    sleeps = wait_for(lead, monkeypatch, path, lambda s: s.update(active_lead=successor))
    completion = lead.drain_ready(path, "drained")
    assert sleeps and "current-lead" in [t["identity"] for t in completion["targets"]]


def test_drain_ready_waits_for_its_own_handoff_to_commit(lead, draining, monkeypatch):
    path, state, _, _ = draining
    drain = state["draining_leads"].pop("drained")
    del drain["completion"]
    state["handoff"] = {"token": "t", "predecessor": drain["lead"]}
    (path.parent / "handoff").mkdir()
    (path.parent / "handoff/lead-drained-drain.md").write_text("Finished work\n")
    path.write_text(json.dumps(state))
    monkeypatch.setattr(lead, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(lead, "verify_draining", lambda path, drain: None)
    sleeps = wait_for(
        lead, monkeypatch, path, lambda s: s.update(handoff={}, draining_leads={"drained": drain})
    )
    assert lead.drain_ready(path, "drained")["report"].endswith("lead-drained-drain.md")
    assert sleeps


def test_drain_ready_refuses_an_unrelated_session_without_waiting(lead, draining, monkeypatch):
    path, state, _, _ = draining
    path.write_text(json.dumps(state))
    sleeps = wait_for(lead, monkeypatch, path, lambda s: None)
    with pytest.raises(ValueError, match="Lead is not registered as draining"):
        lead.drain_ready(path, "stranger")
    assert not sleeps


def test_drain_ready_wait_ends_at_the_deadline(lead, draining, monkeypatch):
    path, state, _, _ = draining
    del state["draining_leads"]["drained"]["completion"]
    (path.parent / "handoff").mkdir()
    (path.parent / "handoff/lead-drained-drain.md").write_text("Finished work\n")
    state["active_lead"] = None
    path.write_text(json.dumps(state))
    monkeypatch.setattr(lead, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(lead, "verify_draining", lambda path, drain: None)
    monkeypatch.setenv("DRAIN_READY_WAIT", "30")
    clock = [0.0]
    monkeypatch.setattr(lead.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        lead.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    with pytest.raises(ValueError, match="Completion recipient identity/transcript is incomplete"):
        lead.drain_ready(path, "drained")
    assert 30 <= clock[0] <= 35


def test_drain_ready_short_deadline_never_sleeps_past_it(lead, draining, monkeypatch):
    path, state, _, _ = draining
    del state["draining_leads"]["drained"]["completion"]
    (path.parent / "handoff").mkdir()
    (path.parent / "handoff/lead-drained-drain.md").write_text("Finished work\n")
    successor = state["active_lead"]
    state["active_lead"] = None
    path.write_text(json.dumps(state))
    monkeypatch.setattr(lead, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(lead, "verify_draining", lambda path, drain: None)
    monkeypatch.setenv("DRAIN_READY_WAIT", "1")
    clock = [0.0]
    monkeypatch.setattr(lead.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        clock[0] += seconds
        current = json.loads(path.read_text())
        current["active_lead"] = successor
        path.write_text(json.dumps(current))

    monkeypatch.setattr(lead.time, "sleep", sleep)
    # The successor appears exactly at the deadline, so one last attempt still runs.
    assert lead.drain_ready(path, "drained")
    assert clock[0] == 1


def test_drain_ready_refuses_when_the_successor_appears_after_the_deadline(
    lead, draining, monkeypatch
):
    path, state, _, _ = draining
    del state["draining_leads"]["drained"]["completion"]
    (path.parent / "handoff").mkdir()
    (path.parent / "handoff/lead-drained-drain.md").write_text("Finished work\n")
    state["active_lead"] = None
    path.write_text(json.dumps(state))
    monkeypatch.setattr(lead, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(lead, "verify_draining", lambda path, drain: None)
    monkeypatch.setenv("DRAIN_READY_WAIT", "1")
    clock = [0.0]
    monkeypatch.setattr(lead.time, "monotonic", lambda: clock[0])
    successor = {**state["draining_leads"]["drained"]["lead"], "pid": 900002}
    successor.update(model_session_id="current-lead", pane="202")

    def overshoot(seconds):
        # The successor is registered while the sleep overshoots the deadline.
        clock[0] += 5
        current = json.loads(path.read_text())
        current["active_lead"] = successor
        path.write_text(json.dumps(current))

    monkeypatch.setattr(lead.time, "sleep", overshoot)
    with pytest.raises(ValueError, match="Completion recipient"):
        lead.drain_ready(path, "drained")
    assert "completion" not in json.loads(path.read_text())["draining_leads"]["drained"]


def test_drain_ready_refuses_unfinished_work_at_once_while_recipients_are_incomplete(
    lead, draining, monkeypatch
):
    path, state, _, _ = draining
    del state["draining_leads"]["drained"]["completion"]
    state["active_lead"] = None
    path.write_text(json.dumps(state))
    monkeypatch.setattr(lead, "originating_lead", lambda state: state["active_lead"])

    def busy(path, drain):
        raise ValueError("Owned job process survives")

    monkeypatch.setattr(lead, "verify_draining", busy)
    sleeps = wait_for(lead, monkeypatch, path, lambda s: None)
    with pytest.raises(ValueError, match="Owned job process survives"):
        lead.drain_ready(path, "drained")
    assert not sleeps


@pytest.mark.parametrize(
    "unfinished",
    [
        None,
        "active run",
        "scoped active run",
        "unknown usage",
        "reviewer",
        "job survivor",
        "job receipt incomplete",
        "live gate",
        "gate stop survivor",
    ],
)
def test_drain_finished_requires_all_owned_work_final(lead, draining, unfinished):
    path, state, table, _ = draining
    drain = state["draining_leads"]["drained"]
    del drain["completion"]
    receipt = Path(drain["jobs"][0]["receipt"])
    child = identity(700001)
    if unfinished == "active run":
        state["reservations"]["a"] = {"parent_session_id": "drained", "active_run": "a"}
    if unfinished == "scoped active run":
        drain["scopes"] = [str(path.parent / "wt")]
        state["reservations"]["b"] = {"worktree": str(path.parent / "wt/u"), "active_run": "b"}
    if unfinished == "unknown usage":
        state["runs"]["job"]["usage_status"] = "unknown"
    if unfinished == "reviewer":
        state["inventory"]["sessions"].append(
            {"session_id": "r", "role": "reviewer", "parent_session_id": "drained"}
        )
    if unfinished == "job survivor":
        table[child["pid"]] = child
        receipt.write_text(
            json.dumps(
                {
                    "remaining_processes": [],
                    "child_exit": 0,
                    "owned_processes": {str(child["pid"]): child},
                }
            )
        )
    if unfinished == "job receipt incomplete":
        receipt.write_text(json.dumps({"remaining_processes": [], "owned_processes": {}}))
    if unfinished == "live gate":
        table[child["pid"]] = child
        drain["gates"] = [child]
    if unfinished == "gate stop survivor":
        stop = path.parent / "gate-stop.json"
        stop.write_text(json.dumps({"remaining_processes": [child["pid"]], "owned_processes": {}}))
        drain["gate_stop_receipts"] = {"1": str(stop)}
    path.write_text(json.dumps(state))
    if unfinished is None:
        lead.drain_finished(path, state, "drained", drain)
    else:
        with pytest.raises(ValueError):
            lead.drain_finished(path, state, "drained", drain)


@pytest.mark.parametrize("recipient", ["successor", "orchestrator"])
@pytest.mark.parametrize(
    "field,value", [("identity", None), ("pane", "invalid"), ("transcript", "")]
)
def test_drain_ready_identifies_incomplete_recipient(
    lead, draining, monkeypatch, recipient, field, value
):
    path, state, _, _ = draining
    del state["draining_leads"]["drained"]["completion"]
    (path.parent / "handoff").mkdir()
    (path.parent / "handoff/lead-drained-drain.md").write_text("Finished work\n")
    if recipient == "orchestrator":
        row = state["inventory"]["threads"][-1]
        key = {"identity": "thread_id", "pane": "pane_id", "transcript": "transcript_path"}[field]
    elif field == "transcript":
        row = next(r for r in state["inventory"]["sessions"] if r["session_id"] == "current-lead")
        key = "transcript_path"
    else:
        row = state["active_lead"]
        key = {"identity": "model_session_id", "pane": "pane"}[field]
    row[key] = value
    path.write_text(json.dumps(state))
    monkeypatch.setattr(lead, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(lead, "verify_draining", lambda path, drain: None)
    monkeypatch.setenv("DRAIN_READY_WAIT", "0")
    with pytest.raises(lead.DrainNotReady) as error:
        lead.drain_ready(path, "drained")
    detail = json.loads(str(error.value).split(": ", 1)[1])
    assert detail["recipient"] == recipient
    assert field in detail["invalid_fields"]
    assert detail[field] == value
    assert detail["run_path"] == str(path.resolve())
    assert "completion" not in json.loads(path.read_text())["draining_leads"]["drained"]


@pytest.mark.parametrize("commit_fails", [False, True])
def test_drain_ready_retries_missing_transcript_without_masking_commit_failure(
    lead, draining, monkeypatch, commit_fails
):
    path, state, _, _ = draining
    del state["draining_leads"]["drained"]["completion"]
    (path.parent / "handoff").mkdir()
    (path.parent / "handoff/lead-drained-drain.md").write_text("Finished work\n")
    row = next(r for r in state["inventory"]["sessions"] if r["session_id"] == "current-lead")
    transcript = row.pop("transcript_path")
    path.write_text(json.dumps(state))
    monkeypatch.setattr(lead, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(lead, "verify_draining", lambda path, drain: None)

    def register(state):
        row = next(r for r in state["inventory"]["sessions"] if r["session_id"] == "current-lead")
        row["transcript_path"] = transcript

    sleeps = wait_for(lead, monkeypatch, path, register)
    if commit_fails:

        def refuse(*args):
            raise ValueError("Draining lead still owns active jobs")

        monkeypatch.setattr(lead, "transact", refuse)
        with pytest.raises(ValueError, match="still owns active jobs"):
            lead.drain_ready(path, "drained")
        assert "completion" not in json.loads(path.read_text())["draining_leads"]["drained"]
    else:
        completion = lead.drain_ready(path, "drained")
        assert completion["targets"][-1]["transcript_path"] == transcript
    assert sleeps == [5]


@pytest.mark.parametrize("completion", [False, True])
@pytest.mark.parametrize("action", ["exit", "retire"])
def test_unbound_reviewer_unknown_usage_blocks_drain(
    lead, draining, monkeypatch, completion, action
):
    import sys

    import pilot_guard

    path, state, _, _ = draining
    drain = state["draining_leads"]["drained"]
    drain["lead"]["pane"] = "301"
    if not completion:
        del drain["completion"]
    reviewer = {"session_id": "drained/agent", "role": "reviewer", "parent_session_id": "drained"}
    state["inventory"]["sessions"].append(reviewer)
    pilot_guard.reviewer_stop_unbound(
        state,
        {"agent_transcript_path": str(path.parent / "missing.jsonl")},
        "agent",
        reviewer,
        path,
    )
    run = state["runs"]["reviewer-drained/agent"]
    assert run["complete"] and run["usage_status"] == "unknown"
    assert "parent_session_id" not in run and "allocation_id" not in run
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    monkeypatch.setitem(sys.modules, "pilot_lead", lead)
    monkeypatch.setattr(pilot_guard, "originating_lead", lambda s: s["active_lead"])
    monkeypatch.setattr(pilot_guard, "check_action", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match=r"reviewer.*accounting"):
        if action == "retire":
            lead.retire_draining(path, "drained")
        else:
            pilot_guard.check(
                {
                    "session_id": "drained",
                    "tool_name": "Bash",
                    "tool_input": {"command": "wezterm cli kill-pane --pane-id 301"},
                },
                state,
                path,
                100,
                95000,
            )
    assert path.read_bytes() == before


@pytest.mark.parametrize("accounting", ["missing", "incomplete", "known", "foreign unknown"])
def test_owned_reviewer_requires_final_accounting(lead, draining, accounting):
    path, state, _, _ = draining
    drain = state["draining_leads"]["drained"]
    del drain["completion"]
    state["inventory"]["sessions"].append(
        {
            "session_id": "review",
            "role": "reviewer",
            "complete": True,
            "parent_session_id": "other" if accounting == "foreign unknown" else "drained",
        }
    )
    if accounting != "missing":
        state["runs"]["review"] = {
            "session_id": "review",
            "complete": accounting != "incomplete",
            "usage_status": "unknown" if accounting == "foreign unknown" else "known",
        }
    path.write_text(json.dumps(state))
    if accounting in {"known", "foreign unknown"}:
        lead.drain_finished(path, state, "drained", drain)
    else:
        with pytest.raises(ValueError, match=r"reviewer.*accounting"):
            lead.drain_finished(path, state, "drained", drain)


@pytest.mark.parametrize("unfinished", ["builder", "reviewer", "gate", "usage", None])
def test_completion_pointer_does_not_bypass_finished_checks(
    lead, draining, monkeypatch, unfinished
):
    import sys

    import pilot_guard

    path, state, table, _ = draining
    drain = state["draining_leads"]["drained"]
    drain["lead"]["pane"] = "301"
    table[drain["lead"]["pid"]] = drain["lead"]
    if unfinished == "builder":
        state["reservations"]["b"] = {"parent_session_id": "drained", "active_run": "b"}
    elif unfinished == "reviewer":
        state["inventory"]["sessions"].append(
            {
                "session_id": "r",
                "role": "reviewer",
                "parent_session_id": "drained",
                "complete": False,
            }
        )
        state["runs"]["r"] = {"session_id": "r", "complete": True, "usage_status": "known"}
    elif unfinished == "gate":
        gate = identity(700001)
        drain["gates"] = [gate]
        table[gate["pid"]] = gate
    elif unfinished == "usage":
        state["runs"]["b"] = {
            "parent_session_id": "drained",
            "complete": True,
            "usage_status": "unknown",
        }
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    monkeypatch.setitem(sys.modules, "pilot_lead", lead)
    monkeypatch.setattr(pilot_guard, "originating_lead", lambda s: s["active_lead"])
    monkeypatch.setattr(pilot_guard, "check_action", lambda *args, **kwargs: None)
    event = {
        "session_id": "drained",
        "tool_name": "Bash",
        "tool_input": {"command": "wezterm cli kill-pane --pane-id 301"},
    }
    if unfinished is None:
        assert pilot_guard.check(event, state, path, 100, 95000)
    else:
        with pytest.raises(ValueError):
            pilot_guard.check(event, state, path, 100, 95000)
    assert path.read_bytes() == before


@pytest.mark.parametrize("completion", [False, True])
@pytest.mark.parametrize("transcript", ["missing", "no usage", "directory", "known"])
def test_self_exit_requires_readable_lead_usage(
    lead, draining, monkeypatch, completion, transcript
):
    import sys

    import pilot_guard

    path, state, table, _ = draining
    drain = state["draining_leads"]["drained"]
    drain["lead"]["pane"] = "301"
    table[drain["lead"]["pid"]] = drain["lead"]
    if not completion:
        del drain["completion"]
    file = path.parent / "drained.jsonl"
    if transcript == "missing":
        file.unlink()
    elif transcript == "no usage":
        file.write_text("{}\n")
    elif transcript == "directory":
        file.unlink()
        file.mkdir()
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    monkeypatch.setitem(sys.modules, "pilot_lead", lead)
    monkeypatch.setattr(pilot_guard, "originating_lead", lambda s: s["active_lead"])
    monkeypatch.setattr(pilot_guard, "check_action", lambda *args, **kwargs: None)
    event = {
        "session_id": "drained",
        "tool_name": "Bash",
        "tool_input": {"command": "wezterm cli kill-pane --pane-id 301"},
    }
    if transcript == "known":
        assert pilot_guard.check(event, state, path, 100, 95000)
    else:
        with pytest.raises(ValueError, match="lead final usage is unknown"):
            pilot_guard.check(event, state, path, 100, 95000)
    assert path.read_bytes() == before


@pytest.mark.parametrize("unfinished", ["reviewer", "gate", "worker usage", "lead usage", None])
def test_stop_rechecks_finished_work_before_retire_start(lead, draining, monkeypatch, unfinished):
    path, state, table, _ = draining
    active = state.pop("draining_leads")["drained"]["lead"]
    state["active_lead"] = active
    table[active["pid"]] = active
    if unfinished == "reviewer":
        state["inventory"]["sessions"].append(
            {
                "session_id": "drained/r",
                "role": "reviewer",
                "parent_session_id": "drained",
                "complete": False,
            }
        )
        state["runs"]["r"] = {"session_id": "drained/r", "complete": False, "usage_status": "known"}
    elif unfinished == "gate":
        gate = identity(700001, ppid=active["pid"], command="python3 unit_gate.py")
        table[gate["pid"]] = gate
    elif unfinished == "worker usage":
        state["runs"]["worker"] = {
            "parent_session_id": "drained",
            "complete": True,
            "usage_status": "unknown",
        }
    elif unfinished == "lead usage":
        (path.parent / "drained.jsonl").unlink()
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    stopped = []

    def stop(pid, *args, **kwargs):
        stopped.append(pid)
        table.pop(pid, None)
        return [], []

    monkeypatch.setattr(lead, "stop", stop)
    if unfinished is None:
        lead.stop_active(path)
        assert stopped == [active["pid"]]
        assert json.loads(path.read_text())["active_lead"] is None
    else:
        with pytest.raises(ValueError):
            lead.stop_active(path)
        assert stopped == []
        assert path.read_bytes() == before
        assert not (path.parent / "leads").exists()
