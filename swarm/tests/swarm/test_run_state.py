import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.swarm import run_state
from scripts.swarm.run_state import ProcessInfo, RunRecord, RunState

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/swarm/run_state.py"


def invoke(
    path: Path, role: str, operation: str, payload: Mapping[str, object]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--run",
            str(path),
            "--as",
            role,
            operation,
            "--data",
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
    )


def bootstrap(path: Path) -> RunState:
    state: RunState = {
        "bootstrap": True,
        "started_at": "2026-10-08T00:00:00Z",
        "lifecycle": "preparing",
        "blockers": [],
        "deadlines": {"overall": None},
        "ceilings": {"overall": 100, "preparation": 100},
        "consumed": {"overall": 10, "preparation": 10},
        "inventory": {"threads": [], "sessions": []},
        "assignments": {},
        "reservations": {},
        "active_lead": None,
        "dispatch_frozen": False,
    }
    path.write_text(json.dumps(state))
    return state


def test_adoption_preserves_accounting_and_init_refuses_existing(tmp_path):
    path = tmp_path / "run.json"
    old = bootstrap(path)
    result = invoke(path, "orchestrator", "adopt-bootstrap", {})
    assert result.returncode == 0, result.stderr
    new = json.loads(path.read_text())
    for key in ("started_at", "consumed", "deadlines", "ceilings"):
        assert new[key] == old[key]
    assert invoke(path, "orchestrator", "init", {}).returncode != 0


def test_assignment_increase_updates_inactive_reservation_and_admits_resume(tmp_path):
    path = tmp_path / "run.json"
    state = bootstrap(path)
    assignment = {
        "assignment_id": "a",
        "allocation_id": "a",
        "model": "gpt-6.1-sol",
        "effort": "medium",
        "allowance": 20,
        "reason": "authorized",
    }
    state["assignments"]["a"] = assignment
    state["reservations"]["a"] = {
        "role": "builder",
        "allowance": 20,
        "observed": 15,
        "active_run": None,
        "released": False,
    }
    path.write_text(json.dumps(state))
    updated = {**assignment, "allowance": 40}
    result = invoke(path, "orchestrator", "assign", updated)
    assert result.returncode == 0, result.stderr
    assert json.loads(path.read_text())["reservations"]["a"]["allowance"] == 40
    result = invoke(
        path,
        "launcher",
        "admit",
        {
            "allocation_id": "a",
            "run_id": "resume",
            "role": "builder",
            "allowance": 40,
        },
    )
    assert result.returncode == 0, result.stderr


def test_assignment_increase_rejects_active_released_and_overbudget(tmp_path):
    path = tmp_path / "run.json"
    assignment = {
        "assignment_id": "a",
        "allocation_id": "a",
        "model": "gpt-6.1-sol",
        "effort": "medium",
        "allowance": 20,
        "reason": "authorized",
    }
    for changes, allowance in [({"active_run": "live"}, 40), ({"released": True}, 40), ({}, 120)]:
        state = bootstrap(path)
        state["assignments"]["a"] = assignment
        state["reservations"]["a"] = {
            "role": "builder",
            "allowance": 20,
            "observed": 15,
            "active_run": None,
            "released": False,
            **changes,
        }
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        assert (
            invoke(
                path, "orchestrator", "assign", {**assignment, "allowance": allowance}
            ).returncode
            != 0
        )
        assert path.read_bytes() == before


def test_roles_and_pause_fail_closed(tmp_path):
    path = tmp_path / "run.json"
    bootstrap(path)
    assert invoke(path, "launcher", "set-lifecycle", {"lifecycle": "complete"}).returncode != 0
    (tmp_path / "PAUSE").write_text("wait")
    assert (
        invoke(
            path,
            "launcher",
            "admit",
            {"allocation_id": "a", "run_id": "r", "role": "builder", "allowance": 20},
        ).returncode
        != 0
    )


def test_parallel_admission_is_locked_and_idempotent(tmp_path):
    path = tmp_path / "run.json"
    bootstrap(path)
    payload = {"allocation_id": "a", "run_id": "r", "role": "builder", "allowance": 60}
    assert invoke(path, "launcher", "admit", payload).returncode == 0
    assert invoke(path, "launcher", "admit", payload).returncode == 0
    assert invoke(path, "launcher", "admit", {**payload, "run_id": "other"}).returncode != 0
    invoke(path, "launcher", "release", {"allocation_id": "a", "run_id": "r"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda i: invoke(
                    path,
                    "launcher",
                    "admit",
                    {
                        "allocation_id": f"b{i}",
                        "run_id": f"r{i}",
                        "role": "builder",
                        "allowance": 60,
                    },
                ),
                range(4),
            )
        )
    assert sum(r.returncode == 0 for r in results) == 1


def test_disjoint_resume_deltas_and_reservation(tmp_path):
    path = tmp_path / "run.json"
    bootstrap(path)
    payload = {"allocation_id": "a", "run_id": "r1", "role": "builder", "allowance": 60}
    assert invoke(path, "launcher", "admit", payload).returncode == 0
    assert (
        invoke(
            path,
            "launcher",
            "record-run",
            {
                "allocation_id": "a",
                "run_id": "r1",
                "thread_id": "t",
                "new_tokens": 15,
                "cache_reads": 10,
                "complete": True,
            },
        ).returncode
        == 0
    )
    assert invoke(path, "launcher", "admit", {**payload, "run_id": "r2"}).returncode == 0
    assert (
        invoke(
            path,
            "launcher",
            "record-run",
            {
                "allocation_id": "a",
                "run_id": "r2",
                "thread_id": "t",
                "new_tokens": 20,
                "cache_reads": 12,
                "complete": True,
            },
        ).returncode
        == 0
    )
    s = json.loads(path.read_text())
    assert s["consumed"]["overall"] == 45
    assert s["reservations"]["a"]["observed"] == 35
    assert (
        invoke(
            path,
            "launcher",
            "admit",
            {"allocation_id": "b", "run_id": "r3", "role": "reviewer", "allowance": 31},
        ).returncode
        != 0
    )


def test_orchestrator_inventory_usage_preserves_bootstrap_and_is_idempotent(tmp_path):
    path = tmp_path / "run.json"
    state = bootstrap(path)
    state["inventory"]["threads"] = [{"thread_id": "own", "role": "orchestrator"}]
    state["orchestrator_usage"] = {"new_tokens": 10, "cache_reads": 30}
    path.write_text(json.dumps(state))
    data = {
        "run_id": "orchestration",
        "thread_id": "own",
        "role": "orchestrator",
        "new_tokens": 20,
        "cache_reads": 50,
        "telemetry_at": "2026-10-09T00:00:00Z",
    }
    assert invoke(path, "launcher", "record-run", data).returncode != 0
    for _ in range(2):
        result = invoke(path, "orchestrator", "record-run", data)
        assert result.returncode == 0, result.stderr
    saved = json.loads(path.read_text())
    assert saved["consumed"] == {"overall": 20, "preparation": 20}
    assert saved["orchestrator_usage"]["cache_reads"] == 50
    assert (
        invoke(path, "orchestrator", "record-run", {**data, "thread_id": "other"}).returncode != 0
    )


def test_missing_worker_telemetry_blocks_further_admission(tmp_path):
    path = tmp_path / "run.json"
    bootstrap(path)
    data = {"allocation_id": "a", "run_id": "r", "role": "builder", "allowance": 20}
    assert invoke(path, "launcher", "admit", data).returncode == 0
    result = invoke(
        path,
        "launcher",
        "record-run",
        {"allocation_id": "a", "run_id": "r", "usage_status": "unknown", "complete": True},
    )
    assert result.returncode == 0, result.stderr
    assert (
        invoke(path, "launcher", "release", {"allocation_id": "a", "run_id": "r"}).returncode == 0
    )
    assert (
        invoke(path, "launcher", "admit", {**data, "allocation_id": "b", "run_id": "s"}).returncode
        != 0
    )
    assert (
        invoke(
            path,
            "launcher",
            "record-run",
            {
                "allocation_id": "a",
                "run_id": "r",
                "new_tokens": 5,
                "cache_reads": 0,
                "usage_status": "known",
                "complete": True,
            },
        ).returncode
        == 0
    )
    assert (
        invoke(path, "launcher", "admit", {**data, "allocation_id": "b", "run_id": "s"}).returncode
        == 0
    )


def test_stage_boundary_requires_snapshot_and_splits_inventory_usage(tmp_path):
    path = tmp_path / "run.json"
    state = bootstrap(path)
    state["inventory"]["threads"] = [{"thread_id": "own", "role": "orchestrator"}]
    state["orchestrator_usage"] = {"new_tokens": 10, "cache_reads": 30}
    path.write_text(json.dumps(state))
    data = {
        "run_id": "own",
        "thread_id": "own",
        "role": "orchestrator",
        "new_tokens": 20,
        "cache_reads": 50,
    }
    assert invoke(path, "orchestrator", "record-run", data).returncode == 0
    transition = {"lifecycle": "running", "reason": "Ready"}
    assert invoke(path, "orchestrator", "set-lifecycle", transition).returncode != 0
    transition["usage_snapshots"] = [{**data, "new_tokens": 25}]
    result = invoke(path, "orchestrator", "set-lifecycle", transition)
    assert result.returncode == 0, result.stderr
    assert invoke(path, "orchestrator", "record-run", {**data, "new_tokens": 32}).returncode == 0
    saved = json.loads(path.read_text())
    assert saved["consumed"] == {"overall": 32, "preparation": 25, "pilot": 7}


def test_inventory_registration_does_not_recharge_bootstrap(tmp_path):
    path = tmp_path / "run.json"
    state = bootstrap(path)
    state["inventory"]["threads"] = [{"thread_id": "old", "role": "orchestrator"}]
    state["orchestrator_usage"] = {"new_tokens": 10, "cache_reads": 30}
    path.write_text(json.dumps(state))
    old = {
        "run_id": "old",
        "thread_id": "old",
        "role": "orchestrator",
        "new_tokens": 15,
        "cache_reads": 40,
    }
    assert invoke(path, "orchestrator", "record-run", old).returncode == 0
    entry = {"thread_id": "new", "role": "orchestrator", "rollout_path": "/tmp/new.jsonl"}
    assert invoke(path, "launcher", "register-inventory", entry).returncode != 0
    assert invoke(path, "orchestrator", "register-inventory", entry).returncode == 0
    data = {
        "run_id": "new",
        "thread_id": "new",
        "role": "orchestrator",
        "new_tokens": 8,
        "cache_reads": 12,
    }
    assert invoke(path, "orchestrator", "record-run", data).returncode == 0
    assert invoke(path, "orchestrator", "record-run", data).returncode == 0
    saved = json.loads(path.read_text())
    assert saved["consumed"]["overall"] == 23
    assert saved["orchestrator_usage"]["cache_reads"] == 52


def test_unknown_final_usage_keeps_observed_counters(tmp_path):
    path = tmp_path / "run.json"
    bootstrap(path)
    allocation = {"allocation_id": "a", "run_id": "r", "role": "builder", "allowance": 20}
    assert invoke(path, "launcher", "admit", allocation).returncode == 0
    observed = {"allocation_id": "a", "run_id": "r", "new_tokens": 5, "cache_reads": 7}
    assert invoke(path, "launcher", "record-run", observed).returncode == 0
    unknown = {"allocation_id": "a", "run_id": "r", "usage_status": "unknown", "complete": True}
    assert invoke(path, "launcher", "record-run", unknown).returncode == 0
    saved = json.loads(path.read_text())
    assert saved["runs"]["r"]["new_tokens"] == 5
    assert saved["consumed"]["overall"] == 15
    assert (
        invoke(
            path, "launcher", "record-run", {**observed, "new_tokens": 8, "complete": True}
        ).returncode
        == 0
    )
    assert json.loads(path.read_text())["consumed"]["overall"] == 18


def test_inventory_can_add_matching_integer_pane_without_changing_identity(tmp_path):
    path = tmp_path / "run.json"
    state = bootstrap(path)
    entry = {"thread_id": "own", "role": "orchestrator", "pane": 304, "pid": 123}
    state["inventory"]["threads"] = [entry]
    path.write_text(json.dumps(state))
    for invalid in (True, "304", 305, -1):
        payload = {**entry, "pane_id": invalid}
        assert invoke(path, "orchestrator", "register-inventory", payload).returncode != 0
    changed = {**entry, "pane_id": 304, "pid": 456}
    assert invoke(path, "orchestrator", "register-inventory", changed).returncode != 0
    corrected = {**entry, "pane_id": 304}
    assert invoke(path, "orchestrator", "register-inventory", corrected).returncode == 0
    assert invoke(path, "orchestrator", "register-inventory", corrected).returncode == 0
    result = json.loads(path.read_text())
    assert result["inventory"]["threads"] == [corrected]
    assert result["consumed"] == state["consumed"]


def test_orchestrator_reset_stalled_handoff_preserves_evidence(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("reset_state", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "run.json"
    s = bootstrap(path)
    identity = {"pid": 42, "pgid": 42, "os_session": 0, "started_at": "start", "command": "claude"}
    snapshot = tmp_path / "delta.md"
    snapshot.write_text("frozen delta")
    s.update(
        active_lead=identity,
        dispatch_frozen=True,
        handoff={
            "token": "stalled",
            "predecessor": identity,
            "aborted_at": "earlier",
            "reason": "Usage is unknown",
            "snapshot_path": str(snapshot),
            "jobs": [],
        },
    )
    monkeypatch.setattr(module, "processes", lambda: {42: identity})
    result = module.apply(
        s,
        "orchestrator",
        "handoff-abort",
        {
            "token": "stalled",
            "reason": "verified cleanup and known usage",
            "reset": True,
            "controller_pid": 99,
        },
        path,
    )
    assert result["handoff"] is None
    assert result["dispatch_frozen"] is False
    assert result["active_lead"] == identity
    assert snapshot.read_text() == "frozen delta"
    assert result["handoff_history"][-1]["token"] == "stalled"
    assert result["consumed"] == {"overall": 10, "preparation": 10}


def test_reset_handoff_requires_orchestrator(tmp_path):
    result = invoke(
        tmp_path / "run.json",
        "spawner",
        "handoff-abort",
        {
            "token": "stalled",
            "reason": "reset",
            "reset": True,
            "controller_pid": 99,
        },
    )
    assert result.returncode != 0


def test_inventory_rejects_string_pane_and_corrects_legacy_string(tmp_path):
    path = tmp_path / "run.json"
    state = bootstrap(path)
    entry = {"thread_id": "own", "role": "orchestrator", "pid": 123}
    path.write_text(json.dumps(state))
    assert (
        invoke(path, "orchestrator", "register-inventory", {**entry, "pane_id": "503"}).returncode
        != 0
    )
    state["inventory"]["threads"] = [{**entry, "pane_id": "503"}]
    path.write_text(json.dumps(state))
    assert (
        invoke(path, "orchestrator", "register-inventory", {**entry, "pane_id": 504}).returncode
        != 0
    )
    corrected = {**entry, "pane_id": 503}
    assert invoke(path, "orchestrator", "register-inventory", corrected).returncode == 0
    assert json.loads(path.read_text())["inventory"]["threads"] == [corrected]


def test_release_drain_scope_requires_no_active_job_in_scope(tmp_path):
    path = tmp_path / "run.json"
    state = bootstrap(path)
    busy, idle = str(tmp_path / "busy"), str(tmp_path / "idle")
    state["draining_leads"] = {"old": {"lead": {}, "scopes": [busy, idle]}}
    state["reservations"] = {
        "a": {"role": "builder", "active_run": "r", "parent_session_id": "old", "worktree": busy},
        "b": {"role": "builder", "active_run": None, "parent_session_id": "old", "worktree": idle},
    }
    path.write_text(json.dumps(state))
    payload = {"session_id": "old", "scope": busy, "reason": "test"}
    assert invoke(path, "lead", "release-drain-scope", payload).returncode
    result = invoke(path, "orchestrator", "release-drain-scope", payload)
    assert result.returncode and "active job" in result.stderr
    assert invoke(path, "orchestrator", "release-drain-scope", {**payload, "reason": ""}).returncode
    release_idle = {**payload, "scope": idle}
    assert invoke(path, "orchestrator", "release-drain-scope", release_idle).returncode == 0
    saved = json.loads(path.read_text())
    assert saved["draining_leads"]["old"]["scopes"] == [busy]
    assert saved["drain_history"][-1]["released_scope"] == idle
    assert invoke(path, "orchestrator", "release-drain-scope", release_idle).returncode


def test_admission_allows_eight_active_builders(tmp_path):
    path = tmp_path / "run.json"
    state = bootstrap(path)
    state["ceilings"] = {"overall": 10000, "preparation": 10000}
    path.write_text(json.dumps(state))
    for i in range(8):
        payload = {"allocation_id": f"b{i}", "run_id": f"r{i}", "role": "builder", "allowance": 60}
        assert invoke(path, "launcher", "admit", payload).returncode == 0
    ninth = {"allocation_id": "b8", "run_id": "r8", "role": "builder", "allowance": 60}
    result = invoke(path, "launcher", "admit", ninth)
    assert result.returncode != 0
    assert "Eight builders are already active" in result.stdout + result.stderr


@pytest.fixture
def large_run(tmp_path: Path) -> Path:
    path = tmp_path / "run.json"
    state = bootstrap(path)
    state["assignment_history"] = [
        {"assignment_id": f"historical-{i}", "reason": "UNRELATED_HISTORY_" + "x" * 1024}
        for i in range(1100)
    ]
    state["assignments"] = {
        "allocation": {
            "assignment_id": "assignment",
            "allocation_id": "allocation",
            "model": "gpt-6.1-sol",
            "effort": "medium",
            "allowance": 20,
            "reason": "authorized",
        }
    }
    state["reservations"] = {
        "allocation": {
            "role": "builder",
            "allowance": 20,
            "observed": 0,
            "active_run": "run",
            "released": False,
        }
    }
    state["runs"] = {
        "run": {
            "allocation_id": "allocation",
            "new_tokens": 0,
            "cache_reads": 0,
            "stage": "preparation",
        }
    }
    path.write_text(json.dumps(state))
    assert path.stat().st_size >= 1024 * 1024
    return path


@pytest.mark.parametrize(
    ("role", "operation", "payload", "key"),
    [
        ("orchestrator", "adopt-bootstrap", {}, None),
        ("orchestrator", "set-lifecycle", {"lifecycle": "paused", "reason": "test"}, None),
        ("orchestrator", "add-blocker", {"id": "block", "actions": ["dispatch"]}, None),
        ("orchestrator", "set-ceilings", {"ceilings": {"overall": 200}, "reason": "test"}, None),
        (
            "orchestrator",
            "assign",
            {
                "assignment_id": "assignment",
                "allocation_id": "allocation",
                "model": "gpt-6.1-sol",
                "effort": "medium",
                "allowance": 20,
                "reason": "authorized",
            },
            "assignment",
        ),
        (
            "launcher",
            "admit",
            {"allocation_id": "allocation", "run_id": "run", "role": "builder", "allowance": 20},
            "run",
        ),
        (
            "launcher",
            "record-run",
            {"allocation_id": "allocation", "run_id": "run", "new_tokens": 5, "cache_reads": 7},
            "run",
        ),
        ("launcher", "release", {"allocation_id": "allocation", "run_id": "run"}, "run"),
        (
            "orchestrator",
            "register-inventory",
            {"session_id": "session", "role": "lead"},
            "session",
        ),
        ("spawner", "handoff-start", {"token": "transfer", "initial": True}, None),
    ],
    ids=[
        "adopt",
        "lifecycle",
        "blocker",
        "ceilings",
        "assign",
        "admit",
        "record",
        "release",
        "inventory",
        "handoff",
    ],
)
def test_success_stdout_is_one_short_json_line(
    large_run: Path, role: str, operation: str, payload: dict[str, object], key: str | None
) -> None:
    result = invoke(large_run, role, operation, payload)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert len(result.stdout.encode("utf-8")) <= 512
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout) == {"ok": True, "operation": operation, "key": key}
    saved = json.loads(large_run.read_text())
    assert len(saved["assignment_history"]) >= 1100
    assert "updated_at" in saved


@pytest.mark.parametrize(
    "operation",
    [
        "close-blocker",
        "register-lead-scopes",
        "reconcile-lead-session",
        "release-drain-scope",
        "freeze-lead",
        "handoff-abort",
        "handoff-commit",
        "retire-start",
        "retire-commit",
        "drain-ready",
        "drain-retire-start",
        "drain-gate-stop",
        "drain-retire",
    ],
)
def test_remaining_operations_print_short_results(large_run: Path, operation: str) -> None:
    spec = importlib.util.spec_from_file_location("quiet_operations", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = {**module.processes()[os.getpid()], "model_session_id": "session"}
    state = json.loads(large_run.read_text())
    state["reservations"]["allocation"]["active_run"] = None
    state["active_lead"] = identity
    state["blockers"] = [{"id": "block", "actions": []}]
    state["handoff"] = {"token": "transfer"}
    state["draining_leads"] = {
        "drained": {
            "lead": {**identity, "pid": -1},
            "scopes": [str(large_run.parent)],
            "gates": [{"pid": -1}],
        }
    }
    payload: dict[str, object] = {"reason": "test"}
    role = "orchestrator"
    key: str | None = None
    if operation == "close-blocker":
        payload["id"] = "block"
    elif operation == "register-lead-scopes":
        state["draining_leads"] = {}
        payload.update(session_id="session", scopes=[str(large_run.parent)])
        key = "session"
    elif operation == "reconcile-lead-session":
        state["handoff"] = None
        transcript = large_run.with_name("new-session.jsonl")
        transcript.write_text('{"sessionId":"new-session"}\n')
        payload.update(
            previous_session_id="session",
            session_id="new-session",
            pid=os.getpid(),
            transcript_path=str(transcript),
        )
        key = "new-session"
    elif operation == "release-drain-scope":
        payload.update(session_id="drained", scope=str(large_run.parent))
        key = "drained"
    elif operation.startswith("drain-"):
        role = "spawner"
        payload["session_id"] = "drained"
        key = "drained"
        if operation == "drain-ready":
            payload["completion"] = {"report": "report.md"}
        elif operation == "drain-gate-stop":
            payload.update(gate_pid=-1, receipt="receipt.json")
        else:
            state["draining_leads"]["drained"]["completion"] = {"report": "report.md"}
            if operation == "drain-retire-start":
                payload["retirement"] = {
                    "lead": state["draining_leads"]["drained"]["lead"],
                    "notification_receipts": [{"receipt_id": "receipt"}],
                }
            else:
                state["runs"]["lead-drained"] = {"complete": True, "usage_status": "known"}
                payload["receipt"] = "receipt.json"
    elif operation.startswith("retire-"):
        role = "spawner"
        payload.update(predecessor=identity, owned_processes={})
        key = "session"
        if operation == "retire-commit":
            state.update(dispatch_frozen=True, retirement={"owned_processes": {}})
    else:
        role = "spawner"
        payload["token"] = "transfer"
        if operation == "freeze-lead":
            key = "session"
        elif operation == "handoff-commit":
            payload["successor"] = identity
            key = "session"
    large_run.write_text(json.dumps(state))
    assert large_run.stat().st_size >= 1024 * 1024
    result = invoke(large_run, role, operation, payload)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert len(result.stdout.encode("utf-8")) <= 512
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout) == {"ok": True, "operation": operation, "key": key}
    assert len(json.loads(large_run.read_text())["assignment_history"]) == 1100


def test_init_prints_short_result(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    payload = bootstrap(path)
    path.unlink()
    result = invoke(path, "orchestrator", "init", payload)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert len(result.stdout.encode("utf-8")) <= 512
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout) == {"ok": True, "operation": "init", "key": None}
    assert json.loads(path.read_text())["bootstrap"] is False


@pytest.mark.parametrize("failure", ["unknown-usage", "concurrent-allocation", "blocker"])
def test_refusal_reports_only_related_rows(large_run: Path, failure: str) -> None:
    state = json.loads(large_run.read_text())
    payload = {"allocation_id": "next", "run_id": "next-run", "role": "builder", "allowance": 20}
    if failure == "unknown-usage":
        state["runs"]["run"]["usage_status"] = "unknown"
        expected = {"runs": {"run": state["runs"]["run"]}}
        reason = "Usage is unknown for an inventoried run"
    elif failure == "concurrent-allocation":
        payload["allocation_id"] = "allocation"
        expected = {"reservations": {"allocation": state["reservations"]["allocation"]}}
        reason = "Concurrent allocation use"
    else:
        blocker = {"id": "blocked-launch", "status": "open", "actions": ["launch:builder"]}
        state["blockers"] = [blocker]
        expected = {"blockers": [blocker]}
        reason = "Blocked by blocked-launch"
    large_run.write_text(json.dumps(state))
    before = large_run.read_bytes()
    result = invoke(large_run, "orchestrator", "admit", payload)
    assert result.returncode == 2
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert lines[0] == "REFUSED: " + reason
    assert len(lines) == 2
    assert all(len(line.encode("utf-8")) <= 2048 for line in lines)
    assert json.loads(lines[1]) == expected
    assert large_run.read_bytes() == before


def test_refusal_with_large_related_row_stays_bounded(large_run: Path) -> None:
    state = json.loads(large_run.read_text())
    state["runs"]["run"].update(usage_status="unknown", reason="LARGE_ROW_" + "x" * 10000)
    large_run.write_text(json.dumps(state))
    before = large_run.read_bytes()
    result = invoke(
        large_run,
        "orchestrator",
        "admit",
        {"allocation_id": "next", "run_id": "next-run", "role": "builder", "allowance": 20},
    )
    assert result.returncode == 2
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert lines[0] == "REFUSED: Usage is unknown for an inventoried run"
    assert len(lines) == 2
    assert all(len(line.encode("utf-8")) <= 2048 for line in lines)
    rows = json.loads(lines[1])
    assert set(rows) == {"runs"}
    assert set(rows["runs"]) == {"run"}
    assert rows["runs"]["run"]["usage_status"] == "unknown"
    assert "UNRELATED_HISTORY_" not in result.stderr
    assert large_run.read_bytes() == before


def test_refusal_without_related_rows_is_only_reason(large_run: Path) -> None:
    before = large_run.read_bytes()
    result = invoke(large_run, "launcher", "set-lifecycle", {"lifecycle": "complete"})
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "REFUSED: Operation not permitted for role\n"
    assert large_run.read_bytes() == before


def test_transact_keeps_full_state_in_process(
    large_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("quiet_run_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.transact(large_run, "orchestrator", "adopt-bootstrap", {})
    assert result == json.loads(large_run.read_text())
    assert len(json.dumps(result).encode("utf-8")) >= 1024 * 1024
    assert capsys.readouterr() == ("", "")


def test_processes_parses_identity_and_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def output(command: list[str], *, text: bool) -> str:
        assert command == ["ps", "-axo", "pid=,ppid=,pgid=,sess=,stat=,lstart=,command="]
        assert text
        return " 42 1 42 7 S Mon Oct 12 14:08:21 2026 claude --session-id lead\ninvalid\n"

    monkeypatch.setattr(subprocess, "check_output", output)
    assert run_state.processes() == {
        42: {
            "pid": 42,
            "ppid": 1,
            "pgid": 42,
            "os_session": 7,
            "status": "S",
            "started_at": "Mon Oct 12 14:08:21 2026",
            "command": "claude --session-id lead",
        }
    }


@pytest.mark.parametrize("field", ["pid", "pgid", "os_session", "started_at", "command"])
def test_same_identity_requires_each_process_field(field: str) -> None:
    identity: dict[str, object] = {
        "pid": 42,
        "pgid": 42,
        "os_session": 7,
        "started_at": "start",
        "command": "claude",
    }
    assert run_state.same_identity(identity, {**identity, "status": "S", "ppid": 1})
    assert not run_state.same_identity(identity, {**identity, field: "changed"})
    assert not run_state.same_identity(identity, None)
    assert not run_state.same_identity(identity, {})


@pytest.mark.parametrize("failure", [None, "missing", "changed", "cycle", "unrelated"])
def test_originating_lead_checks_ancestor_identity(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    lead: ProcessInfo = {
        "pid": 42,
        "ppid": 1,
        "pgid": 42,
        "os_session": 7,
        "started_at": "start",
        "command": "claude",
        "status": "S",
    }
    active: RunRecord = {
        "pid": 42,
        "ppid": 1,
        "pgid": 42,
        "os_session": 7,
        "started_at": "start",
        "command": "claude",
        "status": "S",
        "model_session_id": "lead-session",
    }
    caller: ProcessInfo = {**lead, "pid": 43, "ppid": 42, "command": "python run_state.py"}
    table = {42: lead, 43: caller}
    state: RunState = {"active_lead": active}
    if failure == "missing":
        state["active_lead"] = None
    elif failure == "changed":
        table[42] = {**lead, "started_at": "reused pid"}
    elif failure == "cycle":
        caller["ppid"] = 43
    elif failure == "unrelated":
        caller["ppid"] = 1
    monkeypatch.setattr(run_state, "processes", lambda: table)
    monkeypatch.setattr(os, "getpid", lambda: 43)
    if failure:
        reason = {
            "missing": "No active lead",
            "changed": "Active lead identity changed",
            "cycle": "Caller is not a descendant",
            "unrelated": "Caller is not a descendant",
        }[failure]
        with pytest.raises(ValueError, match=reason):
            run_state.originating_lead(state)
    else:
        assert run_state.originating_lead(state) == active


@pytest.mark.parametrize("failure", [None, "pause", "lifecycle", "blocker", "deadline", "frozen"])
def test_check_action_preserves_work_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    path = tmp_path / "run.json"
    state = bootstrap(path)
    calls: list[RunState] = []

    def origin(current: RunState) -> RunRecord:
        calls.append(current)
        return {"model_session_id": "lead"}

    monkeypatch.setattr(run_state, "originating_lead", origin)
    state["blockers"] = [{"id": "closed", "status": "closed", "actions": ["dispatch"]}]
    state["deadlines"] = {"overall": "2999-01-01T00:00:00+00:00"}
    if failure == "pause":
        path.with_name("PAUSE").write_text("wait")
    elif failure == "lifecycle":
        state["lifecycle"] = "paused"
    elif failure == "blocker":
        state["blockers"].append({"id": "held", "actions": ["dispatch"]})
    elif failure == "deadline":
        state["deadlines"]["overall"] = "2000-01-01T00:00:00+00:00"
    elif failure == "frozen":
        state["dispatch_frozen"] = True
    if failure:
        reason = {
            "pause": "PAUSE exists",
            "lifecycle": "Lifecycle does not permit work",
            "blocker": "Blocked by held",
            "deadline": "Recorded deadline reached",
            "frozen": "Lead dispatch is frozen",
        }[failure]
        with pytest.raises(ValueError, match=reason):
            run_state.check_action(state, path, "dispatch", require_lead=True)
        assert len(calls) == int(failure == "frozen")
    else:
        run_state.check_action(state, path, "dispatch", require_lead=True)
        assert calls == [state]
        state["dispatch_frozen"] = True
        run_state.check_action(state, path, "dispatch")
    assert datetime.fromisoformat(run_state.utc()).tzinfo == UTC
    assert run_state.stage(state) == "preparation"
    state["pilot_started_at"] = run_state.utc()
    assert run_state.stage(state) == "pilot"


def test_reviewer_admission_and_usage_preserve_inventory_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    path = linked / "run.json"
    state = bootstrap(path)
    state["active_lead"] = {"model_session_id": "lead"}
    path.write_text(json.dumps(state))
    path.with_name("tag-candidate.json").write_text('{"expected_review_modes":["code","product"]}')
    monkeypatch.setattr(run_state, "originating_lead", lambda current: current["active_lead"])
    admitted = run_state.transact(
        path,
        "launcher",
        "admit",
        {
            "allocation_id": "review",
            "run_id": "r",
            "role": "reviewer",
            "allowance": 20,
            "tag": "tag",
        },
    )
    outputs = [str(tmp_path / ("tag-" + mode + "-review.json")) for mode in ("code", "product")]
    assert admitted["reservations"]["review"]["parent_session_id"] == "lead"
    assert admitted["reservations"]["review"]["output_paths"] == outputs
    observed: RunRecord = {
        "allocation_id": "review",
        "run_id": "r",
        "session_id": "lead/reviewer",
        "agent_id": "reviewer",
        "transcript_path": str(tmp_path / "reviewer.jsonl"),
        "new_tokens": 5,
        "cache_reads": 7,
        "complete": False,
    }
    run_state.transact(path, "launcher", "record-run", observed)
    saved = run_state.transact(path, "launcher", "record-run", {**observed, "complete": True})
    assert saved["inventory"]["sessions"] == [
        {
            "session_id": "lead/reviewer",
            "role": "reviewer",
            "agent_id": "reviewer",
            "transcript_path": observed["transcript_path"],
            "complete": True,
            "parent_session_id": "lead",
            "output_paths": outputs,
        }
    ]
    assert saved["consumed"] == {"overall": 15, "preparation": 15}
    assert saved["reservations"]["review"]["observed"] == 5
    assert saved["reservations"]["review"]["active_run"] is None
    assert saved["runs"]["r"]["usage_status"] == "known"


@pytest.mark.parametrize(
    "transcript", ["valid", "wrong name", "missing", "relative", "symlink", None]
)
def test_handoff_commit_records_only_the_successors_own_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transcript: str | None
) -> None:
    path = tmp_path / "run.json"
    state = bootstrap(path)
    predecessor: RunRecord = {
        "pid": 42,
        "pgid": 42,
        "os_session": 7,
        "started_at": "old",
        "command": "claude",
        "model_session_id": "old-session",
    }
    successor: RunRecord = {
        **predecessor,
        "pid": 43,
        "started_at": "new",
        "model_session_id": "new",
    }
    real = tmp_path / "projects" / "repo" / "new.jsonl"
    real.parent.mkdir(parents=True)
    real.write_text("{}\n")
    other = real.with_name("other.jsonl")
    other.write_text("{}\n")
    link = tmp_path / "link" / "new.jsonl"
    link.parent.mkdir()
    link.symlink_to(other)
    monkeypatch.chdir(real.parent)
    given = {
        "valid": str(real),
        "wrong name": str(other),
        "missing": str(real.with_name("absent.jsonl")),
        "relative": "new.jsonl",
        "symlink": str(link),
    }
    data: RunRecord = {"token": "transfer", "successor": successor}
    if transcript:
        data["transcript_path"] = given[transcript]
    state["active_lead"] = predecessor
    state["handoff"] = {
        "token": "transfer",
        "predecessor": predecessor,
        "mode": "concurrent",
        "drain_scopes": [str(tmp_path / "held")],
    }
    state["dispatch_frozen"] = True
    monkeypatch.setattr(run_state, "processes", lambda: {42: predecessor, 43: successor})
    run_state.apply(state, "spawner", "handoff-commit", data, path)
    row = state["inventory"]["sessions"][-1]
    assert row["session_id"] == "new"
    assert "transcript_path" not in state["active_lead"]
    if transcript == "valid":
        assert row["transcript_path"] == str(real)
    else:
        assert "transcript_path" not in row


@pytest.mark.parametrize("update", ["alias", "different"])
def test_record_run_keeps_the_recorded_lead_transcript(tmp_path: Path, update: str) -> None:
    path = tmp_path / "run.json"
    state = bootstrap(path)
    real = tmp_path / "projects" / "new.jsonl"
    real.parent.mkdir()
    real.write_text("{}\n")
    other = tmp_path / "projects" / "elsewhere.jsonl"
    other.write_text("{}\n")
    alias = tmp_path / "alias"
    alias.symlink_to(real.parent)
    state.setdefault("inventory", {"threads": [], "sessions": []})["sessions"].append(
        {"session_id": "new", "role": "lead", "transcript_path": str(real), "complete": False}
    )
    data: RunRecord = {
        "run_id": "lead-new",
        "session_id": "new",
        "role": "lead",
        "complete": False,
        "new_tokens": 1,
        "cache_reads": 0,
        "transcript_path": str(alias / "new.jsonl" if update == "alias" else other),
    }
    if update == "alias":
        run_state.apply(state, "orchestrator", "record-run", data, path)
    else:
        with pytest.raises(ValueError, match="conflicts"):
            run_state.apply(state, "orchestrator", "record-run", data, path)
    assert state["inventory"]["sessions"][-1]["transcript_path"] == str(real)


def test_concurrent_handoff_preserves_drain_owner_and_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.json"
    state = bootstrap(path)
    predecessor: RunRecord = {
        "pid": 42,
        "pgid": 42,
        "os_session": 7,
        "started_at": "old",
        "command": "claude",
        "model_session_id": "old-session",
    }
    successor: RunRecord = {
        **predecessor,
        "pid": 43,
        "started_at": "new",
        "model_session_id": "new",
    }
    scopes = [str(tmp_path / "held")]
    jobs: list[RunRecord] = [{"allocation_id": "builder", "run_id": "r"}]
    gates: list[RunRecord] = [{"pid": 44, "command": "unit_gate.py"}]
    state["active_lead"] = predecessor
    state["handoff"] = {
        "token": "transfer",
        "predecessor": predecessor,
        "mode": "concurrent",
        "drain_scopes": scopes,
        "kept_jobs": jobs,
        "drain_gates": gates,
    }
    state["dispatch_frozen"] = True
    monkeypatch.setattr(run_state, "processes", lambda: {42: predecessor, 43: successor})
    run_state.apply(
        state, "spawner", "handoff-commit", {"token": "transfer", "successor": successor}, path
    )
    assert state["active_lead"] == successor
    assert state["draining_leads"]["old-session"] == {
        "lead": predecessor,
        "scopes": scopes,
        "successor": successor,
        "token": "transfer",
        "jobs": jobs,
        "status": "draining",
        "gates": gates,
    }
    assert state["inventory"]["sessions"][0]["session_id"] == "new"
    assert state["handoff_history"][-1]["committed_at"]
    assert state["handoff"] is None
    assert state["dispatch_frozen"] is False


@pytest.mark.parametrize("live_status", [None, "S", "Z"])
def test_orphaned_drain_retirement_requires_exited_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_status: str | None
) -> None:
    path = tmp_path / "run.json"
    state = bootstrap(path)
    lead: RunRecord = {
        "pid": 42,
        "pgid": 42,
        "os_session": 7,
        "started_at": "old",
        "command": "claude",
        "model_session_id": "old",
    }
    state["draining_leads"] = {"old": {"lead": lead, "scopes": []}}
    table = {42: {**lead, "status": live_status}} if live_status else {}
    monkeypatch.setattr(run_state, "processes", lambda: table)
    retirement: RunRecord = {"lead": lead, "exited_without_completion": True}
    data: RunRecord = {"session_id": "old", "retirement": retirement}
    if live_status == "S":
        with pytest.raises(ValueError, match="requires an exited lead"):
            run_state.apply(state, "spawner", "drain-retire-start", data, path)
        assert "retirement" not in state["draining_leads"]["old"]
        return
    run_state.apply(state, "spawner", "drain-retire-start", data, path)
    assert state["draining_leads"]["old"]["status"] == "retiring"
    assert state["draining_leads"]["old"]["retirement"] == retirement
    monkeypatch.setattr(run_state, "processes", lambda: {})
    state["runs"] = {"lead-old": {"complete": True, "usage_status": "known"}}
    run_state.apply(
        state, "spawner", "drain-retire", {"session_id": "old", "receipt": "done"}, path
    )
    assert state["draining_leads"] == {}
    assert state["drain_history"][-1]["retirement"] == retirement


@pytest.mark.parametrize("failure", [None, "missing", "active", "unknown", "completed"])
def test_drain_ready_requires_finished_accounted_jobs(tmp_path: Path, failure: str | None) -> None:
    path = tmp_path / "run.json"
    state = bootstrap(path)
    state["draining_leads"] = {"old": {"lead": {}, "gates": [{"pid": 44}]}}
    state["reservations"] = {"job": {"parent_session_id": "old", "active_run": None}}
    state["runs"] = {
        "r": {"allocation_id": "job", "usage_status": "known"},
        "unrelated": {"usage_status": "unknown"},
    }
    data: RunRecord = {"session_id": "old", "completion": {"reason": "finished"}}
    if failure == "missing":
        data.pop("session_id")
    elif failure == "active":
        state["reservations"]["job"]["active_run"] = "r"
    elif failure == "unknown":
        state["runs"]["r"]["usage_status"] = "unknown"
    elif failure == "completed":
        state["draining_leads"]["old"]["completion"] = {"reason": "earlier"}
    if failure:
        reason = {
            "missing": "Drain completion is missing or already recorded",
            "active": "Draining lead still owns active jobs",
            "unknown": "Draining job usage is unknown",
            "completed": "Drain completion is missing or already recorded",
        }[failure]
        with pytest.raises(ValueError, match=reason):
            run_state.apply(state, "spawner", "drain-ready", data, path)
    else:
        run_state.apply(state, "spawner", "drain-ready", data, path)
        assert state["draining_leads"]["old"]["completion"] == data["completion"]
        assert state["draining_leads"]["old"]["status"] == "notification_pending"
        with pytest.raises(ValueError, match="Gate is not owned"):
            run_state.apply(
                state,
                "spawner",
                "drain-gate-stop",
                {"session_id": "old", "gate_pid": 45, "receipt": "wrong"},
                path,
            )
        run_state.apply(
            state,
            "spawner",
            "drain-gate-stop",
            {"session_id": "old", "gate_pid": 44, "receipt": "stopped"},
            path,
        )
        assert state["draining_leads"]["old"]["gate_stop_receipts"] == {"44": "stopped"}


def test_lifecycle_and_blocker_updates_persist_reasons(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    bootstrap(path)
    assert (
        invoke(
            path, "orchestrator", "add-blocker", {"id": "held", "actions": ["dispatch"]}
        ).returncode
        == 0
    )
    closed = invoke(path, "orchestrator", "close-blocker", {"id": "held", "reason": "resolved"})
    assert closed.returncode == 0, closed.stderr
    result = invoke(
        path, "orchestrator", "set-lifecycle", {"lifecycle": "paused", "reason": "wait"}
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(path.read_text())
    assert state["lifecycle"] == "paused"
    assert state["lifecycle_reason"] == "wait"
    assert state["blockers"][0]["status"] == "closed"
    assert state["blockers"][0]["resolution"] == "resolved"
    assert datetime.fromisoformat(state["blockers"][0]["closed_at"]).tzinfo == UTC


@pytest.mark.parametrize("predecessor_alive", [False, True])
def test_sequential_handoff_passes_lead_scopes_to_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, predecessor_alive: bool
) -> None:
    path = tmp_path / "run.json"
    state = bootstrap(path)
    predecessor: RunRecord = {
        "pid": 42,
        "pgid": 42,
        "os_session": 7,
        "started_at": "old",
        "command": "claude",
        "model_session_id": "old-session",
    }
    successor: RunRecord = {
        **predecessor,
        "pid": 43,
        "started_at": "new",
        "model_session_id": "new",
    }
    scopes = [str(tmp_path / "a"), str(tmp_path / "b")]
    state["active_lead"] = predecessor
    state["lead_scopes"] = {"old-session": scopes, "other": [str(tmp_path / "c")]}
    state["handoff"] = {"token": "transfer", "predecessor": predecessor}
    state["dispatch_frozen"] = True
    table = {42: predecessor, 43: successor} if predecessor_alive else {43: successor}
    monkeypatch.setattr(run_state, "processes", lambda: table)
    data: RunRecord = {"token": "transfer", "successor": successor}
    if predecessor_alive:
        with pytest.raises(ValueError, match="Predecessor still alive"):
            run_state.apply(state, "spawner", "handoff-commit", data, path)
        assert state["lead_scopes"]["old-session"] == scopes
        return
    run_state.apply(state, "spawner", "handoff-commit", data, path)
    assert state["lead_scopes"] == {"new": scopes, "other": [str(tmp_path / "c")]}
    assert "draining_leads" not in state


def test_drain_retire_passes_lead_scopes_to_active_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.json"
    state = bootstrap(path)
    lead: RunRecord = {"pid": 42, "pgid": 42, "started_at": "old", "model_session_id": "old"}
    held = str(tmp_path / "held")
    state["active_lead"] = {"pid": 43, "model_session_id": "new"}
    state["lead_scopes"] = {"old": [held], "new": [str(tmp_path / "own")]}
    state["draining_leads"] = {
        "old": {"lead": lead, "scopes": [held], "completion": {"reason": "finished"}}
    }
    state["runs"] = {"lead-old": {"complete": True, "usage_status": "known"}}
    monkeypatch.setattr(run_state, "processes", lambda: {})
    run_state.apply(
        state, "spawner", "drain-retire", {"session_id": "old", "receipt": "done"}, path
    )
    assert state["lead_scopes"] == {"new": sorted([held, str(tmp_path / "own")])}


def test_drain_retire_keeps_scopes_without_active_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.json"
    state = bootstrap(path)
    lead: RunRecord = {"pid": 42, "pgid": 42, "started_at": "old", "model_session_id": "old"}
    held = str(tmp_path / "held")
    state["lead_scopes"] = {"old": [held]}
    state["draining_leads"] = {
        "old": {"lead": lead, "scopes": [held], "completion": {"reason": "finished"}}
    }
    state["runs"] = {"lead-old": {"complete": True, "usage_status": "known"}}
    monkeypatch.setattr(run_state, "processes", lambda: {})
    run_state.apply(
        state, "spawner", "drain-retire", {"session_id": "old", "receipt": "done"}, path
    )
    assert state["lead_scopes"] == {"old": [held]}


@pytest.mark.parametrize(
    "extra",
    [
        ["--run", "OTHER"],
        ["--as", "launcher"],
        ["--data", "{}"],
        ["--run=OTHER"],
        ["--ru", "OTHER"],
        ["--a", "launcher"],
        ["--d", "{}"],
    ],
)
def test_repeated_option_is_refused_before_any_transaction(
    tmp_path: Path, extra: list[str]
) -> None:
    pinned = tmp_path / "pinned.json"
    other = tmp_path / "other.json"
    for path in (pinned, other):
        path.write_text(json.dumps({"blockers": []}))
    before = {path: path.read_text() for path in (pinned, other)}
    data = json.dumps({"id": "b", "actions": ["merge"], "reason": "test"})
    args = [word.replace("OTHER", str(other)) for word in extra]
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--run",
            str(pinned),
            "--as",
            "orchestrator",
            "add-blocker",
            "--data",
            data,
            *args,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "may be given only once" in result.stderr or "unrecognized arguments" in result.stderr
    assert {path: path.read_text() for path in (pinned, other)} == before
