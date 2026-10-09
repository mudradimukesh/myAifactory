import argparse
import fcntl
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from swarm_config import RUN

DEFAULT_RUN = RUN
OPERATIONS = {
    "orchestrator": {
        "init",
        "adopt-bootstrap",
        "set-lifecycle",
        "add-blocker",
        "close-blocker",
        "set-ceilings",
        "admit",
        "record-run",
        "release",
        "assign",
        "register-inventory",
    },
    "launcher": {"admit", "record-run", "release", "assign"},
    "spawner": {
        "freeze-lead",
        "handoff-start",
        "handoff-commit",
        "handoff-abort",
        "retire-start",
        "retire-commit",
    },
}
LIFECYCLES = {
    "orienting",
    "preparing",
    "running",
    "paused",
    "budget_exhausted",
    "complete",
    "failed",
}


def utc():
    return datetime.now(UTC).isoformat()


def processes():
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,pgid=,sess=,stat=,lstart=,command="], text=True
    )
    result = {}
    for line in output.splitlines():
        parts = line.split(None, 10)
        if len(parts) == 11:
            pid, ppid, pgid, session = map(int, parts[:4])
            result[pid] = {
                "pid": pid,
                "ppid": ppid,
                "pgid": pgid,
                "os_session": session,
                "status": parts[4],
                "started_at": " ".join(parts[5:10]),
                "command": parts[10],
            }
    return result


def same_identity(record, live):
    return bool(live) and all(
        record.get(k) == live.get(k)
        for k in ("pid", "pgid", "os_session", "started_at", "command")
    )


def originating_lead(state):
    active = state.get("active_lead")
    if not active:
        raise ValueError("No active lead")
    table = processes()
    pid = os.getpid()
    visited = set()
    while pid in table and pid not in visited:
        visited.add(pid)
        row = table[pid]
        if pid == active.get("pid"):
            if not same_identity(active, row):
                raise ValueError("Active lead identity changed")
            return {**active, **row}
        pid = row["ppid"]
    raise ValueError("Caller is not a descendant of the active lead")


def check_action(state, path, action, require_lead=False):
    if path.with_name("PAUSE").exists():
        raise ValueError("PAUSE exists")
    if state.get("lifecycle") not in {"preparing", "running"}:
        raise ValueError("Lifecycle does not permit work")
    for blocker in state.get("blockers", []):
        if blocker.get("status", "open") == "open" and action in blocker.get(
            "actions", []
        ):
            raise ValueError("Blocked by " + blocker["id"])
    for deadline in state.get("deadlines", {}).values():
        if deadline and datetime.fromisoformat(deadline) <= datetime.now(UTC):
            raise ValueError("Recorded deadline reached")
    if require_lead:
        originating_lead(state)
        if state.get("dispatch_frozen"):
            raise ValueError("Lead dispatch is frozen")


def stage(state):
    return "pilot" if state.get("pilot_started_at") else "preparation"


def budget(state, extra=0):
    if any(r.get("usage_status") == "unknown" for r in state.get("runs", {}).values()):
        raise ValueError("Usage is unknown for an inventoried run")
    remaining = sum(
        max(0, r["allowance"] - r["observed"])
        for r in state.get("reservations", {}).values()
        if not r.get("released")
    )
    for key in ("overall", stage(state)):
        used = state.get("consumed", {}).get(key)
        if used is None:
            raise ValueError("Usage is unknown for " + key)
        ceiling = state.get("ceilings", {}).get(key)
        if ceiling is None or used + remaining + extra > ceiling:
            raise ValueError("Allocation requires orchestrator reassessment: " + key)


def admit(state, data, path, role):
    worker_role = data["role"]
    check_action(
        state,
        path,
        "launch:" + worker_role,
        require_lead=role == "launcher" and bool(state.get("active_lead")),
    )
    if state.get("dispatch_frozen") and state.get("active_lead"):
        raise ValueError("Dispatch frozen")
    allocation = data["allocation_id"]
    run_id = data["run_id"]
    allowance = data["allowance"]
    if not isinstance(allowance, int) or allowance <= 0:
        raise ValueError("Allowance must be positive")
    existing = state.setdefault("reservations", {}).get(allocation)
    if existing:
        if existing.get("released") or existing["role"] != worker_role:
            raise ValueError("Allocation released or role changed")
        if existing.get("active_run") not in (None, run_id):
            raise ValueError("Concurrent allocation use")
        if existing["allowance"] != allowance:
            raise ValueError("Allowance change requires orchestrator")
        if run_id in state.get("runs", {}) and state["runs"][run_id].get("complete"):
            raise ValueError("Run already complete; use a unique run id")
        budget(state)
        existing["active_run"] = run_id
    else:
        budget(state, allowance)
        state["reservations"][allocation] = {
            "role": worker_role,
            "allowance": allowance,
            "observed": 0,
            "active_run": run_id,
            "released": False,
            "stage": stage(state),
        }
    if worker_role == "reviewer" and data.get("tag"):
        tag = data["tag"]
        if not tag or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in tag
        ):
            raise ValueError("Invalid reviewer tag")
        candidate = json.loads(path.with_name(tag + "-candidate.json").read_text())
        state["reservations"][allocation].update(
            parent_session_id=(state.get("active_lead") or {}).get("model_session_id"),
            output_paths=[
                str(path.with_name(tag + "-" + mode + "-review.json").resolve())
                for mode in candidate["expected_review_modes"]
            ],
        )
    state.setdefault("runs", {}).setdefault(
        run_id,
        {
            "allocation_id": allocation,
            "new_tokens": 0,
            "cache_reads": 0,
            "started_at": utc(),
            "stage": stage(state),
        },
    )


def record_run(state, data, role):
    runs = state.setdefault("runs", {})
    reservation = None
    if data.get("allocation_id"):
        run = runs[data["run_id"]]
        if data["allocation_id"] != run["allocation_id"]:
            raise ValueError("Wrong allocation")
        reservation = state["reservations"][run["allocation_id"]]
    else:
        if role != "orchestrator" or data.get("role") not in {"orchestrator", "lead"}:
            raise ValueError("Unallocated usage requires orchestrator role")
        key = "thread_id" if data.get("thread_id") else "session_id"
        group = "threads" if key == "thread_id" else "sessions"
        rows = state.get("inventory", {}).get(group, [])
        row = next(
            (
                r
                for r in rows
                if data.get(key)
                and r.get(key) == data[key]
                and r.get("role") == data["role"]
            ),
            None,
        )
        if row is None:
            raise ValueError("Usage identity is not inventoried")
        if data.get("transcript_path"):
            row["transcript_path"] = data["transcript_path"]
        if "complete" in data:
            row["complete"] = data["complete"]
        existing = [
            r
            for r in runs.values()
            if r.get(key) == data[key] and not r.get("allocation_id")
        ]
        if existing and data["run_id"] not in runs:
            raise ValueError("Inventory identity already has an accounting run")
        if data["run_id"] not in runs:
            old = {}
            if data["role"] == "orchestrator" and not state.get(
                "orchestrator_usage_migrated"
            ):
                old = state.get("orchestrator_usage", {})
                state["orchestrator_usage_migrated"] = data[key]
            runs[data["run_id"]] = {
                key: data[key],
                "role": data["role"],
                "new_tokens": old.get("new_tokens", 0),
                "cache_reads": old.get("cache_reads", 0),
                "stage": stage(state),
            }
        run = runs[data["run_id"]]
        if (
            run.get(key) != data[key]
            or run.get("role") != data["role"]
            or run.get("allocation_id")
        ):
            raise ValueError("Accounting run identity changed")
    if data.get("usage_status") == "unknown":
        if "new_tokens" in data or "cache_reads" in data:
            raise ValueError("Unknown final usage must preserve observed counters")
        run.update(data, updated_at=utc())
    else:
        new, cache = data["new_tokens"], data["cache_reads"]
        if any(type(v) is not int or v < 0 for v in (new, cache)):
            raise ValueError("Usage counters must be nonnegative integers")
        if new < run["new_tokens"] or cache < run["cache_reads"]:
            raise ValueError("Run counters cannot decrease")
        delta = new - run["new_tokens"]
        cache_delta = cache - run["cache_reads"]
        if reservation is not None:
            reservation["observed"] += delta
        for key in ("overall", run["stage"]):
            state["consumed"][key] = state["consumed"].get(key, 0) + delta
        if reservation is None and data["role"] == "orchestrator":
            total = state.setdefault(
                "orchestrator_usage", {"new_tokens": 0, "cache_reads": 0}
            )
            total["new_tokens"] += delta
            total["cache_reads"] += cache_delta
            total["at"] = data.get("telemetry_at", utc())
        run.update(data, usage_status="known", updated_at=utc())
    if reservation is not None:
        if data.get("complete"):
            reservation["active_run"] = None
        inventory = state.setdefault("inventory", {"threads": [], "sessions": []})
        key = "thread_id" if data.get("thread_id") else "session_id"
        if data.get(key):
            group = "threads" if key == "thread_id" else "sessions"
            row = next(
                (row for row in inventory[group] if row.get(key) == data[key]), None
            )
            if row is None:
                row = {key: data[key], "role": reservation["role"]}
                inventory[group].append(row)
            row.update(
                {
                    k: data[k]
                    for k in ("transcript_path", "complete", "agent_id")
                    if k in data
                }
            )
            if reservation["role"] == "reviewer":
                row.update(
                    {
                        k: reservation[k]
                        for k in ("parent_session_id", "output_paths")
                        if k in reservation
                    }
                )


def assign(state, data, role):
    key = data["assignment_id"]
    assignments = state.setdefault("assignments", {})
    old = assignments.get(key)
    if not data.get("reason"):
        raise ValueError("Assignment reason required")
    if role == "launcher":
        if not old or any(data.get(k, old[k]) != old[k] for k in ("model", "effort")):
            raise ValueError("Launcher cannot authorize a model change")
        if data.get("thread_id"):
            old["thread_id"] = data["thread_id"]
        return
    if data.get("effort") not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ValueError("Invalid effort")
    state.setdefault("assignment_history", []).append(
        {"at": utc(), "old": old, "new": data}
    )
    assignments[key] = data


def handoff(state, data, operation, path):
    check_action(state, path, "launch:lead")
    if operation == "freeze-lead":
        originating_lead(state)
        if state.get("dispatch_frozen"):
            raise ValueError("Already frozen")
        state["dispatch_frozen"] = True
        state["handoff"] = {
            "token": data["token"],
            "predecessor": state["active_lead"],
            "started_at": utc(),
        }
        return
    transfer = state.get("handoff")
    if operation == "handoff-start" and data.get("initial"):
        if state.get("active_lead") or transfer:
            raise ValueError("Initial lead already exists")
        state["dispatch_frozen"] = True
        state["handoff"] = data
        return
    if not transfer or transfer["token"] != data["token"]:
        raise ValueError("Wrong handoff token")
    if operation == "handoff-start":
        if data.get("successor"):
            successor = data["successor"]
            if not state.get("dispatch_frozen") or not same_identity(
                successor, processes().get(successor["pid"])
            ):
                raise ValueError("Invalid successor registration")
        else:
            originating_lead(state)
        transfer.update(data)
    elif operation == "handoff-abort":
        state["dispatch_frozen"] = True
        transfer.update(aborted_at=utc(), reason=data["reason"])
    elif operation == "handoff-commit":
        predecessor = transfer.get("predecessor")
        table = processes()
        if predecessor and same_identity(predecessor, table.get(predecessor["pid"])):
            raise ValueError("Predecessor still alive")
        if not same_identity(data["successor"], table.get(data["successor"]["pid"])):
            raise ValueError("Successor identity mismatch")
        state["active_lead"] = data["successor"]
        state.setdefault("inventory", {}).setdefault("sessions", []).append(
            {
                "session_id": data["successor"]["model_session_id"],
                "role": "lead",
                "pid": data["successor"]["pid"],
                "started_at": utc(),
                "complete": False,
            }
        )
        state["dispatch_frozen"] = False
        state.setdefault("handoff_history", []).append(
            {**transfer, "committed_at": utc()}
        )
        state["handoff"] = None


def apply(state, role, operation, data, path):
    if operation not in OPERATIONS[role]:
        raise ValueError("Operation not permitted for role")
    if operation == "adopt-bootstrap":
        for key in (
            "started_at",
            "lifecycle",
            "inventory",
            "ceilings",
            "deadlines",
            "blockers",
        ):
            if key not in state:
                raise ValueError("Invalid bootstrap: " + key)
        if state["lifecycle"] not in LIFECYCLES:
            raise ValueError("Invalid lifecycle")
        state["bootstrap"] = False
    elif operation in {"retire-start", "retire-commit"}:
        active = state.get("active_lead")
        if not active or not same_identity(active, data["predecessor"]):
            raise ValueError("Retirement owner changed")
        if operation == "retire-start":
            if any(r.get("active_run") for r in state.get("reservations", {}).values()):
                raise ValueError("Finish active workers before retiring lead")
            state["dispatch_frozen"] = True
            state["retirement"] = data
        else:
            retirement = state.get("retirement")
            if not retirement or not state.get("dispatch_frozen"):
                raise ValueError("Retirement was not started")
            table = processes()
            for identity in retirement["owned_processes"].values():
                live = table.get(identity["pid"])
                if live and not live.get("status", "").startswith("Z"):
                    raise ValueError("Owned process survives or identity is uncertain")
            state["active_lead"] = None
            state["handoff"] = None
            state.setdefault("retirement_history", []).append(
                {**retirement, "completed_at": utc()}
            )
            state["retirement"] = None
    elif operation == "register-inventory":
        key = "thread_id" if data.get("thread_id") else "session_id"
        if not data.get(key) or data.get("role") not in {"orchestrator", "lead"}:
            raise ValueError("Inventory requires an identity and role")
        group = "threads" if key == "thread_id" else "sessions"
        rows = state.setdefault("inventory", {}).setdefault(group, [])
        old = next((row for row in rows if row.get(key) == data[key]), None)
        if old is not None and old != data:
            raise ValueError("Inventory identity already registered")
        if old is None:
            if (
                state.get("orchestrator_usage")
                and not state.get("orchestrator_usage_migrated")
                and data["role"] == "orchestrator"
            ):
                raise ValueError(
                    "Record bootstrap orchestration usage before adding another thread"
                )
            rows.append(data)
    elif operation == "set-lifecycle":
        if data["lifecycle"] not in LIFECYCLES:
            raise ValueError("Invalid lifecycle")
        if path.with_name("PAUSE").exists() and data["lifecycle"] != "paused":
            raise ValueError("Archive PAUSE after explicit resume first")
        if data["lifecycle"] == "running" and not state.get("pilot_started_at"):
            unallocated = {
                k
                for k, r in state.get("runs", {}).items()
                if not r.get("allocation_id") and not r.get("complete")
            }
            snapshots = data.get("usage_snapshots", [])
            if {r["run_id"] for r in snapshots} != unallocated:
                raise ValueError(
                    "Pilot start requires usage snapshots for active inventory runs"
                )
            if any(r.get("active_run") for r in state.get("reservations", {}).values()):
                raise ValueError("Preparation workers must finish before pilot start")
            for snapshot in snapshots:
                record_run(state, snapshot, role)
            budget(state)
            for run_id in unallocated:
                run = state["runs"][run_id]
                run.setdefault("stage_boundaries", []).append(
                    {
                        "at": utc(),
                        "stage": run["stage"],
                        "new_tokens": run["new_tokens"],
                        "cache_reads": run["cache_reads"],
                    }
                )
                run["stage"] = "pilot"
        if data.get("work_state_path"):
            state["work_state_path"] = str(Path(data["work_state_path"]).resolve())
        state.update(lifecycle=data["lifecycle"], lifecycle_reason=data["reason"])
        if data["lifecycle"] == "running":
            state.setdefault("pilot_started_at", utc())
    elif operation == "add-blocker":
        if any(b["id"] == data["id"] for b in state["blockers"]):
            raise ValueError("Duplicate blocker")
        state["blockers"].append({**data, "status": "open"})
    elif operation == "close-blocker":
        blocker = next(b for b in state["blockers"] if b["id"] == data["id"])
        blocker.update(status="closed", resolution=data["reason"], closed_at=utc())
    elif operation == "set-ceilings":
        if not data.get("reason"):
            raise ValueError("Budget revision requires evidence")
        state.setdefault("budget_history", []).append(
            {"at": utc(), "old": state["ceilings"].copy(), "revision": data}
        )
        state["ceilings"].update(data.get("ceilings", {}))
        if data.get("allocation_id"):
            state["reservations"][data["allocation_id"]]["allowance"] = data[
                "allowance"
            ]
    elif operation == "admit":
        admit(state, data, path, role)
    elif operation == "record-run":
        record_run(state, data, role)
    elif operation == "release":
        reservation = state["reservations"][data["allocation_id"]]
        if reservation.get("active_run") not in (None, data["run_id"]):
            raise ValueError("Wrong active run")
        reservation.update(released=True, active_run=None)
    elif operation == "assign":
        assign(state, data, role)
    elif operation.startswith("handoff-") or operation == "freeze-lead":
        handoff(state, data, operation, path)
    state["updated_at"] = utc()
    return state


def transact(path, role, operation, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if operation == "init":
            if role != "orchestrator" or path.exists():
                raise ValueError("Init refuses an existing run or wrong role")
            state = data
            apply(state, role, "adopt-bootstrap", {}, path)
        else:
            state = json.loads(path.read_text())
            apply(state, role, operation, data, path)
        fd, name = tempfile.mkstemp(prefix=".run-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(state, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--as", dest="role", choices=OPERATIONS, required=True)
    parser.add_argument("operation")
    parser.add_argument("--data", default="{}")
    args = parser.parse_args()
    try:
        result = transact(args.run, args.role, args.operation, json.loads(args.data))
    except (ValueError, KeyError, OSError, StopIteration) as error:
        parser.exit(2, f"REFUSED: {error}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
