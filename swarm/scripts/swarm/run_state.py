from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Literal, TypedDict


class ProcessInfo(TypedDict):
    pid: int
    ppid: int
    pgid: int
    os_session: int
    status: str
    started_at: str
    command: str


class RunState(TypedDict, total=False):
    active_lead: RunRecord | None
    assignment_history: list[RunRecord]
    assignments: dict[str, RunRecord]
    blockers: list[RunRecord]
    bootstrap: bool
    budget_history: list[RunRecord]
    ceilings: dict[str, int]
    consumed: dict[str, int]
    deadlines: dict[str, str | None]
    dispatch_frozen: bool
    drain_history: list[RunRecord]
    draining_leads: dict[str, RunRecord]
    handoff: RunRecord | None
    handoff_history: list[RunRecord]
    inventory: dict[Literal["threads", "sessions"], list[RunRecord]]
    lead_scopes: dict[str, list[str]]
    lead_session_history: list[RunRecord]
    lifecycle: str
    lifecycle_reason: str
    orchestrator_usage: RunRecord
    orchestrator_usage_migrated: str
    pilot_started_at: str
    reservations: dict[str, RunRecord]
    retirement: RunRecord | None
    retirement_history: list[RunRecord]
    runs: dict[str, RunRecord]
    started_at: str
    updated_at: str
    work_state_path: str


class RunRecord(RunState, total=False):
    aborted_at: str
    actions: list[str]
    active_run: str | None
    agent_id: str
    allocation_id: str
    allowance: int
    assignment_id: str
    at: str
    cache_reads: int
    command: str
    committed_at: str
    complete: bool
    completed_at: str
    completion: RunRecord
    continued_session_id: str
    controller_pid: int
    drain_gates: list[RunRecord]
    drain_scopes: list[str]
    effort: str
    exited_without_completion: bool
    expected_review_modes: list[str]
    gate_pid: int
    gate_stop_receipts: dict[str, str]
    gates: list[RunRecord]
    id: str
    initial: bool
    jobs: list[RunRecord]
    kept_jobs: list[RunRecord]
    lead: RunRecord
    closed_at: str
    resolution: str
    mode: str
    model: str
    model_session_id: str
    new: RunRecord
    new_tokens: int
    notification_receipts: list[RunRecord]
    observed: int
    old: RunRecord | dict[str, int] | None
    os_session: int
    output_paths: list[str]
    owned_processes: dict[str, RunRecord]
    pane: int | str
    pane_id: int | str
    parent_session_id: str | None
    pgid: int
    pid: int
    ppid: int
    predecessor: RunRecord | None
    previous_session_id: str
    process: RunRecord
    reason: str
    receipt: str
    released: bool
    released_scope: str
    reset: bool
    reset_at: str
    reset_reason: str
    retired_at: str
    revision: RunRecord
    role: str
    run_id: str
    scope: str
    scopes: list[str]
    session_id: str
    stage: str
    stage_boundaries: list[RunRecord]
    status: str
    successor: RunRecord
    tag: str
    telemetry_at: str
    thread_id: str
    token: str
    transcript_path: str
    usage_snapshots: list[RunRecord]
    usage_status: str
    worktree: str


DEFAULT_RUN = Path("/tmp/factory-fp-0925/swarm/interior-p1/run.json")
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
        "reconcile-lead-session",
        "handoff-abort",
        "register-lead-scopes",
        "release-drain-scope",
    },
    "launcher": {"admit", "record-run", "release", "assign"},
    "spawner": {
        "freeze-lead",
        "handoff-start",
        "handoff-commit",
        "handoff-abort",
        "retire-start",
        "retire-commit",
        "drain-ready",
        "drain-retire",
        "drain-retire-start",
        "drain-gate-stop",
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


def utc() -> str:
    return datetime.now(UTC).isoformat()


def processes() -> dict[int, ProcessInfo]:
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,pgid=,sess=,stat=,lstart=,command="], text=True
    )
    result: dict[int, ProcessInfo] = {}
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


def same_identity(record: Mapping[str, object], live: Mapping[str, object] | None) -> bool:
    if not live:
        return False
    return all(
        record.get(k) == live.get(k) for k in ("pid", "pgid", "os_session", "started_at", "command")
    )


def lead_transcript_path(state: RunState) -> Path | None:
    active = state.get("active_lead") or {}
    original_path = active.get("transcript_path") or next(
        (
            row.get("transcript_path")
            for row in state.get("inventory", {}).get("sessions", [])
            if row.get("session_id") == active.get("model_session_id") and row.get("role") == "lead"
        ),
        None,
    )
    return Path(original_path) if original_path else None


def continues_lead_session(state: RunState, session: str, transcript: Path) -> bool:
    original = lead_transcript_path(state)
    if original is None:
        return False
    has_session = False
    has_history = False
    try:
        with original.open() as history, transcript.open() as continued:
            history_lines = sum(1 for _ in history)
            if sum(1 for _ in islice(continued, history_lines)) < history_lines:
                return False
            history.seek(0)
            continued.seek(0)
            for line in history:
                source: dict[str, object] = json.loads(line)
                candidate: dict[str, object] = json.loads(next(continued, ""))
                if not isinstance(source, dict) or not isinstance(candidate, dict):
                    return False
                candidate_session = candidate.pop("sessionId", None)
                if candidate_session and candidate_session != session:
                    return False
                has_session = has_session or candidate_session == session
                source.pop("sessionId", None)
                if source != candidate:
                    return False
                has_history = True
    except (OSError, ValueError, TypeError):
        return False
    return has_history and has_session


def daemon_lead_matches(state: RunState, spare: ProcessInfo, table: dict[int, ProcessInfo]) -> bool:
    host = table.get(spare["ppid"])
    if not host:
        return False
    host_words = host["command"].split()
    if (
        len(host_words) < 2
        or Path(host_words[0]).name != "claude"
        or host_words[1] != "bg-pty-host"
    ):
        return False
    active = state.get("active_lead") or {}
    try:
        roster = json.loads((Path.home() / ".claude/daemon/roster.json").read_text())
        for worker in roster["workers"].values():
            if (
                worker.get("replPid") != spare["pid"]
                or worker.get("replProcStart") != spare["started_at"]
                or worker.get("pid") != host["pid"]
                or worker.get("procStart") != host["started_at"]
            ):
                continue
            session = worker.get("sessionId")
            if not isinstance(session, str) or not session or Path(session).name != session:
                continue
            if session == active.get("model_session_id"):
                return True
            original = lead_transcript_path(state)
            if original and continues_lead_session(
                state, session, original.with_name(session + ".jsonl")
            ):
                return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return False


def originating_lead(state: RunState) -> RunRecord:
    active = state.get("active_lead")
    if not active:
        raise ValueError("No active lead")
    table = processes()
    pid = os.getpid()
    visited = set()
    spare: ProcessInfo | None = None
    while pid in table and pid not in visited:
        visited.add(pid)
        row = table[pid]
        if pid == active.get("pid"):
            if not same_identity(active, row):
                raise ValueError("Active lead identity changed")
            return {**active, **row}
        words = row["command"].split()
        if len(words) > 1 and Path(words[0]).name == "claude" and words[1] == "bg-spare":
            spare = row
        pid = row["ppid"]

    active_pid = active.get("pid")
    if (
        spare
        and active_pid is not None
        and same_identity(active, table.get(active_pid))
        and daemon_lead_matches(state, spare, table)
    ):
        return active.copy()
    raise ValueError("Caller is not a descendant of the active lead")


def add_refusal_note(
    error: ValueError, rows: Mapping[str, object], reduced_rows: Mapping[str, object]
) -> None:
    detail = json.dumps(rows)
    if len(detail.encode("utf-8")) > 2048:
        detail = json.dumps(reduced_rows)
    if len(detail.encode("utf-8")) <= 2048:
        error.add_note(detail)


def print_refusal_note(error: BaseException | None) -> None:
    notes = getattr(error, "__notes__", [])
    if notes:
        print(notes[0], file=sys.stderr)


def check_action(state: RunState, path: Path, action: str, require_lead: bool = False) -> None:
    if path.with_name("PAUSE").exists():
        raise ValueError("PAUSE exists")
    if state.get("lifecycle") not in {"preparing", "running"}:
        raise ValueError("Lifecycle does not permit work")
    for blocker in state.get("blockers", []):
        if blocker.get("status", "open") == "open" and action in blocker.get("actions", []):
            error = ValueError("Blocked by " + blocker["id"])
            add_refusal_note(
                error,
                {"blockers": [blocker]},
                {"blockers": [{"id": blocker["id"], "status": blocker.get("status", "open")}]},
            )
            raise error
    for deadline in state.get("deadlines", {}).values():
        if deadline and datetime.fromisoformat(deadline) <= datetime.now(UTC):
            raise ValueError("Recorded deadline reached")
    if require_lead:
        originating_lead(state)
        if state.get("dispatch_frozen"):
            raise ValueError("Lead dispatch is frozen")


def stage(state: RunState) -> str:
    return "pilot" if state.get("pilot_started_at") else "preparation"


def budget(state: RunState, extra: int = 0) -> None:
    for run_id, run in state.get("runs", {}).items():
        if run.get("usage_status") == "unknown":
            error = ValueError("Usage is unknown for an inventoried run")
            add_refusal_note(
                error,
                {"runs": {run_id: run}},
                {"runs": {run_id: {"usage_status": "unknown"}}},
            )
            raise error
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


def admit(state: RunState, data: RunRecord, path: Path, role: str) -> None:
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
    assignment = state.get("assignments", {}).get(allocation, {})
    scope = assignment.get("worktree")
    if state.get("draining_leads") and worker_role == "builder":
        if not scope:
            raise ValueError("Independent builder requires an explicit worktree scope")
        candidate_scope = Path(scope).resolve()
        for drain in state["draining_leads"].values():
            for held in drain["scopes"]:
                held_scope = Path(held).resolve()
                if candidate_scope.is_relative_to(held_scope) or held_scope.is_relative_to(
                    candidate_scope
                ):
                    raise ValueError("Builder scope overlaps a draining lead")
    if worker_role == "builder":
        active_builders = [
            r
            for r in state.get("reservations", {}).values()
            if r.get("role") == "builder" and r.get("active_run")
        ]
        if (
            not state.get("reservations", {}).get(allocation, {}).get("active_run")
            and len(active_builders) >= 8
        ):
            raise ValueError("Eight builders are already active")
    if not isinstance(allowance, int) or allowance <= 0:
        raise ValueError("Allowance must be positive")
    parent = (state.get("active_lead") or {}).get("model_session_id")
    existing = state.get("reservations", {}).get(allocation)
    if existing and existing.get("parent_session_id") not in (None, parent):
        raise ValueError("Allocation belongs to another lead")
    if worker_role == "reviewer" and any(
        key != allocation
        and reservation["role"] == "reviewer"
        and reservation.get("parent_session_id") == parent
        and reservation.get("active_run")
        and not reservation.get("released")
        for key, reservation in state.get("reservations", {}).items()
    ):
        raise ValueError("Reviewer needs one reserved allocation with exact output paths")
    state.setdefault("reservations", {})
    if existing:
        if existing.get("released") or existing["role"] != worker_role:
            raise ValueError("Allocation released or role changed")
        if existing.get("active_run") not in (None, run_id):
            error = ValueError("Concurrent allocation use")
            add_refusal_note(
                error,
                {"reservations": {allocation: existing}},
                {"reservations": {allocation: {"active_run": existing.get("active_run")}}},
            )
            raise error
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
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in tag
        ):
            raise ValueError("Invalid reviewer tag")
        candidate = json.loads(path.with_name(tag + "-candidate.json").read_text())
        state["reservations"][allocation].update(
            {
                "parent_session_id": (state.get("active_lead") or {}).get("model_session_id"),
                "output_paths": [
                    str(path.with_name(tag + "-" + mode + "-review.json").resolve())
                    for mode in candidate["expected_review_modes"]
                ],
            }
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
    reservation = state["reservations"][allocation]
    parent = (state.get("active_lead") or {}).get("model_session_id")
    if reservation.get("parent_session_id") not in (None, parent):
        raise ValueError("Allocation belongs to another lead")
    if parent:
        reservation["parent_session_id"] = parent
    if scope:
        reservation["worktree"] = str(Path(scope).resolve())
        if parent:
            scopes = state.setdefault("lead_scopes", {}).setdefault(parent, [])
            if reservation["worktree"] not in scopes:
                scopes.append(reservation["worktree"])


def record_run(state: RunState, data: RunRecord, role: str) -> None:
    runs = state.setdefault("runs", {})
    reservation: RunRecord | None = None
    if data.get("allocation_id"):
        run = runs[data["run_id"]]
        if data["allocation_id"] != run["allocation_id"]:
            raise ValueError("Wrong allocation")
        reservation = state["reservations"][run["allocation_id"]]
    else:
        if not (
            (role == "orchestrator" and data.get("role") in {"orchestrator", "lead"})
            or (role == "launcher" and data.get("role") == "reviewer")
        ):
            raise ValueError("Unallocated usage requires orchestrator or reviewer launcher role")
        key: Literal["thread_id", "session_id"] = (
            "thread_id" if data.get("thread_id") else "session_id"
        )
        group: Literal["threads", "sessions"] = "threads" if key == "thread_id" else "sessions"
        rows = state.get("inventory", {}).get(group, [])
        row = next(
            (
                r
                for r in rows
                if data.get(key) and r.get(key) == data[key] and r.get("role") == data["role"]
            ),
            None,
        )
        if row is None:
            raise ValueError("Usage identity is not inventoried")
        if data.get("transcript_path"):
            if row.get("role") != "lead" or not row.get("transcript_path"):
                row["transcript_path"] = data["transcript_path"]
            elif Path(row["transcript_path"]).resolve() != Path(data["transcript_path"]).resolve():
                raise ValueError("Inventory transcript conflicts with the recorded one")
        if "complete" in data:
            row["complete"] = data["complete"]
        existing = [
            r for r in runs.values() if r.get(key) == data[key] and not r.get("allocation_id")
        ]
        if existing and data["run_id"] not in runs:
            raise ValueError("Inventory identity already has an accounting run")
        if data["run_id"] not in runs:
            old: RunRecord = {}
            if data["role"] == "orchestrator" and not state.get("orchestrator_usage_migrated"):
                old = state.get("orchestrator_usage", {})
                state["orchestrator_usage_migrated"] = data[key]
            runs[data["run_id"]] = {
                **({"thread_id": data[key]} if key == "thread_id" else {"session_id": data[key]}),
                "role": data["role"],
                "new_tokens": old.get("new_tokens", 0),
                "cache_reads": old.get("cache_reads", 0),
                "stage": stage(state),
            }
        run = runs[data["run_id"]]
        if run.get(key) != data[key] or run.get("role") != data["role"] or run.get("allocation_id"):
            raise ValueError("Accounting run identity changed")
    if data.get("usage_status") == "unknown":
        if "new_tokens" in data or "cache_reads" in data:
            raise ValueError("Unknown final usage must preserve observed counters")
        run.update({**data, "updated_at": utc()})
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
        for counter in ("overall", run["stage"]):
            state["consumed"][counter] = state["consumed"].get(counter, 0) + delta
        if reservation is None and data["role"] == "orchestrator":
            total = state.setdefault("orchestrator_usage", {"new_tokens": 0, "cache_reads": 0})
            total["new_tokens"] += delta
            total["cache_reads"] += cache_delta
            total["at"] = data.get("telemetry_at", utc())
        run.update({**data, "usage_status": "known", "updated_at": utc()})
    if reservation is not None:
        if data.get("complete"):
            reservation["active_run"] = None
        inventory = state.setdefault("inventory", {"threads": [], "sessions": []})
        key = "thread_id" if data.get("thread_id") else "session_id"
        if data.get(key):
            group = "threads" if key == "thread_id" else "sessions"
            row = next((row for row in inventory[group] if row.get(key) == data[key]), None)
            if row is None:
                if key == "thread_id":
                    row = {"thread_id": data[key], "role": reservation["role"]}
                else:
                    row = {"session_id": data[key], "role": reservation["role"]}
                inventory[group].append(row)
            if "transcript_path" in data:
                row["transcript_path"] = data["transcript_path"]
            if "complete" in data:
                row["complete"] = data["complete"]
            if "agent_id" in data:
                row["agent_id"] = data["agent_id"]
            if reservation["role"] == "reviewer":
                if "parent_session_id" in reservation:
                    row["parent_session_id"] = reservation["parent_session_id"]
                if "output_paths" in reservation:
                    row["output_paths"] = reservation["output_paths"]


def assign(state: RunState, data: RunRecord, role: str) -> None:
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
    allocation = data.get("allocation_id")
    reservation = state.get("reservations", {}).get(allocation) if allocation is not None else None
    if reservation and data.get("allowance") != reservation["allowance"]:
        if not old or any(
            data.get(k) != old.get(k) for k in ("allocation_id", "model", "effort", "thread_id")
        ):
            raise ValueError("Allowance increase requires unchanged assignment identity")
        allowance = data.get("allowance")
        if type(allowance) is not int or allowance <= reservation["allowance"]:
            raise ValueError("Existing allowance can only increase")
        if reservation.get("released") or reservation.get("active_run") is not None:
            raise ValueError("Allowance increase requires an inactive unreleased allocation")
        budget(state, allowance - reservation["allowance"])
        reservation["allowance"] = allowance
    state.setdefault("assignment_history", []).append({"at": utc(), "old": old, "new": data})
    assignments[key] = data


def inherit_scopes(state: RunState, predecessor: str) -> None:
    successor = (state.get("active_lead") or {}).get("model_session_id")
    if not successor or predecessor not in state.get("lead_scopes", {}):
        return
    scopes = state["lead_scopes"].pop(predecessor)
    state["lead_scopes"][successor] = sorted(
        set(state["lead_scopes"].get(successor, [])) | set(scopes)
    )


def handoff(state: RunState, data: RunRecord, operation: str, path: Path) -> None:
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
    predecessor: RunRecord | None
    scopes: list[str] | None
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
        if data.get("reset"):
            if not transfer.get("aborted_at") or not state.get("dispatch_frozen"):
                raise ValueError("Only an aborted frozen handoff may be reset")
            if not data.get("reason"):
                raise ValueError("Reset requires a recovery reason")
            table = processes()
            predecessor = transfer.get("predecessor") or {}
            active = state.get("active_lead") or {}
            active_pid = active.get("pid")
            live = table.get(active_pid) if active_pid is not None else None
            if active != predecessor or not same_identity(active, live):
                raise ValueError("Reset predecessor identity changed")
            controller = data.get("controller_pid")
            if type(controller) is not int or controller <= 0 or controller in table:
                raise ValueError("Reset requires the failed controller to have exited")
            successor = transfer.get("successor") or {}
            if successor.get("pid") in table:
                raise ValueError("Reset successor survives or identity is uncertain")
            if transfer.get("jobs") or any(
                r.get("active_run") for r in state.get("reservations", {}).values()
            ):
                raise ValueError("Reset requires drained jobs")
            if any(
                r.get("role") == "reviewer" and not r.get("complete")
                for r in state.get("inventory", {}).get("sessions", [])
            ):
                raise ValueError("Reset requires drained reviewers")
            budget(state)
            state.setdefault("handoff_history", []).append(
                {
                    **transfer,
                    "reset_at": utc(),
                    "reset_reason": data["reason"],
                    "controller_pid": controller,
                }
            )
            state["handoff"] = None
            state["dispatch_frozen"] = False
            return
        state["dispatch_frozen"] = True
        transfer.update({"aborted_at": utc(), "reason": data["reason"]})
    elif operation == "handoff-commit":
        predecessor = transfer.get("predecessor")
        table = processes()
        concurrent = transfer.get("mode") == "concurrent"
        if concurrent:
            scopes = transfer.get("drain_scopes")
            if not scopes or not all(Path(p).is_absolute() for p in scopes):
                raise ValueError("Concurrent handoff requires explicit absolute scopes")
            if not predecessor or not same_identity(predecessor, table.get(predecessor["pid"])):
                raise ValueError("Draining predecessor identity changed")
            session = predecessor["model_session_id"]
            if session in state.get("draining_leads", {}):
                raise ValueError("Predecessor already draining")
        elif predecessor and same_identity(predecessor, table.get(predecessor["pid"])):
            raise ValueError("Predecessor still alive")
        if not same_identity(data["successor"], table.get(data["successor"]["pid"])):
            raise ValueError("Successor identity mismatch")
        state["active_lead"] = data["successor"]
        if not concurrent and predecessor:
            inherit_scopes(state, predecessor["model_session_id"])
        if concurrent and predecessor is not None and scopes is not None:
            state.setdefault("draining_leads", {})[session] = {
                "lead": predecessor,
                "scopes": scopes,
                "successor": data["successor"],
                "token": transfer["token"],
                "jobs": transfer.get("kept_jobs", []),
                "status": "draining",
                "gates": transfer.get("drain_gates", []),
            }
        transcript = Path(str(data.get("transcript_path", "")))
        state.setdefault("inventory", {}).setdefault("sessions", []).append(
            {
                "session_id": data["successor"]["model_session_id"],
                "role": "lead",
                "pid": data["successor"]["pid"],
                "started_at": utc(),
                "complete": False,
                **(
                    {"transcript_path": str(transcript)}
                    if transcript.name == data["successor"]["model_session_id"] + ".jsonl"
                    and transcript.is_absolute()
                    and not transcript.is_symlink()
                    and transcript.is_file()
                    else {}
                ),
            }
        )
        state["dispatch_frozen"] = False
        state.setdefault("handoff_history", []).append({**transfer, "committed_at": utc()})
        state["handoff"] = None


def apply(state: RunState, role: str, operation: str, data: RunRecord, path: Path) -> RunState:
    drain: RunRecord | None
    run: RunRecord
    active: RunRecord | None
    retirement: RunRecord | None
    key: Literal["thread_id", "session_id"]
    if operation not in OPERATIONS[role]:
        raise ValueError("Operation not permitted for role")
    if operation == "handoff-abort" and data.get("reset") and role != "orchestrator":
        raise ValueError("Handoff reset requires orchestrator")
    if operation == "register-lead-scopes":
        check_action(state, path, "dispatch")
        lead = state.get("active_lead") or {}
        if data.get("session_id") != lead.get("model_session_id") or not data.get("reason"):
            raise ValueError("Scope registration requires the current lead and reason")
        scopes = data.get("scopes", [])
        if not scopes or not all(isinstance(p, str) and Path(p).is_absolute() for p in scopes):
            raise ValueError("Lead scopes must be explicit absolute paths")
        scopes = sorted({str(Path(p).resolve()) for p in scopes})
        for drain in state.get("draining_leads", {}).values():
            if any(
                Path(a).is_relative_to(Path(b)) or Path(b).is_relative_to(Path(a))
                for a in scopes
                for b in drain["scopes"]
            ):
                raise ValueError("Lead scope overlaps a draining predecessor")
        existing = state.setdefault("lead_scopes", {}).get(data["session_id"], [])
        state["lead_scopes"][data["session_id"]] = sorted(set(existing) | set(scopes))
    elif operation == "release-drain-scope":
        session = data.get("session_id")
        drain = state.get("draining_leads", {}).get(session) if session is not None else None
        scope = data.get("scope")
        if not drain or session is None or scope not in drain["scopes"] or not data.get("reason"):
            raise ValueError("Scope release requires a held drain scope and reason")
        if any(
            r.get("active_run")
            and r.get("parent_session_id") == session
            and Path(r.get("worktree", "/")).resolve().is_relative_to(Path(scope).resolve())
            for r in state.get("reservations", {}).values()
        ):
            raise ValueError("Draining lead still owns an active job in this scope")
        drain["scopes"].remove(scope)
        state.setdefault("drain_history", []).append(
            {"at": utc(), "session_id": session, "released_scope": scope, "reason": data["reason"]}
        )
    elif operation == "drain-ready":
        session = data.get("session_id")
        drain = state.get("draining_leads", {}).get(session) if session is not None else None
        if not drain or drain.get("completion"):
            raise ValueError("Drain completion is missing or already recorded")
        session = data["session_id"]
        if any(
            r.get("active_run") and r.get("parent_session_id") == session
            for r in state.get("reservations", {}).values()
        ):
            raise ValueError("Draining lead still owns active jobs")
        if any(
            r.get("usage_status") == "unknown"
            and (
                state.get("reservations", {}).get(r["allocation_id"], {})
                if "allocation_id" in r
                else {}
            ).get("parent_session_id")
            == session
            for r in state.get("runs", {}).values()
        ):
            raise ValueError("Draining job usage is unknown")
        drain.update({"status": "notification_pending", "completion": data["completion"]})
    elif operation == "drain-gate-stop":
        session = data.get("session_id")
        drain = state.get("draining_leads", {}).get(session) if session is not None else None
        if not drain or not any(g["pid"] == data.get("gate_pid") for g in drain.get("gates", [])):
            raise ValueError("Gate is not owned by this draining lead")
        drain.setdefault("gate_stop_receipts", {})[str(data["gate_pid"])] = data["receipt"]
    elif operation == "drain-retire-start":
        session = data.get("session_id")
        drain = state.get("draining_leads", {}).get(session) if session is not None else None
        retirement = data.get("retirement", RunRecord())
        orphaned = drain is not None and bool(drain) and not drain.get("completion")
        if drain and not drain.get("completion"):
            live = processes().get(drain["lead"]["pid"])
            if retirement is None:
                raise AttributeError("'NoneType' object has no attribute 'get'")
            if not retirement.get("exited_without_completion") or (
                live is not None
                and same_identity(drain["lead"], live)
                and not live.get("status", "").startswith("Z")
            ):
                raise ValueError("Retirement without completion requires an exited lead")
        if not drain:
            raise ValueError("Retirement requires the exact completed drain owner")
        if retirement is None:
            raise AttributeError("'NoneType' object has no attribute 'get'")
        if retirement.get("lead") != drain["lead"]:
            raise ValueError("Retirement requires the exact completed drain owner")
        if not orphaned and not retirement.get("notification_receipts"):
            raise ValueError("Retirement requires verified notification receipts")
        if drain.get("retirement") and drain["retirement"] != retirement:
            raise ValueError("Retirement identity changed")
        drain.update({"status": "retiring", "retirement": retirement})
    elif operation == "drain-retire":
        session = data.get("session_id")
        drain = state.get("draining_leads", {}).get(session) if session is not None else None
        if not drain or session is None:
            raise ValueError("Drain completion notifications are required")
        retirement = drain.get("retirement", RunRecord())
        if not drain.get("completion") and retirement is None:
            raise AttributeError("'NoneType' object has no attribute 'get'")
        if not (
            drain.get("completion")
            or (retirement is not None and retirement.get("exited_without_completion"))
        ):
            raise ValueError("Drain completion notifications are required")
        if same_identity(drain["lead"], processes().get(drain["lead"]["pid"])):
            raise ValueError("Draining lead still alive")
        run = state.get("runs", {}).get("lead-" + session, {})
        if not run.get("complete") or run.get("usage_status") != "known":
            raise ValueError("Draining lead final accounting is incomplete")
        state.setdefault("drain_history", []).append(
            {**drain, "retired_at": utc(), "receipt": data["receipt"]}
        )
        del state["draining_leads"][session]
        inherit_scopes(state, session)
    elif operation == "adopt-bootstrap":
        for field in ("started_at", "lifecycle", "inventory", "ceilings", "deadlines", "blockers"):
            if field not in state:
                raise ValueError("Invalid bootstrap: " + field)
        if state["lifecycle"] not in LIFECYCLES:
            raise ValueError("Invalid lifecycle")
        state["bootstrap"] = False
    elif operation == "reconcile-lead-session":
        check_action(state, path, "dispatch")
        active = state.get("active_lead") or {}
        if state.get("dispatch_frozen") or state.get("handoff") or state.get("retirement"):
            raise ValueError("Lead transfer or retirement is pending")
        if any(r.get("active_run") for r in state.get("reservations", {}).values()):
            raise ValueError("Finish active workers before reconciling lead session")
        if active.get("model_session_id") != data.get("previous_session_id") or active.get(
            "pid"
        ) != data.get("pid"):
            raise ValueError("Active lead process identity changed")
        table = processes()
        active_pid = active.get("pid")
        live = table.get(active_pid) if active_pid is not None else None
        if not same_identity(active, live):
            raise ValueError("Active lead process identity changed")
        session = data.get("session_id")
        transcript = Path(data.get("transcript_path", ""))
        if (
            not session
            or session == active.get("model_session_id")
            or transcript.name != session + ".jsonl"
            or not transcript.is_file()
            or not data.get("reason")
        ):
            raise ValueError("Actual session transcript and reason are required")
        rows = state.setdefault("inventory", {}).setdefault("sessions", [])
        if any(row.get("session_id") == session for row in rows):
            raise ValueError("Session identity already registered")
        state.setdefault("lead_session_history", []).append(
            {**data, "at": utc(), "process": active.copy()}
        )
        active["model_session_id"] = session
        rows.append(
            {
                "session_id": session,
                "role": "lead",
                "pid": active["pid"],
                "transcript_path": str(transcript),
                "started_at": utc(),
                "complete": False,
            }
        )
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
            state.setdefault("retirement_history", []).append({**retirement, "completed_at": utc()})
            state["retirement"] = None
    elif operation == "register-inventory":
        key = "thread_id" if data.get("thread_id") else "session_id"
        if not data.get(key) or data.get("role") not in {"orchestrator", "lead"}:
            raise ValueError("Inventory requires an identity and role")
        if "continued_session_id" in data:
            continued = data["continued_session_id"]
            if not isinstance(continued, str) or not continued.strip() or continued == data[key]:
                raise ValueError("Inventory continued_session_id must name a different session")
        group: Literal["threads", "sessions"] = "threads" if key == "thread_id" else "sessions"
        rows = state.setdefault("inventory", {}).setdefault(group, [])
        old = next((row for row in rows if row.get(key) == data[key]), None)
        if old is not None and old != data:
            pane = data.get("pane_id")
            if (
                old.get("role") == "orchestrator"
                and type(pane) is int
                and pane >= 0
                and (
                    ("pane_id" not in old and str(old.get("pane")) == str(pane))
                    or old.get("pane_id") == str(pane)
                )
                and data == {**old, "pane_id": pane}
            ):
                old["pane_id"] = pane
            else:
                raise ValueError("Inventory identity already registered")
        if old is None:
            pane = data.get("pane_id")
            if "pane_id" in data and (type(pane) is not int or pane < 0):
                raise ValueError("Inventory pane_id must be a non-negative integer")
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
                raise ValueError("Pilot start requires usage snapshots for active inventory runs")
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
        state.update({"lifecycle": data["lifecycle"], "lifecycle_reason": data["reason"]})
        if data["lifecycle"] == "running":
            state.setdefault("pilot_started_at", utc())
    elif operation == "add-blocker":
        if any(b["id"] == data["id"] for b in state["blockers"]):
            raise ValueError("Duplicate blocker")
        state["blockers"].append({**data, "status": "open"})
    elif operation == "close-blocker":
        blocker = next(b for b in state["blockers"] if b["id"] == data["id"])
        blocker.update({"status": "closed", "resolution": data["reason"], "closed_at": utc()})
    elif operation == "set-ceilings":
        if not data.get("reason"):
            raise ValueError("Budget revision requires evidence")
        state.setdefault("budget_history", []).append(
            {"at": utc(), "old": state["ceilings"].copy(), "revision": data}
        )
        state["ceilings"].update(data.get("ceilings", {}))
        if data.get("allocation_id"):
            state["reservations"][data["allocation_id"]]["allowance"] = data["allowance"]
    elif operation == "admit":
        admit(state, data, path, role)
    elif operation == "record-run":
        record_run(state, data, role)
    elif operation == "release":
        reservation = state["reservations"][data["allocation_id"]]
        if reservation.get("active_run") not in (None, data["run_id"]):
            raise ValueError("Wrong active run")
        reservation.update({"released": True, "active_run": None})
    elif operation == "assign":
        assign(state, data, role)
    elif operation.startswith("handoff-") or operation == "freeze-lead":
        handoff(state, data, operation, path)
    state["updated_at"] = utc()
    return state


def transact(path: Path, role: str, operation: str, data: RunRecord) -> RunState:
    path.parent.mkdir(parents=True, exist_ok=True)
    state: RunState
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


def main() -> None:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--as", dest="role", choices=OPERATIONS, required=True)
    parser.add_argument("operation")
    parser.add_argument("--data", default="{}")
    for option in ("--run", "--as", "--data"):
        if sum(word == option or word.startswith(option + "=") for word in sys.argv[1:]) > 1:
            parser.error(option + " may be given only once")
    args = parser.parse_args()
    try:
        data: RunRecord = json.loads(args.data)
        result = transact(args.run, args.role, args.operation, data)
    except (ValueError, KeyError, OSError, StopIteration) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        print_refusal_note(error)
        parser.exit(2)
    key: str | None = None
    if args.operation in {"admit", "record-run", "release"}:
        key = data.get("run_id") or data.get("allocation_id")
    elif args.operation == "assign":
        key = data.get("assignment_id")
    elif args.operation == "set-ceilings":
        key = data.get("allocation_id")
    elif args.operation == "register-inventory":
        key = data.get("thread_id") or data.get("session_id")
    elif args.operation in {
        "register-lead-scopes",
        "reconcile-lead-session",
        "release-drain-scope",
    } or args.operation.startswith("drain-"):
        key = data.get("session_id")
    elif args.operation == "handoff-commit":
        key = data.get("successor", {}).get("model_session_id")
    elif args.operation in {"freeze-lead", "retire-start", "retire-commit"}:
        owner = data.get("predecessor") or result.get("active_lead") or {}
        key = owner.get("model_session_id")
    print(json.dumps({"ok": True, "operation": args.operation, "key": key}))


if __name__ == "__main__":
    main()
