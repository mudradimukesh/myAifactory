import contextlib
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from owned_processes import capture, stop
from run_state import (
    DEFAULT_RUN,
    budget,
    check_action,
    originating_lead,
    print_refusal_note,
    processes,
    same_identity,
    transact,
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def validate_pinned_state(path, transfer):
    if "state_path" not in transfer and "state_sha256" not in transfer:
        return
    expected = path.parent / "handoff" / (transfer["token"] + "-state.md")
    if transfer.get("state_path") != str(expected) or not expected.resolve().is_relative_to(
        (path.parent / "handoff").resolve()
    ):
        raise ValueError("Pinned state path differs")
    try:
        actual = sha(expected)
    except OSError as error:
        raise ValueError("Pinned state missing or unreadable") from error
    if actual != transfer.get("state_sha256"):
        raise ValueError("Pinned state hash differs")


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temporary, path)


def validate_ack(transfer, ack, table):
    if any(ack.get(k) != transfer[k] for k in ("token", "snapshot_hash")):
        raise ValueError("Acknowledgement token or snapshot hash differs")
    if transfer.get("mode") == "concurrent":
        successor = transfer.get("successor") or {}
        if (
            ack.get("successor_session_id") != successor.get("model_session_id")
            or ack.get("successor_pid") != successor.get("pid")
            or not same_identity(successor, table.get(successor.get("pid")))
        ):
            raise ValueError("Acknowledgement successor identity differs")
    jobs = transfer.get("jobs", [])
    adopted = ack.get("jobs", [])
    if len(adopted) != len(jobs) or {j["pid"] for j in jobs} != {j["pid"] for j in adopted}:
        raise ValueError("Acknowledgement must cover every recorded job exactly once")
    for job in jobs:
        row = next(j for j in adopted if j["pid"] == job["pid"])
        if not same_identity(job, row):
            raise ValueError("Adopted job identity differs")
        if job["pid"] in table:
            if not same_identity(job, table[job["pid"]]):
                raise ValueError("Live job identity differs")
        else:
            receipt = job.get("receipt")
            if not receipt or not Path(receipt).is_file():
                raise ValueError("Completed job has no preserved receipt")
            result = read(Path(receipt))
            if (
                result.get("remaining_processes") != []
                or result.get("child_exit") is None
                or not result.get("ended_at")
            ):
                raise ValueError("Completed job receipt is incomplete")


def launch(path, env, token):
    state = read(path)
    check_action(state, path, "launch:lead")
    budget(state)
    session = str(uuid.uuid4())
    repo = Path(env["REPO"])
    brief = Path(env["LEAD_BRIEF"])
    prompt = brief.read_text() + "\nPilot handoff token: " + token + "."
    transfer = state.get("handoff") or {}
    validate_pinned_state(path, transfer)
    if transfer.get("state_path"):
        prompt += (
            "\nComplete historical state is preserved at "
            + transfer["state_path"]
            + ", SHA-256 "
            + transfer["state_sha256"]
            + ". Do not load it during startup. "
            + "Read a relevant section only when a current task needs it."
        )
    if transfer.get("snapshot_path") and transfer.get("ack_path"):
        prompt += (
            "\nSuccessor startup rule: no Bash before adoption. Use Read on "
            + transfer["snapshot_path"]
            + ". Snapshot hash: "
            + transfer["snapshot_hash"]
            + ". Use Read on "
            + str(path.parent / "handoff" / (token + "-successor.json"))
            + " with offset=4 and limit=1 for successor.pid only. "
            + "Do not read the full record or command. "
            + "Missing or invalid PID blocks acknowledgement. "
            + "Then use Write BEFORE adoption to "
            + transfer["ack_path"]
            + " with token, snapshot_hash, successor_session_id="
            + session
            + ", successor_pid and jobs copied exactly from this registered list: "
            + json.dumps(transfer.get("jobs", []))
            + ". Do not add historical or completed jobs described in the snapshot. "
            + "Write the acknowledgement immediately. The background controller verifies ACK "
            + "and adopts you while you continue independent read-only preparation. "
            + "Do not idle, poll or end your turn to wait for ACK, adoption or a repeated ruling. "
            + "After ACK, use the bounded read-only rg ownership check on "
            + str(path)
            + ", the literal PILOT_RUN path. Do not use shell variables in this check. "
            + "Run rg --only-matching -e '^  \"dispatch_frozen\": [a-z]+' "
            + '-e \'^  "handoff": null\' -e \'^    "model_session_id": "[^"]+"\' '
            + shlex.quote(str(path))
            + ". Do not Read run.json line ranges or output command fields. "
            + "Only this read-only ownership check is permitted Bash before adoption. "
            + "Correct a malformed check without asking again; keep preparing read-only "
            + "if adoption is pending. Verify adopted ownership with bounded run.json fields "
            + "before dispatch or other writes. "
            + "Do not read full run.json, historical logs or the full procedural brief at startup. "
            + "Read the relevant procedural section when the next operation requires it."
        )
    selected = {
        key: env[key]
        for key in (
            "REPO",
            "BIN",
            "LEAD_BRIEF",
            "LEAD_AGENT",
            "LEAD_LOG",
            "BUILDER_LOGS",
            "FP_CAP_FRACTION",
            "ART",
            "PILOT_PYTHON",
            "ANTHROPIC_BASE_URL",
        )
        if key in env
    }
    selected.update(
        FP_CAP_FRACTION="0.753012048192771",
        PILOT_RUN=str(path),
        FP_LEAD="1",
        FP_LEAD_SPAWN=token,
        CLAUDE_CODE_FORCE_SESSION_PERSISTENCE="1",
        PILOT_LEAD_OVERLAP="1",
    )
    prompt += (
        "\nAt your context cap, use the exact standalone native --handoff-begin command. "
        "The overlap-enabled controller keeps your owned jobs with you as drain-only and "
        "adopts one dispatch successor for independent registered worktree scopes. "
        "Never transfer or restart running jobs. Register exact lead scopes through the "
        "orchestrator before the cap. A draining lead may only read results, write its "
        "handoff/lead-<session>-drain.md report and call --drain-ready <session>. "
        "That command returns persisted completion pointers for the orchestrator and "
        "current successor; deliver them through the approved literal WezTerm text and "
        "separate carriage-return path, preserving drafts and native receipts. "
        "The orchestrator verifies receipts, drain and final accounting through "
        "--drain-retire <session>. All six-slot, owner, approval, hold and gate controls remain."
        " Your gate stays attached to you and retains its native exit notification. "
        "On each owned builder/reviewer/gate completion, send the existing orchestrator "
        "alert with exact task/process/receipt identity immediately so accounting can "
        "finish; do not wait for drain-ready before alerting. You consume and report "
        "your existing results, the orchestrator owns final drain/accounting, and the "
        "dispatch successor consumes the accepted evidence pointer after release. "
        "An unresolved recorded gate blocks overlapping successor gates/benchmarks, "
        "but not independent registered builder scopes. Never poll for an exit."
        " Once your --drain-ready completion pointer is delivered, or your lead work is "
        "finished with no owned job running, close your own pane with the exact command "
        'wezterm cli kill-pane --pane-id "$WEZTERM_PANE". The orchestrator\'s '
        "--drain-retire accepts the exited lead."
    )
    try:
        from startup_packet import render

        prompt += "\n\n" + render(path.parent, "lead", token)
    except Exception as error:  # a missing packet must not block succession
        prompt += "\n\nStartup packet unavailable (" + repr(error) + "); check state directly."
    cmd = ["env", "-u", "FP_CONTEXT_CAP"] + [k + "=" + v for k, v in selected.items()]
    cmd += [
        "claude",
        "--disable-slash-commands",
        "--session-id",
        session,
        "--agent",
        "p1-pilot-lead",
        "--model",
        "claude-opus-5-5",
        "--effort",
        "medium",
        "--permission-mode",
        "auto",
        "--settings",
        str(Path(env["BIN"]) / "builder-settings.json"),
        "--add-dir",
        str(path.parent),
        "--",
        prompt,
    ]
    pane = subprocess.check_output(
        [
            "wezterm",
            "cli",
            "spawn",
            "--cwd",
            str(repo),
            "--",
            "/bin/zsh",
            "-c",
            "exec " + shlex.join(cmd),
        ],
        text=True,
    ).strip()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        matches = [
            row
            for row in processes().values()
            if ("--session-id " + session) in row["command"]
            and Path(row["command"].split()[0]).name == "claude"
        ]
        if len(matches) == 1:
            identity = {
                **matches[0],
                "model_session_id": session,
                "pane": pane,
                "handoff_token": token,
            }
            write(path.parent / "leads" / (str(identity["pid"]) + ".json"), identity)
            return identity
        time.sleep(0.2)
    raise ValueError(
        "WezTerm pane started but Claude identity was not observed; manual reconciliation required"
    )


def initial(path, env):
    state = read(path)
    if state.get("active_lead") or state.get("handoff"):
        raise ValueError("An active lead or handoff already exists")
    check_action(state, path, "launch:lead")
    budget(state)
    token = str(uuid.uuid4())
    transact(path, "spawner", "handoff-start", {"token": token, "initial": True})
    successor = None
    owned = {}
    try:
        successor = launch(path, env, token)
        capture(successor["pid"], owned)
        transact(path, "spawner", "handoff-commit", {"token": token, "successor": successor})
        return successor
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        if successor:
            signals, remaining = stop(successor["pid"], owned)
            write(
                path.parent / "handoff" / (token + "-initial-stop.json"),
                {"signals": signals, "remaining_processes": remaining},
            )
            if remaining:
                raise ValueError("Initial lead cleanup incomplete") from error
        transact(path, "spawner", "handoff-abort", {"token": token, "reason": str(error)})
        raise


def retire(path, transfer):
    predecessor = transfer["predecessor"]
    owned = {predecessor["pid"]: predecessor}
    signals, remaining = stop(predecessor["pid"], owned, descendants=False)
    write(
        path.parent / "handoff" / (transfer["token"] + "-retirement.json"),
        {"signals": signals, "remaining_processes": remaining, "identities": owned},
    )
    if remaining:
        raise ValueError("Predecessor survived retirement")


def controller(path, env, token):
    transfer = read(path)["handoff"]
    if transfer["token"] != token:
        raise ValueError("Wrong controller token")
    successor = None
    owned = {}
    try:
        concurrent = transfer.get("mode") == "concurrent"
        if not concurrent:
            drain_jobs(read(path), transfer["predecessor"])
        successor = launch(path, env, token)
        capture(successor["pid"], owned)
        update = {
            "token": token,
            "successor": successor,
            "successor_session_id": successor["model_session_id"],
        }
        transact(path, "spawner", "handoff-start", update)
        state = read(path)
        state_transfer = state["handoff"]
        write(path.parent / "handoff" / (token + "-successor.json"), update)
        deadline = time.monotonic() + float(env.get("PILOT_ACK_TIMEOUT", "900"))
        ack_path = Path(transfer["ack_path"])
        while time.monotonic() < deadline:
            check_action(read(path), path, "launch:lead")
            capture(successor["pid"], owned)
            if ack_path.exists():
                validate_ack(state_transfer, read(ack_path), processes())
                from startup_packet import commit

                # No packet was built if this fails; its entries are delivered again next time.
                with contextlib.suppress(ValueError, OSError):
                    commit(path.parent, "lead", token)
                break
            if not same_identity(successor, processes().get(successor["pid"])):
                raise ValueError("Successor exited before acknowledgement")
            time.sleep(1)
        else:
            raise ValueError("No valid acknowledgement within 15 minutes")
        if sha(Path(transfer["snapshot_path"])) != transfer["snapshot_hash"]:
            raise ValueError("Frozen snapshot changed")
        validate_pinned_state(path, transfer)
        check_action(read(path), path, "launch:lead")
        if not concurrent:
            drain_jobs(read(path), transfer["predecessor"])
            retire(path, state_transfer)
        transact(path, "spawner", "handoff-commit", {"token": token, "successor": successor})
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        if successor:
            signals, remaining = stop(successor["pid"], owned)
            write(
                path.parent / "handoff" / (token + "-successor-stop.json"),
                {"signals": signals, "remaining_processes": remaining},
            )
        transact(path, "spawner", "handoff-abort", {"token": token, "reason": str(error)})
        raise


def drain_jobs(state, lead, scoped=False, allow_exited=False):
    session = lead.get("model_session_id")
    allocations = [
        r
        for r in state.get("reservations", {}).values()
        if not scoped or r.get("parent_session_id") == session
    ]
    if any(r.get("active_run") for r in allocations):
        raise ValueError("Wait for builder/reviewer completion and final accounting before handoff")
    if scoped and any(
        r.get("usage_status") == "unknown"
        and state.get("reservations", {}).get(r.get("allocation_id"), {}).get("parent_session_id")
        == session
        for r in state.get("runs", {}).values()
    ):
        raise ValueError("Draining job usage is unknown")
    sessions = state.get("inventory", {}).get("sessions", [])
    if any(
        s.get("role") == "reviewer"
        and not s.get("complete")
        and (not scoped or s.get("parent_session_id") == session)
        for s in sessions
    ):
        raise ValueError("Drain in-process reviewer work before handoff")
    owned = {}
    table = capture(lead["pid"], owned)
    live = table.get(lead["pid"])
    exited = live is None or (
        live.get("status", "").startswith("Z")
        and all(live.get(k) == lead.get(k) for k in ("pid", "pgid", "os_session", "started_at"))
    )
    if not same_identity(lead, live) and not (allow_exited and exited):
        raise ValueError("Lead identity changed before handoff")
    for pid, row in owned.items():
        if pid == lead["pid"] or row.get("status", "").startswith("Z"):
            continue
        if any(
            Path(word.strip("'\"")).name in {"unit_gate.py", "unit_gate.sh", "red_check.py"}
            for word in row["command"].split()
        ):
            raise ValueError(f"Wait for running gate PID {pid} to finish before handoff")


def begin(path, concurrent=False):
    state = read(path)
    lead = originating_lead(state)
    scopes = state.get("lead_scopes", {}).get(lead.get("model_session_id"), [])
    if concurrent:
        if not scopes:
            raise ValueError("Concurrent handoff requires registered lead scopes")
        for reservation in state.get("reservations", {}).values():
            if reservation.get("active_run") and not reservation.get("parent_session_id"):
                raise ValueError("Running job has no registered lead owner")
        owned = {}
        capture(lead["pid"], owned)
        gates = [
            row
            for row in owned.values()
            if row["pid"] != lead["pid"]
            and any(
                Path(word.strip("'\"")).name in {"unit_gate.py", "unit_gate.sh", "red_check.py"}
                for word in row["command"].split()
            )
        ]
    else:
        drain_jobs(state, lead)
    token = str(uuid.uuid4())
    repo = Path(os.environ.get("REPO", Path.cwd()))
    work_state = Path(state.get("work_state_path") or repo / "prompts/phase-1/state.md")
    complete_state = work_state.read_bytes()
    pinned = path.parent / "handoff" / (token + "-state.md")
    pinned.parent.mkdir(parents=True, exist_ok=True)
    with pinned.open("xb") as output:
        output.write(complete_state)
    transact(path, "spawner", "freeze-lead", {"token": token})
    transact(
        path,
        "spawner",
        "handoff-start",
        {
            "token": token,
            "state_path": str(pinned),
            "state_sha256": sha(pinned),
            **(
                {"mode": "concurrent", "drain_scopes": scopes, "drain_gates": gates}
                if concurrent
                else {}
            ),
        },
    )
    print(token)


def start_handoff(path, env, token):
    from collections.abc import Callable

    register_controller: Callable[[Path, str, str, dict[str, object]], object] = transact
    controller_processes: Callable[[], dict[int, dict[str, object]]] = processes
    retire_controller: Callable[
        [int, dict[int, dict[str, object]], int, int, Callable[[], int | None]],
        tuple[list[dict[str, object]], list[int]],
    ] = stop
    state = read(path)
    lead = originating_lead(state)
    transfer = state.get("handoff")
    if not transfer or transfer["token"] != token or not state.get("dispatch_frozen"):
        raise ValueError("Handoff is not frozen with this token")
    snapshot = path.parent / "handoff" / (token + ".md")
    if not snapshot.is_file():
        raise ValueError("Frozen snapshot missing")
    work_state = Path(
        state.get("work_state_path") or Path(env["REPO"]) / "prompts/phase-1/state.md"
    )
    if "state_path" in transfer or "state_sha256" in transfer:
        validate_pinned_state(path, transfer)
        if Path(transfer["state_path"]).read_bytes() != work_state.read_bytes():
            raise ValueError("Pinned state differs from complete work state")
        if snapshot.stat().st_size > 6144:
            raise ValueError("Current handoff delta exceeds 6 KB; keep history in pinned state")
    elif work_state.read_bytes() not in snapshot.read_bytes():
        raise ValueError("Snapshot lacks the complete work state")
    concurrent = transfer.get("mode") == "concurrent"
    if not concurrent:
        drain_jobs(state, lead)
    elif any(
        str(gate["pid"]) not in snapshot.read_text() for gate in transfer.get("drain_gates", [])
    ):
        raise ValueError("Snapshot omits a drain-owned running gate")
    owned = {}
    capture(lead["pid"], owned)
    ancestors = set()
    table = processes()
    current = os.getpid()
    while current in table and current != lead["pid"]:
        ancestors.add(current)
        current = table[current]["ppid"]
    owned = {pid: row for pid, row in owned.items() if pid not in ancestors}
    jobs = []
    for allocation in state.get("reservations", {}).values():
        if concurrent and allocation.get("parent_session_id") != lead["model_session_id"]:
            continue
        run_id = allocation.get("active_run")
        if run_id:
            receipt_path = path.parent / (run_id + "-launch.json")
            receipt = read(receipt_path)
            job = {k: receipt[k] for k in ("pid", "pgid", "os_session", "started_at", "command")}
            job["receipt"] = str(receipt_path)
            if str(job["pid"]) not in snapshot.read_text():
                raise ValueError("Snapshot omits a running job")
            jobs.append(job)
    for blocker in state.get("blockers", []):
        if blocker.get("status") == "open" and blocker["id"] not in snapshot.read_text():
            raise ValueError("Snapshot omits an open blocker")
    ack_path = env["LEAD_LOG"] + ".ack-" + token + ".json"
    log = path.parent / "handoff" / (token + "-controller.log")
    if log.exists():
        controller_pid = transfer.get("controller_pid")
        if type(controller_pid) is not int or controller_pid <= 0 or controller_pid in table:
            raise ValueError(f"Handoff controller {controller_pid} may still be running: {log}")
        number = 1
        archive = log.with_name(token + f"-controller-{number}.log")
        while archive.exists():
            number += 1
            archive = log.with_name(token + f"-controller-{number}.log")
        log.rename(archive)
    child = None
    with log.open("x") as output:
        try:
            transact(
                path,
                "spawner",
                "handoff-start",
                {
                    "token": token,
                    "snapshot_path": str(snapshot),
                    "snapshot_hash": sha(snapshot),
                    "jobs": [] if concurrent else jobs,
                    "kept_jobs": jobs if concurrent else [],
                    "owned_processes": owned,
                    "ack_path": ack_path,
                },
            )
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--controller", token],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            register_controller(
                path,
                "spawner",
                "handoff-start",
                {
                    "token": token,
                    "controller_pid": child.pid,
                    "controller": controller_processes().get(child.pid),
                },
            )
        except (ValueError, OSError) as error:
            if child is not None:
                _, remaining = retire_controller(child.pid, {}, 30, 15, child.poll)
                if remaining:
                    raise ValueError(
                        f"Handoff controller {child.pid} cleanup incomplete: {log}"
                    ) from error
            number = 1
            archive = log.with_name(token + f"-controller-{number}.log")
            while archive.exists():
                number += 1
                archive = log.with_name(token + f"-controller-{number}.log")
            log.rename(archive)
            raise
    print("handoff controller", child.pid, "token", token)


def stop_active(path):
    from pilot_guard import claude_usage

    state = read(path)
    active = state.get("active_lead")
    if not active:
        raise ValueError("No active lead to retire")
    live = processes().get(active["pid"])
    exited = live is None or (
        live.get("status", "").startswith("Z")
        and all(live.get(k) == active.get(k) for k in ("pid", "pgid", "os_session", "started_at"))
    )
    if not exited and not same_identity(active, live):
        raise ValueError("Lead identity cannot be verified for retirement")
    owned = {active["pid"]: active}
    transact(path, "spawner", "retire-start", {"predecessor": active, "owned_processes": owned})
    signals, remaining = stop(active["pid"], owned, descendants=False) if not exited else ([], [])
    write(
        path.parent / "leads" / (str(active["pid"]) + "-retired.json"),
        {"signals": signals, "remaining_processes": remaining, "owned_processes": owned},
    )
    if remaining:
        raise ValueError("Lead retirement incomplete")
    session = active["model_session_id"]
    row = next(r for r in state["inventory"]["sessions"] if r["session_id"] == session)
    usage = claude_usage(Path(row["transcript_path"])) if row.get("transcript_path") else None
    transact(
        path,
        "orchestrator",
        "record-run",
        {
            "run_id": "lead-" + session,
            "session_id": session,
            "role": "lead",
            "complete": True,
            **(usage if usage is not None else {"usage_status": "unknown"}),
        },
    )
    transact(path, "spawner", "retire-commit", {"predecessor": active})


def verify_draining(path, drain):
    state = read(path)
    lead = drain["lead"]
    drain_jobs(
        state,
        lead,
        scoped=True,
        allow_exited=bool(drain.get("retirement") or drain.get("completion")),
    )
    table = processes()
    for job in drain.get("jobs", []):
        receipt = read(Path(job["receipt"]))
        remaining = receipt.get("remaining_processes")
        if (
            not isinstance(remaining, list)
            or any(type(pid) is not int or pid <= 0 or pid in table for pid in remaining)
            or receipt.get("child_exit") is None
        ):
            raise ValueError("Owned job final receipt is incomplete")
        run = state.get("runs", {}).get(Path(job["receipt"]).name.removesuffix("-launch.json"), {})
        if not run.get("complete") or run.get("usage_status") != "known":
            raise ValueError("Owned job final accounting is incomplete")
        for row in receipt.get("owned_processes", {}).values():
            if same_identity(row, table.get(row["pid"])) and not table[row["pid"]].get(
                "status", ""
            ).startswith("Z"):
                raise ValueError("Owned job process survives")
    if any(same_identity(gate, table.get(gate["pid"])) for gate in drain.get("gates", [])):
        raise ValueError("Owned gate survives")
    for receipt_path in drain.get("gate_stop_receipts", {}).values():
        receipt = read(Path(receipt_path))
        if receipt.get("remaining_processes") != []:
            raise ValueError("Stopped gate cleanup is incomplete")
        for row in receipt["owned_processes"].values():
            if same_identity(row, table.get(row["pid"])) and not table[row["pid"]].get(
                "status", ""
            ).startswith("Z"):
                raise ValueError("Stopped gate child survives")
    return table


def stop_draining_gate(path, session, pid):
    drain = read(path).get("draining_leads", {}).get(session)
    gate = next((g for g in (drain or {}).get("gates", []) if g["pid"] == pid), None)
    if not gate or not same_identity(gate, processes().get(pid)):
        raise ValueError("Exact registered gate identity is not live")
    owned = {pid: gate}
    capture(pid, owned)
    signals, remaining = stop(pid, owned)
    receipt = path.parent / "handoff" / ("lead-" + session + "-gate-" + str(pid) + "-stop.json")
    write(
        receipt,
        {
            "gate": gate,
            "owned_processes": owned,
            "signals": signals,
            "remaining_processes": remaining,
            "status": "FAIL",
            "reason": "owned gate stopped",
        },
    )
    transact(
        path,
        "spawner",
        "drain-gate-stop",
        {"session_id": session, "gate_pid": pid, "receipt": str(receipt)},
    )
    if remaining:
        raise ValueError("Owned gate cleanup incomplete")


def drain_ready(path, session):
    state = read(path)
    drain = state.get("draining_leads", {}).get(session)
    if not drain:
        raise ValueError("Lead is not registered as draining")
    originating_lead({**state, "active_lead": drain["lead"]})
    verify_draining(path, drain)
    if drain.get("completion"):
        return drain["completion"]
    report = path.parent / "handoff" / ("lead-" + session + "-drain.md")
    if not report.is_file():
        raise ValueError("Draining completion report is missing")
    orchestrator = next(
        (
            r
            for r in reversed(state.get("inventory", {}).get("threads", []))
            if r.get("role") == "orchestrator" and not r.get("complete")
        ),
        {},
    )
    successor = state.get("active_lead") or {}
    targets = []
    for identity, pane, transcript in [
        (
            orchestrator.get("thread_id"),
            orchestrator.get("pane_id"),
            orchestrator.get("transcript_path"),
        ),
        (
            successor.get("model_session_id"),
            successor.get("pane"),
            next(
                (
                    r.get("transcript_path")
                    for r in state.get("inventory", {}).get("sessions", [])
                    if r.get("session_id") == successor.get("model_session_id")
                ),
                None,
            ),
        ),
    ]:
        if not identity or pane is None or not str(pane).isdigit() or not transcript:
            raise ValueError("Completion recipient identity/transcript is incomplete")
        targets.append({"identity": identity, "pane": str(pane), "transcript_path": transcript})
    completion = {
        "report": str(report),
        "report_sha256": sha(report),
        "targets": targets,
        "message": "DRAIN_COMPLETE " + session + " " + str(uuid.uuid4()) + " " + str(report),
    }
    transact(path, "spawner", "drain-ready", {"session_id": session, "completion": completion})
    return completion


class TranscriptMessage(TypedDict, total=False):
    type: str
    role: str
    content: str | list[dict[str, str]]


class TranscriptAttachment(TypedDict, total=False):
    type: str
    commandMode: str
    prompt: str


class TranscriptRow(TypedDict, total=False):
    type: str
    sessionId: str
    message: TranscriptMessage
    attachment: TranscriptAttachment
    payload: TranscriptMessage
    timestamp: str
    uuid: str
    ordinal: int


class SessionRegistration(TypedDict, total=False):
    thread_id: str
    session_id: str
    pane_id: str | int
    role: str
    transcript_path: str
    started_at: str
    continued_session_id: str
    complete: bool


def notification_text(row: TranscriptRow) -> str | None:
    message: TranscriptMessage
    if row.get("type") == "user":
        message = row.get("message", {})
    elif (
        row.get("type") == "attachment"
        and row.get("attachment", {}).get("type") == "queued_command"
        and row["attachment"].get("commandMode") == "prompt"
    ):
        message = {"role": "user", "content": row["attachment"].get("prompt", "")}
    elif row.get("type") == "response_item" and row.get("payload", {}).get("type") == "message":
        message = row["payload"]
    else:
        return None
    if message.get("role") != "user":
        return None
    content = message.get("content", [])
    return content if isinstance(content, str) else "\n".join(c.get("text", "") for c in content)


def continued_rows(
    path: Path, history: list[dict[str, object]], identity: str
) -> list[TranscriptRow] | None:
    continued: list[TranscriptRow] = []
    seen_identity = False
    count = 0
    with path.open() as stream:
        for index, line in enumerate(stream):
            row: TranscriptRow = json.loads(line)
            if not isinstance(row, dict):
                return None
            session = row.get("sessionId")
            if session:
                if session != identity:
                    return None
                seen_identity = True
            if index < len(history):
                if {k: v for k, v in row.items() if k != "sessionId"} != history[index]:
                    return None
            else:
                continued.append(row)
            count += 1
    return continued if count >= len(history) and seen_identity else None


def registered_continuation_receipt(
    registrations: list[SessionRegistration],
    registered_target: SessionRegistration,
    target: dict[str, str],
    history: list[dict[str, object]],
    message: str,
) -> dict[str, str | int] | None:
    candidates = (
        (identity, r)
        for r in registrations
        if (identity := r.get("thread_id") or r.get("session_id"))
        and identity != target["identity"]
        and str(r.get("pane_id")) == str(registered_target.get("pane_id"))
        and r.get("role") == registered_target.get("role")
        and (
            registered_target.get("role") != "lead"
            or r.get("continued_session_id") == target["identity"]
        )
        and r.get("transcript_path")
    )
    for identity, registered in candidates:
        try:
            target_started = datetime.fromisoformat(registered_target["started_at"])
            started = datetime.fromisoformat(registered["started_at"])
            if not target_started < started:
                continue
            continued = continued_rows(Path(registered["transcript_path"]), history, identity)
        except (KeyError, OSError, TypeError, ValueError):
            continue
        if continued is None:
            continue
        found = None
        for row in continued:
            text = notification_text(row)
            try:
                valid_time = started < datetime.fromisoformat(row.get("timestamp", ""))
            except (TypeError, ValueError):
                continue
            receipt_id = row.get("uuid") or row.get("ordinal")
            if text is not None and valid_time and text == message and receipt_id is not None:
                found = {
                    "identity": target["identity"],
                    "session_id": identity,
                    "timestamp": row["timestamp"],
                    "receipt_id": receipt_id,
                }
        if found is not None:
            return found
    return None


def continuation_receipt(
    inventory: dict[str, list[SessionRegistration]],
    target: dict[str, str],
    history: list[dict[str, object]],
    message: str,
) -> dict[str, str | int] | None:
    registrations = inventory.get("threads", []) + inventory.get("sessions", [])
    registered_target = next(
        (
            r
            for r in registrations
            if (r.get("thread_id") or r.get("session_id")) == target["identity"]
            and str(r.get("pane_id")) == target["pane"]
            and r.get("transcript_path") == target["transcript_path"]
        ),
        None,
    )
    if registered_target is None:
        return None
    return registered_continuation_receipt(
        registrations, registered_target, target, history, message
    )


class LeadRecord(TypedDict, total=False):
    model_session_id: str
    session_id: str
    role: str
    pane: str | int
    pane_id: str | int
    pid: int
    started_at: str
    transcript_path: str


def lead_continuation_receipt(
    leads: list[LeadRecord],
    inventory: dict[str, list[SessionRegistration]],
    target: dict[str, str],
    history: list[dict[str, object]],
    message: str,
) -> dict[str, str | int] | None:
    matching = [
        lead
        for lead in leads
        if (lead.get("model_session_id") or lead.get("session_id")) == target["identity"]
        and lead.get("role", "lead") == "lead"
    ]
    pane = next(
        (
            lead.get("pane_id", lead.get("pane"))
            for lead in matching
            if "pane_id" in lead or "pane" in lead
        ),
        None,
    )
    transcript = next(
        (lead["transcript_path"] for lead in matching if lead.get("transcript_path")),
        target["transcript_path"],
    )
    if str(pane) != target["pane"] or transcript != target["transcript_path"]:
        return None
    registrations = inventory.get("threads", []) + inventory.get("sessions", [])
    registered_target = next(
        (
            r
            for r in registrations
            if r.get("session_id") == target["identity"]
            and r.get("role") == "lead"
            and r.get("transcript_path") == target["transcript_path"]
        ),
        None,
    )
    if registered_target is None or (
        "pane_id" in registered_target and str(registered_target["pane_id"]) != target["pane"]
    ):
        return None
    return registered_continuation_receipt(
        registrations, {**registered_target, "pane_id": target["pane"]}, target, history, message
    )


def retire_draining(path, session):
    state = read(path)
    drain = state.get("draining_leads", {}).get(session)
    if not drain:
        previous = next(
            (
                r
                for r in state.get("drain_history", [])
                if r.get("lead", {}).get("model_session_id") == session
            ),
            None,
        )
        if previous:
            return
        raise ValueError("Lead is not registered as draining")
    completion = drain.get("completion")
    lead = drain["lead"]
    if not completion:
        live = processes().get(lead["pid"])
        if not (
            live is None
            or (
                live.get("status", "").startswith("Z")
                and all(
                    live.get(k) == lead.get(k) for k in ("pid", "pgid", "os_session", "started_at")
                )
            )
        ):
            raise ValueError("Draining completion is not ready")
    allocations = {
        allocation
        for allocation, reservation in state.get("reservations", {}).items()
        if reservation.get("parent_session_id") == session
        or any(
            Path(reservation.get("worktree", "/")).resolve().is_relative_to(Path(scope).resolve())
            for scope in drain.get("scopes", [])
        )
    }
    if any(state["reservations"][allocation].get("active_run") for allocation in allocations):
        raise ValueError("Draining lead still owns active jobs")
    if any(
        (not run.get("complete") or run.get("usage_status") != "known")
        and (run.get("parent_session_id") == session or run.get("allocation_id") in allocations)
        for run in state.get("runs", {}).values()
    ):
        raise ValueError("Draining job final accounting is incomplete")
    table = verify_draining(
        path,
        drain
        if completion or drain.get("retirement")
        else {**drain, "retirement": {"exited_without_completion": True}},
    )
    live = table.get(lead["pid"])
    exited = live is None or (
        live.get("status", "").startswith("Z")
        and all(live.get(k) == lead.get(k) for k in ("pid", "pgid", "os_session", "started_at"))
    )
    if same_identity(lead, live) and not exited:
        raise ValueError("Draining lead survived retirement")
    if completion and sha(Path(completion["report"])) != completion["report_sha256"]:
        raise ValueError("Completion report changed")
    transcript = next(
        (
            r.get("transcript_path")
            for r in state.get("inventory", {}).get("sessions", [])
            if r.get("session_id") == session and r.get("role") == "lead"
        ),
        None,
    )
    from pilot_guard import claude_usage

    if not transcript or claude_usage(Path(transcript)) is None:
        raise ValueError("Draining lead final usage is unknown before retirement")

    def completion_receipt(target: dict[str, str]) -> Mapping[str, str | int | None] | None:
        found: Mapping[str, str | int | None] | None = None
        identities = set()
        history: list[dict[str, object]] = []
        with Path(target["transcript_path"]).open() as transcript_stream:
            for line in transcript_stream:
                row: TranscriptRow = json.loads(line)
                history.append({k: v for k, v in row.items() if k != "sessionId"})
                if row.get("type") == "session_meta":
                    identities.add(row.get("payload", {}).get("id"))
                if row.get("sessionId"):
                    identities.add(row["sessionId"])
                text = notification_text(row)
                if text is None:
                    continue
                if text == completion["message"]:
                    found = {
                        "identity": target["identity"],
                        "timestamp": row.get("timestamp"),
                        "receipt_id": row.get("uuid") or row.get("ordinal"),
                    }
        if identities != {target["identity"]}:
            raise ValueError("Native completion transcript identity differs")
        if found is None and history:
            found = continuation_receipt(
                state.get("inventory", {}), target, history, completion["message"]
            )
            if found is None:
                found = lead_continuation_receipt(
                    [state.get("runs", {}).get("lead-" + target["identity"], {})]
                    + [r.get("predecessor", {}) for r in state.get("retirement_history", [])]
                    + [state.get("active_lead") or {}]
                    + [d["lead"] for d in state.get("draining_leads", {}).values()],
                    state.get("inventory", {}),
                    target,
                    history,
                    completion["message"],
                )
        return found if found is not None and found["receipt_id"] is not None else None

    receipts = []
    replacements = None
    inventory: dict[str, list[SessionRegistration]] = state.get("inventory", {})
    for target in (completion or {}).get("targets", []):
        registered_lead = any(
            r.get("session_id") == target["identity"]
            and r.get("role") == "lead"
            and r.get("transcript_path") == target["transcript_path"]
            for r in inventory.get("sessions", []) + inventory.get("threads", [])
        )
        retired = next(
            (
                retirement
                for retirement in reversed(state.get("retirement_history", []))
                if retirement.get("predecessor", {}).get("model_session_id") == target["identity"]
                and str(retirement["predecessor"].get("pane")) == target["pane"]
            ),
            None,
        )
        if registered_lead:
            retired = None
        if retired is None:
            # A completed drain retires even a registered lead once its pid is dead.
            retired = next(
                (
                    retirement
                    for retirement in reversed(state.get("drain_history", []))
                    if retirement.get("lead", {}).get("model_session_id") == target["identity"]
                    and str(retirement["lead"].get("pane")) == target["pane"]
                    and "pid" in retirement["lead"]
                    and retirement["lead"]["pid"] not in table
                ),
                None,
            )
        if exited and retired and not Path(target["transcript_path"]).exists():
            found = None
        else:
            found = completion_receipt(target)
        if found is not None:
            receipts.append(found)
            continue
        if exited and retired:
            receipts.append(
                {
                    "identity": target["identity"],
                    "retired": True,
                    "retirement_receipt": retired.get("receipt"),
                    "timestamp": retired.get("completed_at") or retired.get("retired_at"),
                }
            )
            continue
        if replacements is None:
            orchestrator: SessionRegistration = next(
                (
                    r
                    for r in reversed(inventory.get("threads", []))
                    if r.get("role") == "orchestrator" and not r.get("complete")
                ),
                SessionRegistration(),
            )
            active = state.get("active_lead") or {}
            successor: SessionRegistration = next(
                (
                    r
                    for r in inventory.get("sessions", [])
                    if r.get("session_id") == active.get("model_session_id")
                    and r.get("role") == "lead"
                    and not r.get("complete")
                ),
                SessionRegistration(),
            )
            replacements = []
            for identity, pane, transcript_path in [
                (
                    orchestrator.get("thread_id"),
                    orchestrator.get("pane_id"),
                    orchestrator.get("transcript_path"),
                ),
                (
                    active.get("model_session_id"),
                    active.get("pane"),
                    successor.get("transcript_path"),
                ),
            ]:
                if not identity or pane is None or not str(pane).isdigit() or not transcript_path:
                    raise ValueError("Native completion notification receipt is missing")
                replacement = completion_receipt(
                    {"identity": identity, "pane": str(pane), "transcript_path": transcript_path}
                )
                if replacement is None:
                    raise ValueError("Native completion notification receipt is missing")
                replacements.append(replacement)
            receipts.extend(replacements)
        registered: SessionRegistration = next(
            (
                r
                for r in inventory.get("threads", []) + inventory.get("sessions", [])
                if (r.get("thread_id") or r.get("session_id")) == target["identity"]
                and ("pane_id" not in r or str(r["pane_id"]) == target["pane"])
                and r.get("transcript_path") == target["transcript_path"]
            ),
            SessionRegistration(),
        )
        if not registered.get("complete"):
            try:
                panes = json.loads(
                    subprocess.check_output(
                        ["wezterm", "cli", "list", "--format", "json"],
                        text=True,
                        stderr=subprocess.PIPE,
                        timeout=5,
                    )
                )
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                raise ValueError("Native completion notification receipt is missing") from error
            if any(str(pane["pane_id"]) == target["pane"] for pane in panes):
                raise ValueError("Native completion notification receipt is missing")
    lead = drain["lead"]
    table = processes()
    live = table.get(lead["pid"])
    exited = live is None or (
        live.get("status", "").startswith("Z")
        and all(live.get(k) == lead.get(k) for k in ("pid", "pgid", "os_session", "started_at"))
    )
    if not same_identity(lead, live) and not exited:
        raise ValueError("Draining lead identity changed before retirement")
    retirement = (
        drain.get("retirement")
        or {"lead": lead, "notification_receipts": [], "exited_without_completion": True}
        if not completion
        else {"lead": lead, "notification_receipts": receipts}
    )
    transact(
        path, "spawner", "drain-retire-start", {"session_id": session, "retirement": retirement}
    )
    signals, remaining = (
        stop(lead["pid"], {lead["pid"]: lead}, descendants=False) if not exited else ([], [])
    )
    if remaining:
        raise ValueError("Draining lead survived retirement")
    usage = claude_usage(Path(transcript))
    if usage is None:
        raise ValueError("Retired lead final usage is unknown")
    transact(
        path,
        "orchestrator",
        "record-run",
        {
            "run_id": "lead-" + session,
            "session_id": session,
            "role": "lead",
            "transcript_path": transcript,
            "complete": True,
            **usage,
        },
    )
    receipt = path.parent / "handoff" / ("lead-" + session + "-drain-retirement.json")
    write(
        receipt,
        {
            "lead": lead,
            "signals": signals,
            "remaining_processes": remaining,
            "notification_receipts": receipts,
            "exited_without_completion": not completion,
            "final_usage": usage,
        },
    )
    transact(path, "spawner", "drain-retire", {"session_id": session, "receipt": str(receipt)})


def main():
    path = Path(os.environ.get("PILOT_RUN", str(DEFAULT_RUN)))
    args = sys.argv[1:]
    if args == ["--stop"]:
        stop_active(path)
    elif args == ["--start"]:
        print(json.dumps(initial(path, os.environ)))
    elif args == ["--handoff-begin"]:
        begin(path, concurrent=os.environ.get("PILOT_LEAD_OVERLAP") == "1")
    elif len(args) == 2 and args[0] == "--drain-ready":
        print(json.dumps(drain_ready(path, args[1])))
    elif len(args) == 2 and args[0] == "--drain-retire":
        retire_draining(path, args[1])
    elif len(args) == 3 and args[0] == "--drain-stop-gate":
        stop_draining_gate(path, args[1], int(args[2]))
    elif len(args) == 2 and args[0] == "--handoff-start":
        start_handoff(path, os.environ, args[1])
    elif len(args) == 2 and args[0] == "--controller":
        controller(path, os.environ, args[1])
    else:
        raise ValueError("Use --start, --handoff-begin or --handoff-start TOKEN")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("REFUSED:", error, file=sys.stderr)
        print_refusal_note(error)
        raise SystemExit(2) from error
