import json
import os
import re
from pathlib import Path

from run_state import DEFAULT_RUN

DEFAULT_REPO = (
    "/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer"
)


def assignment(state, target, env):
    key = env["ASSIGNMENT_ID"]
    selected = state["assignments"][key]
    if target.startswith("resume:"):
        if selected.get("thread_id") != target.removeprefix("resume:"):
            raise ValueError("Resume thread does not match authorized assignment")
    elif target != selected["model"]:
        raise ValueError("Requested model is not authorized")
    for name, field in (("MODEL", "model"), ("EFFORT", "effort")):
        if name in env and env[name] != selected[field]:
            raise ValueError(name + " does not match authorized assignment")
    return selected


def command(target, selected, repo, reply, message, env):
    result = ["codex", "exec"]
    resume = target.startswith("resume:")
    if resume:
        result.append("resume")
    result += [
        "-m",
        selected["model"],
        "-c",
        'model_reasoning_effort="' + selected["effort"] + '"',
        "-c",
        'openai_base_url="http://127.0.0.1:8791/p/'
        + env.get("HEADROOM_PROJECT", "floorplan-accuracy")
        + '/v1"',
        "-c",
        "model_auto_compact_token_limit=1000000000",
        "-c",
        "skills.bundled.enabled=false",
        "--disable",
        "plugins",
        "-c",
        "skills.config=["
        + ",".join(
            '{path="' + str(Path.home() / ".agents/skills" / name / "SKILL.md") + '",enabled=false}'
            for name in ("find-skills", "typesafe-ai")
        )
        + "]",
    ]
    sandbox = env.get("SANDBOX", "workspace-write")
    if resume:
        result += [
            "-c",
            'sandbox_mode="' + sandbox + '"',
            "-c",
            'sandbox_workspace_write.writable_roots=["/tmp/factory-fp-0925/swarm"]',
        ]
        if sandbox == "workspace-write":
            result += [
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ]
    else:
        mode = ["--approve-for-me"] if sandbox == "workspace-write" else ["-s", sandbox]
        result += [*mode, "-C", str(repo), "--add-dir", "/tmp/factory-fp-0925/swarm"]
    if env.get("OUTPUT_SCHEMA"):
        result += ["--output-schema", env["OUTPUT_SCHEMA"]]
    result += ["--json", "-o", str(reply)]
    if resume:
        result.append(target.removeprefix("resume:"))
    result.append(message)
    return result


def events(path: Path | None, offset: int = 0) -> list[dict[str, object]]:
    if not path or not path.exists():
        return []
    with path.open("rb") as stream:
        stream.seek(offset)
        lines = stream.read().splitlines()
    result: list[dict[str, object]] = []
    for line in lines:
        try:
            row: object = json.loads(line)
            if isinstance(row, dict) and all(isinstance(key, str) for key in row):
                result.append(row)
        except (ValueError, UnicodeError):
            continue
    return result


def drift(path, offset, selected):
    result = set()
    for row in events(path, offset):
        if row.get("type") != "turn_context":
            continue
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if payload.get("model") != selected["model"]:
            result.add("MODEL_DRIFT")
        effort = payload.get("effort", payload.get("reasoning_effort"))
        if effort != selected["effort"]:
            result.add("EFFORT_DRIFT")
    return sorted(result)


def usage(path):
    for row in reversed(events(path)):
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if row.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            counters = info.get("total_token_usage")
            if isinstance(counters, dict) and counters:
                return counters
    return None


def find_rollout(home, thread):
    paths = list((home / "sessions").rglob("*" + thread + ".jsonl"))
    if len(paths) > 1:
        raise ValueError("Multiple rollouts for thread")
    return paths[0] if paths else None


def persist(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)


def builder_rule(path: Path) -> str:
    diagnostic = "Missing RULING573j paragraph in " + str(path)
    try:
        lines = iter(path.read_text().splitlines())
    except FileNotFoundError as error:
        raise ValueError(diagnostic) from error
    for line in lines:
        if line.startswith("User rule (October 13, RULING573j"):
            paragraph = [line]
            for continuation in lines:
                if not continuation.strip():
                    break
                paragraph.append(continuation)
            return "\n".join(paragraph)
    raise ValueError(diagnostic)


def supervise(directory, tag, target, message_file, env):
    import subprocess
    import time

    from owned_processes import capture, stop, survivors
    from run_state import transact, utc

    run_path = Path(env.get("PILOT_RUN", str(DEFAULT_RUN)))
    state = json.loads(run_path.read_text())
    selected = assignment(state, target, env)
    repo = Path(env.get("REPO", DEFAULT_REPO)).resolve()
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError("Artifact directory does not exist")
    event_path = directory / (tag + "-events.jsonl")
    reply = directory / (tag + "-reply.txt")
    receipt_path = directory / (tag + "-launch.json")
    if any(p.exists() for p in (event_path, reply, receipt_path)):
        raise ValueError("Launch tag already exists")
    allocation = env["ALLOCATION_ID"]
    run_id = tag
    home = Path(env.get("CODEX_HOME", str(Path.home() / ".codex")))
    thread = target.removeprefix("resume:") if target.startswith("resume:") else None
    rollout = find_rollout(home, thread) if thread else None
    starting = usage(rollout) if rollout else None
    if thread and starting is None:
        raise ValueError("Resume starting usage is unknown")
    offset = rollout.stat().st_size if rollout else 0
    message = message_file.read_text()
    if env.get("ROLE", "builder") == "builder":
        builder_brief = repo / "prompts/phase-1/builder-brief.md"
        rule = builder_rule(builder_brief)
        if rule not in message:
            message += "\n\n" + rule
    transact(
        run_path,
        "launcher",
        "admit",
        {
            "allocation_id": allocation,
            "run_id": run_id,
            "role": env.get("ROLE", "builder"),
            "allowance": int(env["ALLOWANCE"]),
        },
    )
    cmd = command(target, selected, repo, reply, message, env)
    receipt = {
        "run_id": run_id,
        "allocation_id": allocation,
        "feature": env["FEATURE"],
        "repo": str(repo),
        "assignment": selected,
        "started_at": utc(),
        "rollout_offset": offset,
        "starting_usage": starting,
        "thread_id": thread,
        "command": cmd,
        "remaining_processes": [],
    }
    persist(receipt_path, receipt)
    owned = {}
    child = None
    status = "FAIL"
    try:
        with event_path.open("x") as output:
            child = subprocess.Popen(
                cmd,
                cwd=repo,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        capture(child.pid, owned)
        receipt.update(
            owned.get(
                child.pid,
                {
                    "pid": child.pid,
                    "pgid": child.pid,
                    "os_session": None,
                    "started_at": receipt["started_at"],
                },
            )
        )
        receipt["argv"] = cmd
        receipt["owned_processes"] = owned
        persist(receipt_path, receipt)
        began = time.monotonic()
        last_change = began
        signature = None
        idle_alerted = False
        stop_reason = None
        while True:
            table = capture(child.pid, owned)
            child_exit = child.poll()
            if child_exit is not None:
                table.pop(child.pid, None)
            for row in events(event_path):
                if row.get("type") == "thread.started":
                    observed = row["thread_id"]
                    if not isinstance(observed, str):
                        continue
                    if thread and observed != thread:
                        raise ValueError("Observed thread differs from resume target")
                    if not thread:
                        thread = observed
                        transact(
                            run_path,
                            "launcher",
                            "assign",
                            {
                                "assignment_id": env["ASSIGNMENT_ID"],
                                "thread_id": thread,
                                "reason": "Observed thread.started event",
                            },
                        )
                    receipt["thread_id"] = thread
            if thread and rollout is None:
                rollout = find_rollout(home, thread)
            problems = drift(rollout, offset, selected) if rollout else []
            if problems:
                stop_reason = problems[0]
            current = usage(rollout) if rollout else None
            if current:
                before = starting or {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                }
                fresh = (
                    current["input_tokens"]
                    - current.get("cached_input_tokens", 0)
                    + current["output_tokens"]
                ) - (
                    before["input_tokens"]
                    - before.get("cached_input_tokens", 0)
                    + before["output_tokens"]
                )
                cached = current.get("cached_input_tokens", 0) - before.get(
                    "cached_input_tokens", 0
                )
                receipt["usage_delta"] = {"new_tokens": fresh, "cache_reads": cached}
                receipt["final_cumulative_usage"] = current
                transact(
                    run_path,
                    "launcher",
                    "record-run",
                    {
                        "allocation_id": allocation,
                        "run_id": run_id,
                        "thread_id": thread,
                        "new_tokens": fresh,
                        "cache_reads": cached,
                        "transcript_path": str(rollout),
                        "complete": False,
                    },
                )
                updated = json.loads(run_path.read_text())
                reservation = updated["reservations"][allocation]
                if reservation["observed"] >= reservation["allowance"]:
                    stop_reason = "BUDGET"
                for row in reversed(events(rollout, offset)):
                    payload = row.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    last = info.get("last_token_usage")
                    if not isinstance(last, dict):
                        continue
                    input_tokens = last.get("input_tokens", 0)
                    if isinstance(input_tokens, int) and input_tokens > int(
                        env.get("TRIGGER", "230000")
                    ):
                        stop_reason = "CONTEXT"
                        break
            now = time.monotonic()
            if not thread and now - began >= float(env.get("START_S", "90")):
                stop_reason = "STALL_START"
            sig = (event_path.stat().st_size, rollout.stat().st_size if rollout else 0)
            if sig != signature:
                signature, last_change, idle_alerted = sig, now, False
            elif now - last_change >= float(env.get("IDLE_S", "600")) and not idle_alerted:
                print("STALL_IDLE", tag, "supervision continues", flush=True)
                idle_alerted = True
            receipt.update(owned_processes=owned, rollout_path=str(rollout) if rollout else None)
            persist(receipt_path, receipt)
            if stop_reason:
                print(stop_reason, tag, flush=True)
                break
            if child_exit is not None:
                lingering = [pid for pid in survivors(owned, table) if pid != child.pid]
                if lingering:
                    stop_reason = "SURVIVING_DESCENDANT"
                    break
                for row in reversed(events(rollout, offset)):
                    payload = row.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    if row.get("type") == "event_msg" and payload.get("type") == "task_complete":
                        error = payload.get("error")
                        if isinstance(error, dict):
                            reason = error.get("codex_error_info")
                            if isinstance(reason, str):
                                stop_reason = reason
                        break
                status = (
                    "REPLY"
                    if (
                        not stop_reason
                        and child_exit == 0
                        and reply.exists()
                        and reply.read_text().strip()
                    )
                    else "FAIL"
                )
                if stop_reason == "server_overloaded":
                    status = "CAPACITY"
                break
            time.sleep(float(env.get("PILOT_TICK", "1")))
        receipt["stop_reason"] = stop_reason
    finally:
        if child:
            try:
                signals, remaining = stop(
                    child.pid,
                    owned,
                    float(env.get("PILOT_STOP_INT", "30")),
                    float(env.get("PILOT_STOP_TERM", "15")),
                    child.poll,
                )
            except ValueError as error:
                signals, remaining = [], sorted(owned)
                receipt["supervision_error"] = str(error)
            receipt.update(signals=signals, remaining_processes=remaining, child_exit=child.poll())
            if remaining:
                status = "FAIL"
                receipt["ownership_blocked"] = True
        if (
            "usage_delta" in receipt
            and receipt.get("child_exit") == 0
            and not receipt.get("stop_reason")
        ):
            receipt["usage_status"] = "known"
            transact(
                run_path,
                "launcher",
                "record-run",
                {
                    "allocation_id": allocation,
                    "run_id": run_id,
                    "thread_id": thread,
                    **receipt["usage_delta"],
                    "complete": True,
                    "transcript_path": str(rollout),
                },
            )
        else:
            receipt["usage_status"] = "unknown"
            transact(
                run_path,
                "launcher",
                "record-run",
                {
                    "allocation_id": allocation,
                    "run_id": run_id,
                    "thread_id": thread,
                    "usage_status": "unknown",
                    "complete": True,
                },
            )
        if not receipt.get("remaining_processes"):
            keep = status == "REPLY" and env.get("KEEP_ALLOCATION") == "1"
            if not keep:
                transact(
                    run_path, "launcher", "release", {"allocation_id": allocation, "run_id": run_id}
                )
        receipt.update(status=status, ended_at=utc())
        persist(receipt_path, receipt)
    print(status, tag, "exit=" + str(receipt.get("child_exit")), flush=True)
    return 0 if status == "REPLY" else 1


def main():
    import sys

    try:
        # Settings may come as leading KEY=VALUE arguments, so a launch command starts
        # with the script path and matches its permission rule.
        args, env = sys.argv[1:], dict(os.environ)
        while args and re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", args[0]):
            key, value = args.pop(0).split("=", 1)
            env[key] = value
        if len(args) != 4:
            raise ValueError(
                "usage: codex-agent.sh [KEY=VALUE ...] <dir> <tag> <model|resume:THREAD> "
                "<message-file>"
            )
        return supervise(Path(args[0]), args[1], args[2], Path(args[3]), env)
    except (ValueError, KeyError, OSError) as error:
        print("FAIL", str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
