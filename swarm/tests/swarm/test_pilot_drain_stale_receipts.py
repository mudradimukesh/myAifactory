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
        "unregistered-orchestrator",
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
    if case == "unregistered-orchestrator":
        state["inventory"]["threads"][-1]["complete"] = True
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
