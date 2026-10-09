import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/swarm/run_state.py"


def invoke(path, role, operation, payload):
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
        check=False,
    )


def bootstrap(path):
    state = {
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


def test_roles_and_pause_fail_closed(tmp_path):
    path = tmp_path / "run.json"
    bootstrap(path)
    assert (
        invoke(path, "launcher", "set-lifecycle", {"lifecycle": "complete"}).returncode
        != 0
    )
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
    assert (
        invoke(path, "launcher", "admit", {**payload, "run_id": "other"}).returncode
        != 0
    )
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
    assert (
        invoke(path, "launcher", "admit", {**payload, "run_id": "r2"}).returncode == 0
    )
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
        invoke(
            path, "orchestrator", "record-run", {**data, "thread_id": "other"}
        ).returncode
        != 0
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
        {
            "allocation_id": "a",
            "run_id": "r",
            "usage_status": "unknown",
            "complete": True,
        },
    )
    assert result.returncode == 0, result.stderr
    assert (
        invoke(
            path, "launcher", "release", {"allocation_id": "a", "run_id": "r"}
        ).returncode
        == 0
    )
    assert (
        invoke(
            path, "launcher", "admit", {**data, "allocation_id": "b", "run_id": "s"}
        ).returncode
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
        invoke(
            path, "launcher", "admit", {**data, "allocation_id": "b", "run_id": "s"}
        ).returncode
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
    assert (
        invoke(
            path, "orchestrator", "record-run", {**data, "new_tokens": 32}
        ).returncode
        == 0
    )
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
    entry = {
        "thread_id": "new",
        "role": "orchestrator",
        "rollout_path": "/tmp/new.jsonl",
    }
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
    allocation = {
        "allocation_id": "a",
        "run_id": "r",
        "role": "builder",
        "allowance": 20,
    }
    assert invoke(path, "launcher", "admit", allocation).returncode == 0
    observed = {"allocation_id": "a", "run_id": "r", "new_tokens": 5, "cache_reads": 7}
    assert invoke(path, "launcher", "record-run", observed).returncode == 0
    unknown = {
        "allocation_id": "a",
        "run_id": "r",
        "usage_status": "unknown",
        "complete": True,
    }
    assert invoke(path, "launcher", "record-run", unknown).returncode == 0
    saved = json.loads(path.read_text())
    assert saved["runs"]["r"]["new_tokens"] == 5
    assert saved["consumed"]["overall"] == 15
    assert (
        invoke(
            path,
            "launcher",
            "record-run",
            {**observed, "new_tokens": 8, "complete": True},
        ).returncode
        == 0
    )
    assert json.loads(path.read_text())["consumed"]["overall"] == 18
