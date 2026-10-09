import json
import os
import shlex
import sys
from pathlib import Path

from run_state import DEFAULT_RUN, check_action, originating_lead, transact
from swarm_config import BIN

READ_TOOLS = {"Read", "Grep", "Glob"}
READ_COMMANDS = {"cat", "head", "ls", "pwd", "rg", "grep", "wc", "shasum"}


def orchestrator_alert(event, state):
    if event.get("tool_name") != "Bash" or event.get("agent_id"):
        return False
    transfer = state.get("handoff") or {}
    leads = [state.get("active_lead") or {}, transfer.get("predecessor") or {}]
    sessions = {lead.get("model_session_id") for lead in leads}
    sessions.add(transfer.get("successor_session_id"))
    sessions.update(
        row.get("session_id")
        for row in state.get("inventory", {}).get("sessions", [])
        if row.get("role") == "lead"
    )
    if not event.get("session_id") or event["session_id"] not in sessions:
        return False
    command = event.get("tool_input", {}).get("command", "").replace("$'\\r'", "'\r'")
    if "$" in command or "`" in command:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        groups = [[]]
        for word in lexer:
            if word == "&&":
                groups.append([])
            else:
                groups[-1].append(word)
    except ValueError:
        return False
    sent = False
    for words in groups:
        if words == ["echo", "sent"] and sent:
            continue
        if words[:5] != [
            "wezterm",
            "cli",
            "send-text",
            "--pane-id",
            os.environ.get("ORCHESTRATOR_PANE", ""),
        ]:
            return False
        payload = words[5:]
        if payload[:1] == ["--no-paste"]:
            payload = payload[1:]
        if len(payload) != 1 or not payload[0]:
            return False
        sent = True
    return sent


def readonly(event):
    if event.get("tool_name") in READ_TOOLS:
        return True
    if event.get("tool_name") != "Bash":
        return False
    command = event.get("tool_input", {}).get("command", "")
    if any(token in command for token in ("$", "`", ">", "<", ";", "&", "\n", "|")):
        return False
    words = shlex.split(command)
    if not words:
        return False
    if words[0] in READ_COMMANDS:
        return True
    return (
        len(words) > 1
        and words[0] == "git"
        and words[1] in {"status", "diff", "show", "log", "rev-parse"}
    )


def check(event, state, path, used, cap):
    if readonly(event):
        return True
    tool = event.get("tool_name")
    args = event.get("tool_input", {})
    check_action(state, path, "dispatch")
    session = event.get("session_id")
    if event.get("agent_id"):
        reviewer = next(
            (
                row
                for row in state.get("inventory", {}).get("sessions", [])
                if row.get("role") == "reviewer"
                and row.get("agent_id") == event["agent_id"]
                and row.get("parent_session_id") == session
                and session == (state.get("active_lead") or {}).get("model_session_id")
            ),
            None,
        )
        if reviewer is None:
            raise ValueError("Subagent identity is not registered for this lead")
        if tool == "SubagentHandback":
            return True
        if reviewer.get("complete"):
            raise ValueError("Completed reviewer may only hand back its result")
        output = str(Path(args.get("file_path", ".")).resolve())
        if tool in {"Write", "Edit"} and output in reviewer.get("output_paths", []):
            return True
        if tool == "Bash":
            command = args.get("command", "")
            words = shlex.split(command)
            if (
                words
                and not any(
                    c in command for c in (";", "|", "&", ">", "<", "`", "$", "\n")
                )
                and (
                    words[0] in {"pytest", "npm", "shasum"}
                    or words[:2] in [["git", "diff"], ["git", "show"]]
                )
            ):
                return True
        raise ValueError(
            "Reviewer may only check the packet and write registered outputs"
        )
    transfer = state.get("handoff") or {}
    file = str(Path(args.get("file_path", ".")).resolve())
    if tool in {"Write", "Edit"}:
        for row in state.get("inventory", {}).get("sessions", []):
            if row.get("session_id") == session and row.get("role") == "reviewer":
                if row.get("parent_session_id") == (state.get("active_lead") or {}).get(
                    "model_session_id"
                ) and file in row.get("output_paths", []):
                    return True
                raise ValueError("Reviewer may write only its registered output files")
        if (
            session == transfer.get("successor_session_id")
            and transfer.get("ack_path")
            and file == str(Path(transfer["ack_path"]).resolve())
        ):
            return True
    lead = originating_lead(state)
    if session != lead.get("model_session_id"):
        raise ValueError("Tool event session is not the active lead")
    words = shlex.split(args.get("command", "")) if tool == "Bash" else []
    launcher = str(BIN / "interior-lead.sh")
    if words == [launcher, "--handoff-begin"]:
        return True
    if transfer.get("token") and words == [
        launcher,
        "--handoff-start",
        transfer["token"],
    ]:
        return True
    snapshot = path.parent / "handoff" / (str(transfer.get("token")) + ".md")
    if (
        tool in {"Write", "Edit"}
        and transfer.get("token")
        and file == str(snapshot.resolve())
        and state.get("dispatch_frozen")
        and used <= cap + cap // 10
    ):
        return True
    if state.get("dispatch_frozen"):
        raise ValueError("Predecessor dispatch is frozen")
    if used > cap:
        raise ValueError("Context cap permits handoff only")
    action = "launch:reviewer" if tool == "Agent" else "dispatch"
    command = args.get("command", "")
    for script, target in (
        ("builder-start.sh", "launch:builder"),
        ("codex-agent.sh", "launch:builder"),
        ("prepare_candidate.sh", "candidate"),
        ("merge_unit.sh", "merge"),
    ):
        if script in command:
            action = target
    check_action(state, path, action, require_lead=True)
    return True


def claude_usage(transcript):
    messages = {}
    if not transcript.is_file():
        return None
    for line in transcript.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        message = row.get("message", {})
        if message.get("id") and message.get("usage"):
            old = messages.setdefault(message["id"], {})
            for key, value in message["usage"].items():
                if type(value) is int:
                    old[key] = max(old.get(key, 0), value)
    if not messages:
        return None
    return {
        "new_tokens": sum(
            u.get("input_tokens", 0)
            + u.get("cache_creation_input_tokens", 0)
            + u.get("output_tokens", 0)
            for u in messages.values()
        ),
        "cache_reads": sum(
            u.get("cache_read_input_tokens", 0) for u in messages.values()
        ),
    }


def record_lead(event, path):
    if event.get("agent_id"):
        return
    state = json.loads(path.read_text())
    session = event.get("session_id")
    if session != (state.get("active_lead") or {}).get("model_session_id"):
        return
    originating_lead(state)
    transcript = Path(event["transcript_path"])
    usage = claude_usage(transcript)
    if usage is not None:
        transact(
            path,
            "orchestrator",
            "record-run",
            {
                "run_id": "lead-" + session,
                "session_id": session,
                "role": "lead",
                "transcript_path": str(transcript),
                "complete": False,
                **usage,
            },
        )


def reviewer_event(event, path):
    if event.get("agent_type") != "p1-pilot-reviewer":
        return
    state = json.loads(path.read_text())
    originating_lead(state)
    parent = event["session_id"]
    if parent != state["active_lead"]["model_session_id"]:
        raise ValueError("Reviewer parent is not the active lead")
    session = parent + "/" + event["agent_id"]
    known = [
        (key, value)
        for key, value in state.get("runs", {}).items()
        if value.get("session_id") == session and value.get("allocation_id")
    ]
    if event["hook_event_name"] == "SubagentStop" and len(known) == 1:
        run_id, run = known[0]
        allocation = run["allocation_id"]
        reservation = state["reservations"][allocation]
        if (
            reservation["role"] != "reviewer"
            or reservation.get("parent_session_id") != parent
        ):
            raise ValueError("Reviewer allocation identity differs")
    else:
        rows = [
            (key, value)
            for key, value in state.get("reservations", {}).items()
            if value["role"] == "reviewer"
            and value.get("parent_session_id") == parent
            and value.get("active_run")
            and not value.get("released")
        ]
        if len(rows) != 1 or not rows[0][1].get("output_paths"):
            raise ValueError(
                "Reviewer needs one reserved allocation with exact output paths"
            )
        allocation, reservation = rows[0]
        run_id = reservation["active_run"]
    data = {
        "allocation_id": allocation,
        "run_id": run_id,
        "session_id": session,
        "agent_id": event["agent_id"],
        "complete": event["hook_event_name"] == "SubagentStop",
    }
    if data["complete"]:
        transcript = Path(event["agent_transcript_path"])
    else:
        transcript = (
            Path(event["transcript_path"]).with_suffix("")
            / "subagents"
            / ("agent-" + event["agent_id"] + ".jsonl")
        )
    data["transcript_path"] = str(transcript)
    usage = claude_usage(transcript)
    data.update(usage if usage is not None else {"usage_status": "unknown"})
    transact(path, "launcher", "record-run", data)
    if data["complete"]:
        transact(
            path,
            "launcher",
            "release",
            {"allocation_id": allocation, "run_id": data["run_id"]},
        )
    evidence = path.parent / (
        "reviewer-hook-" + event["agent_id"] + "-" + event["hook_event_name"] + ".json"
    )
    evidence.write_text(
        json.dumps(
            {
                k: event.get(k)
                for k in (
                    "session_id",
                    "agent_id",
                    "agent_type",
                    "hook_event_name",
                    "transcript_path",
                    "agent_transcript_path",
                )
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    try:
        reviewer_event(
            json.load(sys.stdin), Path(os.environ.get("PILOT_RUN", str(DEFAULT_RUN)))
        )
    except (ValueError, OSError, KeyError) as error:
        print("Pilot reviewer hook: " + str(error), file=sys.stderr)
        raise SystemExit(2) from error
