import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

from owned_processes import capture, stop
from run_state import (
    DEFAULT_RUN,
    budget,
    check_action,
    originating_lead,
    processes,
    same_identity,
    transact,
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temporary, path)


def validate_ack(transfer, ack, table):
    if any(ack.get(k) != transfer[k] for k in ("token", "snapshot_hash")):
        raise ValueError("Acknowledgement token or snapshot hash differs")
    jobs = transfer.get("jobs", [])
    adopted = ack.get("jobs", [])
    if len(adopted) != len(jobs) or {j["pid"] for j in jobs} != {
        j["pid"] for j in adopted
    }:
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
    prompt = (
        brief.read_text()
        + "\nPilot handoff token: "
        + token
        + ". Read run.json before acting."
    )
    selected = {
        key: env[key]
        for key in (
            "SWARM_HOME",
            "SWARM_STATE",
            "ORCHESTRATOR_PANE",
            "SWARM_PROJECT_ADAPTER",
            "CODEX_BASE_URL",
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
        PILOT_RUN=str(path),
        FP_LEAD="1",
        FP_LEAD_SPAWN=token,
        CLAUDE_CODE_FORCE_SESSION_PERSISTENCE="1",
    )
    cmd = ["env", "-u", "FP_CONTEXT_CAP"] + [k + "=" + v for k, v in selected.items()]
    cmd += [
        "claude",
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
        transact(
            path, "spawner", "handoff-commit", {"token": token, "successor": successor}
        )
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
        transact(
            path, "spawner", "handoff-abort", {"token": token, "reason": str(error)}
        )
        raise


def retire(path, transfer):
    predecessor = transfer["predecessor"]
    owned = {int(k): v for k, v in transfer["owned_processes"].items()}
    signals, remaining = stop(predecessor["pid"], owned)
    write(
        path.parent / "handoff" / (transfer["token"] + "-retirement.json"),
        {"signals": signals, "remaining_processes": remaining, "identities": owned},
    )
    if remaining:
        raise ValueError("Predecessor descendants survived retirement")


def controller(path, env, token):
    transfer = read(path)["handoff"]
    if transfer["token"] != token:
        raise ValueError("Wrong controller token")
    successor = None
    owned = {}
    try:
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
                validate_ack(transfer, read(ack_path), processes())
                break
            if not same_identity(successor, processes().get(successor["pid"])):
                raise ValueError("Successor exited before acknowledgement")
            time.sleep(1)
        else:
            raise ValueError("No valid acknowledgement within 15 minutes")
        if sha(Path(transfer["snapshot_path"])) != transfer["snapshot_hash"]:
            raise ValueError("Frozen snapshot changed")
        check_action(read(path), path, "launch:lead")
        retire(path, state_transfer)
        transact(
            path, "spawner", "handoff-commit", {"token": token, "successor": successor}
        )
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        if successor:
            signals, remaining = stop(successor["pid"], owned)
            write(
                path.parent / "handoff" / (token + "-successor-stop.json"),
                {"signals": signals, "remaining_processes": remaining},
            )
        transact(
            path, "spawner", "handoff-abort", {"token": token, "reason": str(error)}
        )
        raise


def begin(path):
    state = read(path)
    originating_lead(state)
    sessions = state.get("inventory", {}).get("sessions", [])
    if any(s.get("role") == "reviewer" and not s.get("complete") for s in sessions):
        raise ValueError("Drain in-process reviewer work before handoff")
    token = str(uuid.uuid4())
    transact(path, "spawner", "freeze-lead", {"token": token})
    print(token)


def start_handoff(path, env, token):
    state = read(path)
    lead = originating_lead(state)
    transfer = state.get("handoff")
    if not transfer or transfer["token"] != token or not state.get("dispatch_frozen"):
        raise ValueError("Handoff is not frozen with this token")
    snapshot = path.parent / "handoff" / (token + ".md")
    if not snapshot.is_file():
        raise ValueError("Frozen snapshot missing")
    work_state = Path(
        env.get("SWARM_STATE", str(Path(env["REPO"]) / ".swarm/state.md"))
    )
    if work_state.read_text() not in snapshot.read_text():
        raise ValueError("Snapshot lacks the complete work state")
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
        run_id = allocation.get("active_run")
        if run_id:
            receipt_path = path.parent / (run_id + "-launch.json")
            receipt = read(receipt_path)
            job = {
                k: receipt[k]
                for k in ("pid", "pgid", "os_session", "started_at", "command")
            }
            job["receipt"] = str(receipt_path)
            if str(job["pid"]) not in snapshot.read_text():
                raise ValueError("Snapshot omits a running job")
            jobs.append(job)
    for blocker in state.get("blockers", []):
        if (
            blocker.get("status") == "open"
            and blocker["id"] not in snapshot.read_text()
        ):
            raise ValueError("Snapshot omits an open blocker")
    ack_path = env["LEAD_LOG"] + ".ack-" + token + ".json"
    transact(
        path,
        "spawner",
        "handoff-start",
        {
            "token": token,
            "snapshot_path": str(snapshot),
            "snapshot_hash": sha(snapshot),
            "jobs": jobs,
            "owned_processes": owned,
            "ack_path": ack_path,
        },
    )
    log = path.parent / "handoff" / (token + "-controller.log")
    with log.open("x") as output:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--controller", token],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print("handoff controller", child.pid, "token", token)


def stop_active(path):
    from pilot_guard import claude_usage

    state = read(path)
    active = state.get("active_lead")
    if not active:
        raise ValueError("No active lead to retire")
    if not same_identity(active, processes().get(active["pid"])):
        raise ValueError("Lead identity cannot be verified for retirement")
    owned = {}
    capture(active["pid"], owned)
    transact(
        path,
        "spawner",
        "retire-start",
        {"predecessor": active, "owned_processes": owned},
    )
    signals, remaining = stop(active["pid"], owned)
    write(
        path.parent / "leads" / (str(active["pid"]) + "-retired.json"),
        {
            "signals": signals,
            "remaining_processes": remaining,
            "owned_processes": owned,
        },
    )
    if remaining:
        raise ValueError("Lead retirement incomplete")
    session = active["model_session_id"]
    row = next(r for r in state["inventory"]["sessions"] if r["session_id"] == session)
    usage = (
        claude_usage(Path(row["transcript_path"]))
        if row.get("transcript_path")
        else None
    )
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


def main():
    path = Path(os.environ.get("PILOT_RUN", str(DEFAULT_RUN)))
    args = sys.argv[1:]
    if args == ["--stop"]:
        stop_active(path)
    elif args == ["--start"]:
        print(json.dumps(initial(path, os.environ)))
    elif args == ["--handoff-begin"]:
        begin(path)
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
        raise SystemExit(2) from error
