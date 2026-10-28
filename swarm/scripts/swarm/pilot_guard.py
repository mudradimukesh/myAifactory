import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

from run_state import (
    DEFAULT_RUN,
    apply,
    check_action,
    originating_lead,
    print_refusal_note,
    processes,
    same_identity,
    transact,
)


class ReviewerSession(TypedDict, total=False):
    session_id: str
    agent_id: str
    parent_session_id: str
    role: str
    transcript_path: str
    complete: bool
    pending: bool
    output_paths: list[str]


class ReviewerReservation(TypedDict, total=False):
    role: str
    parent_session_id: str
    active_run: str | None
    released: bool
    output_paths: list[str]


class ReviewerRun(TypedDict, total=False):
    session_id: str
    agent_id: str
    allocation_id: str
    role: str
    complete: bool


class ReviewerInventory(TypedDict, total=False):
    sessions: list[ReviewerSession]


class ReviewerDrain(TypedDict, total=False):
    lead: Mapping[str, object]


class ReviewerState(TypedDict, total=False):
    active_lead: Mapping[str, object]
    draining_leads: dict[str, ReviewerDrain]
    inventory: ReviewerInventory
    reservations: dict[str, ReviewerReservation]
    runs: dict[str, ReviewerRun]


READ_TOOLS = {"Read", "Grep", "Glob"}
LAUNCH_ENV = {
    "REPO",
    "PILOT_RUN",
    "JEV_LEDGER",
    "FEATURE",
    "ROLE",
    "ASSIGNMENT_ID",
    "ALLOCATION_ID",
    "ALLOWANCE",
    "KEEP_ALLOCATION",
}
READ_COMMANDS = {
    "cat",
    "head",
    "jq",
    "ls",
    "pwd",
    "rg",
    "grep",
    "wc",
    "shasum",
    "tail",
    "ps",
    "stat",
    "cut",
}
READ_GIT = {"status", "diff", "show", "log", "rev-parse", "ls-files", "merge-base"}
DRAIN_GIT_READ = {
    "status",
    "log",
    "diff",
    "show",
    "rev-parse",
    "merge-base",
    "ls-files",
    "ls-tree",
    "cat-file",
    "for-each-ref",
}
DRAIN_GIT_WRITE = {
    "add",
    "commit",
    "merge",
    "checkout",
    "switch",
    "restore",
    "rm",
    "mv",
    "branch",
    "worktree",
    "cherry-pick",
    "revert",
}
DRAIN_GIT_FORCE = {"-f", "-B", "-C", "-D", "--hard"}


def orchestrator_alert(event, state):
    if event.get("tool_name") != "Bash" or event.get("agent_id"):
        return False
    orchestrator = next(
        (
            row
            for row in reversed(state.get("inventory", {}).get("threads", []))
            if row.get("role") == "orchestrator" and not row.get("complete")
        ),
        {},
    )
    pane = orchestrator.get("pane_id")
    if type(pane) is not int or pane < 0:
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
    # The one-call helper resolves the same orchestrator pane and may only append its message.
    helper = ["/tmp/factory-fp-0925/swarm/bin/alert-orchestrator.sh"]
    if len(groups) == 1 and groups[0][:1] == helper:
        rest = groups[0][1:]
        if rest[:1] == ["--log"]:
            rest = rest[1:]
        return len(rest) == 1 and bool(rest[0]) and not rest[0].startswith("-")
    sent = False
    for words in groups:
        if words == ["echo", "sent"] and sent:
            continue
        if words[:5] != ["wezterm", "cli", "send-text", "--pane-id", str(pane)]:
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
    expansions = re.findall(r"""'[^']*'|"(?:\\[\s\S]|[^"\\])*"|\\[\s\S]|[`$]""", command)
    if any(
        token in {"`", "$"}
        or (token.startswith('"') and any(c in re.sub(r"\\[\s\S]", "", token) for c in ("`", "$")))
        for token in expansions
    ):
        return False
    # A sequence or pipeline is read-only when every stage is, e.g. rg ... | head -20 && wc -l f.
    # Tokenize with quotes first so a quoted | or ; stays part of its argument.
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    stages = [[]]
    for token in tokens:
        if token in {"|", "||", "&&", ";", "\n"}:
            stages.append([])
        elif set(token) <= set(";&|<>\n"):
            return False
        else:
            stages[-1].append(token)
    stages = [s for s in stages if s] if tokens and tokens[-1] == "\n" else stages
    return all(read_stage(s) for s in stages)


def options(args):
    """Return (short letters, long names) of option words before a -- terminator.

    Combined short options such as -nf count every letter; letters inside an attached
    option argument count too, which can only refuse a harmless read, never allow a write.
    """
    short, long = set(), set()
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("--"):
            long.add(arg[2:].split("=", 1)[0])
        elif arg.startswith("-") and len(arg) > 1:
            short.update(arg[1:])
    return short, long


def read_stage(words):
    if not words:
        return False
    name, args = words[0], words[1:]
    short, long = options(args)
    # Readers with options that write, execute helpers or never exit are checked for them;
    # anything uncertain falls through to the normal guarded path.
    if name == "sed":
        scripts = [a for a in args if not a.startswith("-")]
        return (
            args[:1] == ["-n"]
            and len(scripts) >= 1
            and re.fullmatch(r"\d+(,\d+)?p", scripts[0]) is not None
            and all(not a.startswith("-") for a in args[1:])
        )
    if name == "tail":
        return not (short & {"f", "F"} or any(n.startswith(("f", "retry")) for n in long))
    if name == "rg":
        return not ("z" in short or any(n.startswith(("pre", "search-zip")) for n in long))
    if name in READ_COMMANDS:
        return True
    if words[:3] in (["wezterm", "cli", "get-text"], ["wezterm", "cli", "list"]):
        return True
    if words[0] != "git":
        return False
    return (
        name == "git"
        and len(args) > 0
        and args[0] in READ_GIT
        and not any(n.startswith(("output", "ext-diff", "textconv")) for n in long)
        and "o" not in options(args[1:])[0]
    )


def report_context_start(event, snapshot, cap):
    transcript = event.get("transcript_path")
    if not transcript:
        return cap
    try:
        lines = Path(transcript).read_text().splitlines()
    except OSError:
        return cap
    for line in lines:
        try:
            message = json.loads(line).get("message", {})
        except ValueError:
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if (
                item.get("type") == "tool_use"
                and item.get("name") in {"Write", "Edit"}
                and Path(item.get("input", {}).get("file_path", ".")).resolve()
                == snapshot.resolve()
            ):
                usage = message.get("usage", {})
                return max(
                    cap,
                    sum(
                        usage.get(k, 0)
                        for k in (
                            "input_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens",
                        )
                    ),
                )
    return cap


def gate_tag(words):
    for index, word in enumerate(words):
        name = Path(word.strip("'\"")).name
        if name in {"unit_gate.py", "unit_gate.sh"} and len(words) > index + 2:
            return words[index + 2]
        if name in {"red_check.py", "merge_unit.sh"} and len(words) > index + 1:
            return words[index + 1]
    return None


def drain_git(words, path, state):
    if len(words) < 4 or words[1] != "-C" or not words[2].startswith("/"):
        raise ValueError("Concurrent drain git requires git -C <absolute path> <subcommand>")
    root = Path(words[2]).resolve()
    if root == path.parent.resolve():
        raise ValueError("Concurrent drain git may not run in the run directory")
    common = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            "--show-toplevel",
        ],
        capture_output=True,
        text=True,
    )
    if common.returncode != 0 or len(common.stdout.split("\n")) < 2:
        raise ValueError("Concurrent drain git requires a repository path")
    git_dir, top = common.stdout.split("\n")[:2]
    main_checkout = Path(git_dir).resolve().parent == Path(top).resolve()
    command, rest = words[3], words[4:]
    branch_read = command == "branch" and (
        rest == ["--show-current"]
        or (rest[:1] == ["--list"] and not any(word.startswith("-") for word in rest[1:]))
    )
    if (
        command not in DRAIN_GIT_READ
        and not branch_read
        and (main_checkout or command not in DRAIN_GIT_WRITE)
    ):
        raise ValueError("Concurrent drain git subcommand is not allowed here")
    if command == "worktree" and rest[:1] not in (["add"], ["list"]):
        raise ValueError("Concurrent drain git worktree allows only add and list")
    if command == "branch" and not branch_read and any(word.startswith("-") for word in rest):
        raise ValueError(
            "Concurrent drain git branch allows only create, --list [patterns], and --show-current"
        )
    if any(word in DRAIN_GIT_FORCE or word.startswith("--force") for word in rest):
        raise ValueError("Concurrent drain git rejects force options")
    scopes = [
        Path(s).resolve() for drain in state["draining_leads"].values() for s in drain["scopes"]
    ]
    branches = set()
    for scope in scopes:
        head = subprocess.run(
            ["git", "-C", str(scope), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True,
            text=True,
        )
        if head.returncode == 0 and head.stdout.strip():
            branches.add(head.stdout.strip())
    for word in words[2:]:
        target = (root / word).resolve()
        if any(target.is_relative_to(scope) or scope.is_relative_to(target) for scope in scopes):
            raise ValueError("Command scope overlaps a draining lead")
        for name in re.split(r"\.\.\.?|[:~^@=]", word):
            if name.lstrip("+").removeprefix("refs/heads/") in branches:
                raise ValueError("Concurrent drain git names a draining lead branch")


def reviewer_candidate_root(state: ReviewerState, reviewer: ReviewerSession, path: Path) -> Path:
    reservations = [
        reservation
        for allocation, reservation in live_reviewer_reservations(
            state, reviewer["parent_session_id"]
        )
        if (run := state.get("runs", {}).get(reservation["active_run"], {})).get("allocation_id")
        == allocation
        and run.get("session_id") == reviewer["session_id"]
        and run.get("agent_id") == reviewer.get("agent_id")
        and not run.get("complete")
    ]
    packets = {
        Path(output).with_name(Path(output).name.removesuffix(suffix) + "-candidate.json")
        for reservation in reservations
        for output in reservation.get("output_paths", [])
        for suffix in ("-code-review.json", "-product-review.json")
        if output.endswith(suffix) and Path(output).parent.resolve() == path.parent.resolve()
    }
    if len(reservations) != 1 or len(packets) != 1:
        raise ValueError("Reviewer pytest requires its reserved candidate packet")
    try:
        worktree = json.loads(packets.pop().read_text())["worktree"]
        if not isinstance(worktree, str) or not Path(worktree).is_absolute():
            raise ValueError("Candidate worktree must be absolute")
        return Path(worktree).resolve()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("Reviewer pytest requires a valid candidate worktree") from error


def check(event, state, path, used, cap):
    read_only = readonly(event)
    if (
        read_only
        and not event.get("agent_id")
        and not any(
            row.get("role") == "reviewer" and row.get("session_id") == event.get("session_id")
            for row in state.get("inventory", {}).get("sessions", [])
        )
    ):
        if (
            event.get("tool_name") == "Bash"
            and state.get("draining_leads")
            and event.get("session_id") == (state.get("active_lead") or {}).get("model_session_id")
        ):
            words = shlex.split(event.get("tool_input", {}).get("command", ""))
            values: list[str] = []
            for word in values + words[1:]:
                if word.startswith("/") and any(
                    Path(word).resolve().is_relative_to(Path(scope))
                    or (
                        Path(word).resolve() != path.parent.resolve()
                        and Path(scope).is_relative_to(Path(word).resolve())
                    )
                    for drain in state["draining_leads"].values()
                    for scope in drain["scopes"]
                ):
                    raise ValueError("Command scope overlaps a draining lead")
        return True
    if not read_only:
        check_action(state, path, "dispatch")
    tool = event.get("tool_name")
    args = event.get("tool_input", {})
    session = event.get("session_id")
    draining = state.get("draining_leads", {}).get(session)
    if draining and not event.get("agent_id"):
        originating_lead({**state, "active_lead": draining["lead"]})
        report = path.parent / "handoff" / ("lead-" + session + "-drain.md")
        if (
            tool in {"Write", "Edit"}
            and Path(args.get("file_path", ".")).resolve() == report.resolve()
        ):
            return True
        words = shlex.split(args.get("command", "")) if tool == "Bash" else []
        if words == ["/tmp/factory-fp-0925/swarm/bin/interior-lead.sh", "--drain-ready", session]:
            return True
        if orchestrator_alert(event, state):
            return True
        completion = draining.get("completion", {})
        for target in completion.get("targets", []):
            expected = [
                "wezterm",
                "cli",
                "send-text",
                "--pane-id",
                str(target["pane"]),
                "--no-paste",
                completion["message"],
            ]
            if words == expected or words == [*expected[:-1], "\r"]:
                return True
            if args.get("command") == " ".join(expected[:-1]) + " $'\\r'":
                return True
            if words == ["printf", "\\r", "|", *expected[:-1]]:
                return True
        kill = ["wezterm", "cli", "kill-pane", "--pane-id"]
        own_pane = [[str(draining["lead"].get("pane"))], ["$WEZTERM_PANE"]]
        if words[:4] == kill and words[4:] in own_pane:
            # Recheck retirement's finished-work requirements even after completion was recorded.
            from pilot_lead import drain_finished

            drain_finished(path, state, session, draining)
            return True
        raise ValueError("A draining lead may only report and finalize its owned jobs")
    reviewer = next(
        (
            row
            for row in state.get("inventory", {}).get("sessions", [])
            if row.get("role") == "reviewer"
            and (
                (
                    event.get("agent_id")
                    and row.get("agent_id") == event["agent_id"]
                    and row.get("parent_session_id") == session
                )
                or (not event.get("agent_id") and row.get("session_id") == session)
            )
            and (
                row.get("parent_session_id")
                == (state.get("active_lead") or {}).get("model_session_id")
                or row.get("parent_session_id") in state.get("draining_leads", {})
            )
        ),
        None,
    )
    if event.get("agent_id") or reviewer is not None:
        if reviewer is None:
            if (
                read_only
                and event.get("agent_type") != "p1-pilot-reviewer"
                and not any(
                    row.get("role") == "reviewer" and row.get("agent_id") == event.get("agent_id")
                    for row in state.get("inventory", {}).get("sessions", [])
                )
            ):
                return True
            raise ValueError("Subagent identity is not registered for this lead")
        if reviewer.get("pending"):
            reviewer_session = reviewer["session_id"]
            reviewer_event(
                {
                    **event,
                    "session_id": reviewer["parent_session_id"],
                    "agent_id": reviewer["agent_id"],
                    "agent_type": "p1-pilot-reviewer",
                    "hook_event_name": "PreToolUse",
                },
                path,
            )
            state = json.loads(path.read_text())
            reviewer = next(
                row
                for row in state["inventory"]["sessions"]
                if row.get("session_id") == reviewer_session
            )
        if tool == "SubagentHandback":
            return True
        if reviewer.get("complete"):
            raise ValueError("Completed reviewer may only hand back its result")
        if read_only:
            return True
        output = str(Path(args.get("file_path", ".")).resolve())
        if tool in {"Write", "Edit"} and output in reviewer.get("output_paths", []):
            return True
        if tool == "Bash":
            command = args.get("command", "")
            words = shlex.split(command)
            if any(c in command for c in (";", "|", "&", ">", "<", "`", "$", "\n")):
                raise ValueError("Reviewer requires a single verification command")
            if (
                words[:2] == ["git", "-C"]
                and len(words) > 3
                and words[3] in {"diff", "show"}
                and not any(word.startswith("--output") for word in words[4:])
            ):
                return True
            if words and (
                words[0] == "pytest"
                or (
                    Path(words[0]).is_absolute()
                    and Path(words[0]).parts[-3:] == (".venv", "bin", "pytest")
                )
            ):
                candidate_root = None
                if words[1:2] == ["-c"]:
                    candidate_root = reviewer_candidate_root(state, reviewer, path)
                    if words[1:5] != [
                        "-c",
                        str(candidate_root / "pyproject.toml"),
                        "-o",
                        f"pythonpath={candidate_root} {candidate_root / 'src'}",
                    ]:
                        raise ValueError(
                            "Reviewer pytest requires exact candidate config and imports"
                        )
                    words = [words[0], *words[5:]]
                nodes = []
                arguments = iter(words[1:])
                for word in arguments:
                    option, separator, value = word.partition("=")
                    if option in {"--junitxml", "--junit-xml", "--basetemp"}:
                        output = value if separator else next(arguments, "")
                        if not output or str(Path(output).resolve()) not in reviewer.get(
                            "output_paths", []
                        ):
                            raise ValueError("Reviewer pytest may write only registered outputs")
                    elif word in {
                        "-q",
                        "-v",
                        "-vv",
                        "-x",
                        "--disable-warnings",
                        "--no-header",
                        "--no-summary",
                    }:
                        continue
                    elif not word.startswith("-") and ".py::" in word:
                        if candidate_root is not None:
                            node_path = Path(word.split("::", 1)[0])
                            if (
                                not node_path.is_absolute()
                                or not node_path.resolve().is_relative_to(candidate_root)
                            ):
                                raise ValueError(
                                    "Reviewer pytest nodes must belong to its candidate"
                                )
                        nodes.append(word)
                    else:
                        raise ValueError("Reviewer pytest requires explicit test node ids")
                if nodes:
                    return True
        raise ValueError("Reviewer may only check the packet and write registered outputs")
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
    if tool in {"Write", "Edit"}:
        target = Path(args.get("file_path", ".")).resolve()
        if any(
            target.is_relative_to(Path(scope))
            for drain in state.get("draining_leads", {}).values()
            for scope in drain["scopes"]
        ):
            raise ValueError("Write overlaps a draining lead scope")
    if tool == "Bash" and state.get("draining_leads"):
        command = args.get("command", "")
        words = shlex.split(command)
        shell_tokens = re.findall(
            r"""'[^']*'|"(?:\\[\s\S]|[^"\\])*"|\\[\s\S]|[;|&<>`$\n]""", command
        )
        if not words or any(
            token[0] not in {"'", '"', "\\"}
            or (
                token.startswith('"')
                and any(c in re.sub(r"\\[\s\S]", "", token) for c in ("`", "$"))
            )
            for token in shell_tokens
        ):
            raise ValueError("Concurrent drain requires an exact native command")
        values = []
        while words and "=" in words[0] and not words[0].startswith("/"):
            key, value = words.pop(0).split("=", 1)
            if key not in LAUNCH_ENV and not (
                (key == "ART" and Path(value).resolve() == path.parent.resolve())
                or (key == "PILOT_BASE_REF" and value in {"pre-main", "main"})
                or (
                    key == "PILOT_LICENSED_IMAGES"
                    and Path(value).resolve().parts[-3:]
                    == ("data", "licensed-floorplans", "images")
                )
            ):
                raise ValueError("Concurrent drain rejects an unknown environment key")
            values.append(value)
        if not words:
            raise ValueError("Concurrent drain requires an exact native command")
        native = Path(words[0])
        repo = os.environ.get("REPO")
        if repo and native == Path(repo) / ".venv/bin/python" and len(words) > 1:
            native = Path(words[1])
            if native.parent != Path("/tmp/factory-fp-0925/swarm/bin") or native.name not in {
                "red_check.py",
                "review_check.py",
                "run_state.py",
                "jev_triage.py",
            }:
                raise ValueError("Concurrent drain requires an exact native command")
        if words[0] == "git":
            drain_git(words, path, state)
        elif native.parent != Path("/tmp/factory-fp-0925/swarm/bin") or native.name not in {
            "alert-orchestrator.sh",
            "interior-lead.sh",
            "builder-start.sh",
            "codex-agent.sh",
            "prepare_candidate.sh",
            "unit_gate.sh",
            "red_check.py",
            "merge_unit.sh",
            "review_check.py",
            "run_state.py",
            "jev_triage.py",
        }:
            raise ValueError("Concurrent drain requires an exact native command")
        if native.name == "codex-agent.sh" and any(
            re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", w) and w.split("=", 1)[0] not in LAUNCH_ENV
            for w in words[1:]
        ):
            raise ValueError("Concurrent drain rejects an unknown environment key")
        for word in values + [w.split("=", 1)[-1] for w in words[1:]]:
            if word.startswith("/") and any(
                Path(word).resolve().is_relative_to(Path(scope))
                or (
                    Path(word).resolve() != path.parent.resolve()
                    and Path(scope).is_relative_to(Path(word).resolve())
                )
                for drain in state["draining_leads"].values()
                for scope in drain["scopes"]
            ):
                raise ValueError("Command scope overlaps a draining lead")
        live = processes()
        drain_tags = {
            gate_tag(gate["command"].split())
            for drain in state["draining_leads"].values()
            for gate in drain.get("gates", [])
            if same_identity(gate, live.get(gate["pid"]))
            and not live[gate["pid"]]["status"].startswith("Z")
        }
        if (
            drain_tags
            and native.name in {"unit_gate.sh", "red_check.py", "merge_unit.sh"}
            and (None in drain_tags or gate_tag(words) in drain_tags | {None})
        ):
            raise ValueError("A draining lead owns an unresolved gate or benchmark")
    words = shlex.split(args.get("command", "")) if tool == "Bash" else []
    launcher = "/tmp/factory-fp-0925/swarm/bin/interior-lead.sh"
    if words == [launcher, "--handoff-begin"]:
        return True
    if transfer.get("token") and words == [launcher, "--handoff-start", transfer["token"]]:
        return True
    if state.get("dispatch_frozen") and words == [launcher, "--drain-ready", session]:
        return True
    snapshot = path.parent / "handoff" / (str(transfer.get("token")) + ".md")
    if (
        tool in {"Write", "Edit"}
        and transfer.get("token")
        and file == str(snapshot.resolve())
        and state.get("dispatch_frozen")
    ):
        return True
    report = path.parent / "handoff" / ("lead-" + session + "-orchestrator.md")
    if (
        tool in {"Write", "Edit"}
        and state.get("dispatch_frozen")
        and transfer.get("token")
        and snapshot.is_file()
        and cap < used <= report_context_start(event, snapshot, cap) + cap // 10
        and file == str(report.resolve())
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
        "cache_reads": sum(u.get("cache_read_input_tokens", 0) for u in messages.values()),
    }


def record_lead(event, path):
    if event.get("agent_id"):
        return
    state = json.loads(path.read_text())
    session = event.get("session_id")
    owner = (
        (state.get("draining_leads", {}).get(session) or {}).get("lead")
        or state.get("active_lead")
        or {}
    )
    if session != owner.get("model_session_id"):
        return
    originating_lead({**state, "active_lead": owner})
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


def first_user_allocation(transcript: Path) -> str:
    allocation = None
    try:
        with transcript.open() as source:
            for line in source:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("Invalid reviewer transcript message")
                if row.get("type") != "user":
                    continue
                message = row.get("message")
                if not isinstance(message, dict) or message.get("role") != "user":
                    raise ValueError("Invalid reviewer user message")
                content = message.get("content")
                if isinstance(content, list):
                    content = "".join(
                        block["text"]
                        for block in content
                        if isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    )
                if isinstance(content, str) and content.startswith("ALLOCATION: "):
                    allocation = content.split("\n", 1)[0][len("ALLOCATION: ") :]
                break
    except OSError as error:
        raise ValueError("Reviewer transcript is unavailable") from error
    if not allocation or any(c.isspace() for c in allocation):
        raise ValueError("Reviewer first user message needs an allocation line")
    return allocation


def live_reviewer_reservations(
    state: ReviewerState, parent: str
) -> list[tuple[str, ReviewerReservation]]:
    return [
        (key, value)
        for key, value in state.get("reservations", {}).items()
        if value["role"] == "reviewer"
        and value.get("parent_session_id") == parent
        and value.get("active_run")
        and not value.get("released")
    ]


def reviewer_start(
    state: ReviewerState,
    event: Mapping[str, object],
    parent: str,
    agent: str,
    reviewer: ReviewerSession | None,
) -> bool:
    if parent in state.get("draining_leads", {}):
        raise ValueError("A draining lead cannot start a reviewer")
    if reviewer is not None:
        return False
    transcript_path = event["transcript_path"]
    if not isinstance(transcript_path, str) or not transcript_path:
        raise ValueError("Reviewer needs its parent transcript path")
    transcript = Path(transcript_path).with_suffix("") / "subagents" / ("agent-" + agent + ".jsonl")
    reviewer = {
        "session_id": parent + "/" + agent,
        "agent_id": agent,
        "parent_session_id": parent,
        "role": "reviewer",
        "transcript_path": str(transcript),
        "complete": False,
        "pending": True,
    }
    rows = live_reviewer_reservations(state, parent)
    if len(rows) == 1:
        reviewer["output_paths"] = rows[0][1].get("output_paths", [])
    state["inventory"]["sessions"].append(reviewer)
    return True


def record_reviewer(
    state: ReviewerState,
    reviewer: ReviewerSession,
    data: dict[str, object],
    transcript: Path,
    path: Path,
) -> None:
    usage = (
        claude_usage(transcript)
        if data["complete"]
        else {"new_tokens": 0, "cache_reads": 0, "usage_status": "known"}
    )
    data.update(usage if usage is not None else {"usage_status": "unknown"})
    apply(state, "launcher", "record-run", data, path)
    reviewer["pending"] = False


def reviewer_bind(
    state: ReviewerState, parent: str, agent: str, reviewer: ReviewerSession, path: Path
) -> bool:
    if not reviewer.get("pending"):
        return False
    if parent in state.get("draining_leads", {}):
        raise ValueError("A draining lead cannot bind a reviewer")
    transcript = Path(reviewer["transcript_path"])
    allocation = first_user_allocation(transcript)
    reservation: ReviewerReservation = state.get("reservations", {}).get(allocation, {})
    run_id = reservation.get("active_run")
    run: ReviewerRun = state.get("runs", {}).get(run_id, {}) if run_id else {}
    session = parent + "/" + agent
    if (
        reservation.get("role") != "reviewer"
        or reservation.get("parent_session_id") != parent
        or reservation.get("released")
        or not reservation.get("output_paths")
        or not run_id
        or run.get("allocation_id") != allocation
        or run.get("complete")
        or run.get("session_id") not in (None, session)
        or run.get("agent_id") not in (None, agent)
    ):
        raise ValueError("Reviewer needs its active allocation with exact output paths")
    record_reviewer(
        state,
        reviewer,
        {
            "allocation_id": allocation,
            "run_id": run_id,
            "session_id": session,
            "agent_id": agent,
            "complete": False,
            "transcript_path": str(transcript),
        },
        transcript,
        path,
    )
    return True


def reviewer_stop_unbound(
    state: ReviewerState,
    event: Mapping[str, object],
    agent: str,
    reviewer: ReviewerSession,
    path: Path,
) -> bool:
    transcript_path = event.get("agent_transcript_path")
    transcript = Path(
        transcript_path
        if isinstance(transcript_path, str) and transcript_path
        else reviewer["transcript_path"]
    )
    record_reviewer(
        state,
        reviewer,
        {
            "run_id": "reviewer-" + reviewer["session_id"],
            "session_id": reviewer["session_id"],
            "agent_id": agent,
            "role": "reviewer",
            "complete": True,
            "transcript_path": str(transcript),
        },
        transcript,
        path,
    )
    return True


def reviewer_stop(
    state: ReviewerState,
    event: Mapping[str, object],
    parent: str,
    agent: str,
    reviewer: ReviewerSession,
    path: Path,
) -> bool:
    session = parent + "/" + agent
    known = [
        (key, value)
        for key, value in state.get("runs", {}).items()
        if value.get("session_id") == session and value.get("allocation_id")
    ]
    unbound_run = state.get("runs", {}).get("reviewer-" + session, {})
    if unbound_run.get("session_id") == session and not unbound_run.get("allocation_id"):
        return reviewer_stop_unbound(state, event, agent, reviewer, path)
    transcript_path = event.get("agent_transcript_path")
    transcript = Path(
        transcript_path
        if isinstance(transcript_path, str) and transcript_path
        else reviewer["transcript_path"]
    )
    if len(known) == 1:
        run_id, run = known[0]
        allocation = run["allocation_id"]
        reservation = state.get("reservations", {}).get(allocation, {})
        if (
            reservation.get("role") != "reviewer"
            or reservation.get("parent_session_id") != parent
            or run.get("agent_id") not in (None, agent)
        ):
            return reviewer_stop_unbound(state, event, agent, reviewer, path)
    elif not known and reviewer.get("pending"):
        try:
            allocation = first_user_allocation(transcript)
        except ValueError:
            return reviewer_stop_unbound(state, event, agent, reviewer, path)
        reservation = state.get("reservations", {}).get(allocation, {})
        active_run = reservation.get("active_run")
        run = state.get("runs", {}).get(active_run, {}) if active_run else {}
        if (
            reservation.get("role") != "reviewer"
            or reservation.get("parent_session_id") != parent
            or reservation.get("released")
            or not reservation.get("output_paths")
            or not active_run
            or run.get("allocation_id") != allocation
            or run.get("complete")
            or run.get("session_id") not in (None, session)
            or run.get("agent_id") not in (None, agent)
        ):
            return reviewer_stop_unbound(state, event, agent, reviewer, path)
        run_id = active_run
    else:
        return reviewer_stop_unbound(state, event, agent, reviewer, path)
    record_reviewer(
        state,
        reviewer,
        {
            "allocation_id": allocation,
            "run_id": run_id,
            "session_id": session,
            "agent_id": agent,
            "complete": True,
            "transcript_path": str(transcript),
        },
        transcript,
        path,
    )
    apply(state, "launcher", "release", {"allocation_id": allocation, "run_id": run_id}, path)
    return True


def update_reviewer(
    state: ReviewerState, event: Mapping[str, object], parent: str, agent: str, path: Path
) -> bool:
    hook = event["hook_event_name"]
    owner = (state.get("draining_leads", {}).get(parent) or {}).get("lead") or state["active_lead"]
    originating_lead({**state, "active_lead": owner})
    if parent != owner["model_session_id"]:
        raise ValueError("Reviewer parent is not the active lead")
    sessions = state.setdefault("inventory", {}).setdefault("sessions", [])
    reviewer = next(
        (row for row in sessions if row.get("session_id") == parent + "/" + agent), None
    )
    if hook == "SubagentStart":
        return reviewer_start(state, event, parent, agent, reviewer)
    if (
        reviewer is None
        or reviewer.get("role") != "reviewer"
        or reviewer.get("agent_id") != agent
        or reviewer.get("parent_session_id") != parent
    ):
        raise ValueError("Subagent identity is not registered for this lead")
    if hook == "PreToolUse":
        return reviewer_bind(state, parent, agent, reviewer, path)
    return reviewer_stop(state, event, parent, agent, reviewer, path)


def reviewer_event(event: Mapping[str, object], path: Path) -> None:
    if event.get("agent_type") != "p1-pilot-reviewer":
        return
    hook = event["hook_event_name"]
    if hook not in {"SubagentStart", "PreToolUse", "SubagentStop"}:
        raise ValueError("Unknown reviewer hook")
    parent = event["session_id"]
    agent = event["agent_id"]
    if not isinstance(parent, str) or not isinstance(agent, str) or not parent or not agent:
        raise ValueError("Reviewer needs parent and agent identities")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state: ReviewerState = json.loads(path.read_text())
        if not update_reviewer(state, event, parent, agent, path):
            return
        fd, name = tempfile.mkstemp(prefix=".run-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(state, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    if hook == "PreToolUse":
        return
    evidence = path.parent / ("reviewer-hook-" + agent + "-" + str(hook) + ".json")
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
        reviewer_event(json.load(sys.stdin), Path(os.environ.get("PILOT_RUN", str(DEFAULT_RUN))))
    except (ValueError, OSError, KeyError) as error:
        print("Pilot reviewer hook: " + str(error), file=sys.stderr)
        print_refusal_note(error)
        raise SystemExit(2) from error
