"""PreToolUse guard for floorplan-render builder agents (docs/token-budget.md).

Blocks waiting and polling. Past the context cap a builder may only hand off;
a lead gets a reserve of 10% of its cap for its hand-off and successor only,
and past the reserve only the spawn script.
Exit 2 blocks the tool call and shows stderr to the agent.
"""

import ctypes
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from itertools import takewhile
from pathlib import Path
from typing import NoReturn

from run_state import RunState, continues_lead_session, print_refusal_note

CTL_KERN = 1
KERN_PROCARGS2 = 49

CAP_BASE = {"claude-opus-5-5": 166000, "claude-sonnet-5": 166000}
CAP_BASE_DEFAULT = 166000
RECOVER_KEYS = (
    "FP_LEAD",
    "LEAD_AGENT",
    "FP_CAP_FRACTION",
    "FP_CONTEXT_CAP",
    "PILOT_RUN",
    "FP_LEAD_SPAWN",
)


def launch_environment(pid: int) -> dict[str, str]:
    """Read only guard keys from macOS KERN_PROCARGS2, never ps output."""
    if sys.platform != "darwin":
        return {}
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        size = ctypes.c_size_t()
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
            return {}
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return {}
        raw = buffer.raw[: size.value]
        argc = int.from_bytes(raw[:4], sys.byteorder, signed=True)
        if argc < 1:
            return {}
        offset = raw.index(b"\0", 4) + 1
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        for _ in range(argc):
            offset = raw.index(b"\0", offset) + 1
        result = {}
        for entry in takewhile(bool, raw[offset:].split(b"\0")):
            key, separator, value = entry.partition(b"=")
            name = key.decode("utf-8", errors="surrogateescape")
            if separator and name in RECOVER_KEYS:
                result[name] = value.decode("utf-8", errors="surrogateescape")
        return result
    except (OSError, ValueError, AttributeError):
        return {}


def lead_session_matches(event: Mapping[str, object], state: RunState) -> bool:
    active = state.get("active_lead") or {}
    session = event.get("session_id")
    transcript_path = event.get("transcript_path")
    if not isinstance(session, str) or not session or not isinstance(transcript_path, str):
        return False
    transcript = Path(transcript_path)
    if transcript.name != session + ".jsonl" or not transcript.is_file():
        return False
    return session == active.get("model_session_id") or continues_lead_session(
        state, session, transcript
    )


def recover_lead_environment(event: Mapping[str, object]) -> str | None:
    if ("FP_LEAD" in os.environ and "LEAD_AGENT" in os.environ) or event.get("agent_id"):
        return None
    from run_state import DEFAULT_RUN, processes, same_identity

    paths = (
        [Path(os.environ["PILOT_RUN"])]
        if "PILOT_RUN" in os.environ
        else [DEFAULT_RUN, Path("/tmp/factory-fp-0925/swarm/interior-p1-poc/run.json")]
    )
    for path in paths:
        try:
            state: RunState = json.loads(path.read_text())
            if not lead_session_matches(event, state):
                continue
            active = state.get("active_lead") or {}
            pid = active.get("pid")
            if not isinstance(pid, int) or not same_identity(active, processes().get(pid)):
                continue
            env = launch_environment(pid)
            if env.get("FP_LEAD") != "1" or env.get("LEAD_AGENT") != "p1-pilot-lead":
                continue
            if "PILOT_RUN" in env and Path(env["PILOT_RUN"]).resolve() != path.resolve():
                continue
            if not same_identity(active, processes().get(pid)):
                continue
            for key, value in env.items():
                os.environ.setdefault(key, value)
            return str(active["model_session_id"])
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            subprocess.SubprocessError,
        ):
            continue
    return None


def parent_is_headless() -> bool:
    pid = os.getppid()
    visited = set()
    while pid > 1 and pid not in visited:
        visited.add(pid)
        try:
            row = subprocess.run(
                ["ps", "-o", "ppid=,command=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.split(None, 1)
            if len(row) != 2:
                return False
            pid = int(row[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        argv = row[1].split(" -- ", 1)[0]
        if (
            re.search(r"(^|/)claude(\s|$)", argv) is not None
            and re.search(r"\s(-p|--print)(\s|$)", argv) is not None
        ):
            return True
    return False


POLL = re.compile(
    "\\bsleep\\b|\\buntil\\b|\\bwhile\\s+(true|:)|\\bwatch\\b|\\binotifywa"
    "it\\b|tail\\s+-[a-zA-Z]*f|\\bwait\\b\\s*($|;|&&)"
)
_G = r"\bgit(?:\s+-[cC]\s*\S+|\s+--\S+)*\s+"
GIT_DANGER = re.compile(
    _G + r"(?:push\b"
    r"|reset\b[^|;&]*--hard"
    r"|clean\b[^|;&]*(?:-[a-zA-Z]*f|--force)"
    "|branch\\b[^|;&]*(?:-[a-zA-Z]*D\\b|--delete[^|;&]*--force|--fo"
    "rce[^|;&]*--delete|-d[^|;&]*--force|--force[^|;&]*-d\\b)"
    r"|checkout\s+(?:\.|--\s|[^-\s][^|;&]*\s--\s)"
    r"|restore\b(?![^|;&]*--staged\b)"
    r"|worktree\s+remove\b"
    r"|stash\s+(?:drop|clear)\b)"
)
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^\s*\2\s*$", re.S | re.M)
SHELL_BG = re.compile(r"(?<!&)&(?!&)\s*($|;|\n)|\bnohup\b|\bdisown\b")

event = json.load(sys.stdin)
inherited_pilot = os.environ.get("LEAD_AGENT") == "p1-pilot-lead"
recovered_session = recover_lead_environment(event)
FRACTION = float(os.environ.get("FP_CAP_FRACTION", "0.90"))
CAP_OVERRIDE = int(os.environ["FP_CONTEXT_CAP"]) if os.environ.get("FP_CONTEXT_CAP") else None
LEAD = os.environ.get("FP_LEAD") == "1" and not parent_is_headless()
PILOT = os.environ.get("LEAD_AGENT") == "p1-pilot-lead" and (
    inherited_pilot or os.environ.get("FP_LEAD") == "1"
)
pilot_event = {**event, "session_id": recovered_session} if recovered_session and LEAD else event
tool, args = event.get("tool_name", ""), event.get("tool_input") or {}
if LEAD and event.get("session_id"):
    try:
        os.makedirs("/tmp/factory-fp-0925/swarm/fp3d", exist_ok=True)
        with open(f"/tmp/factory-fp-0925/swarm/fp3d/lead-session-{os.getppid()}.txt", "w") as f:
            f.write(event["session_id"] + "\n")
    except OSError:
        pass


def block(msg: str, error: BaseException | None = None) -> NoReturn:
    print(msg, file=sys.stderr)
    print_refusal_note(error)
    sys.exit(2)


def context() -> tuple[int, str | None]:
    try:
        with open(event["transcript_path"]) as transcript:
            lines = transcript.read().splitlines()
    except (KeyError, OSError):
        return 0, None
    for line in reversed(lines):
        try:
            msg = json.loads(line)["message"]
            u = msg["usage"]
        except (ValueError, KeyError, TypeError):
            continue
        return u["input_tokens"] + u.get("cache_read_input_tokens", 0) + u.get(
            "cache_creation_input_tokens", 0
        ), msg.get("model")
    return 0, None


def cap_for(model: str | None) -> int:
    if CAP_OVERRIDE:
        return CAP_OVERRIDE
    if os.environ.get("LEAD_AGENT") == "p1-pilot-lead":
        return 120000
    return int(CAP_BASE.get(model or "", CAP_BASE_DEFAULT) * FRACTION)


if PILOT:
    from pilot_guard import orchestrator_alert
    from run_state import DEFAULT_RUN

    try:
        run_path = Path(os.environ.get("PILOT_RUN", str(DEFAULT_RUN)))
        if orchestrator_alert(pilot_event, json.loads(run_path.read_text())):
            sys.exit(0)
    except (ValueError, OSError, KeyError):
        pass

if tool == "Bash":
    cmd = args.get("command", "")
    if args.get("run_in_background") and not LEAD:
        block(
            "Blocked: background jobs die when a -p turn ends. Run it in "
            "the foreground with a timeout up to 600000 ms."
        )
    if GIT_DANGER.search(cmd) and not LEAD:
        block(
            "Blocked: git push, reset --hard, clean -f, branch -D, checko"
            "ut ./restore . and worktree remove are not available to buil"
            "ders. Commit on your branch; the lead merges."
        )
    if SHELL_BG.search(cmd) and not LEAD:
        block(
            "Blocked: shell background jobs (a trailing &, nohup, disown)"
            " die when a -p turn ends. Run it in the foreground."
        )
    if POLL.search(HEREDOC.sub("", cmd)):
        block(
            "Blocked: no sleeping, polling or waiting loops (docs/token-b"
            "udget.md). The Blender lock blocks by itself; run the real c"
            "ommand in the foreground."
        )

used, model = context()
CAP = cap_for(model)
RESERVE = CAP // 10
if PILOT:
    from pilot_guard import check, record_lead
    from run_state import DEFAULT_RUN

    try:
        run_path = Path(os.environ.get("PILOT_RUN", str(DEFAULT_RUN)))
        record_lead(pilot_event, run_path)
        check(pilot_event, json.loads(run_path.read_text()), run_path, used, CAP)
    except (ValueError, OSError, KeyError) as error:
        try:
            with (run_path.parent / "logs" / "guard-rejections.jsonl").open("a") as rejections:
                rejections.write(
                    json.dumps(
                        {
                            "session": event.get("session_id"),
                            "tool": tool,
                            "input": args.get("command") or args.get("file_path"),
                            "error": str(error),
                        }
                    )
                    + "\n"
                )
        except OSError:
            pass
        block(f"Pilot guard: {error}", error)
    sys.exit(0)
if used > CAP:
    path = args.get("file_path", "")
    cmd = args.get("command", "")
    only_add_commit = (
        re.fullmatch(
            r"\s*git\s+(add|commit)\b[^|;&]*(\s*(&&|;)\s*git\s+(add|commit)\b[^|;&]*)*\s*", cmd
        )
        is not None
    )
    cmd_no_msg = re.sub(r"""(-m|--message|-[a-z]*m)(\s+|=)("[^"]*"|'[^']*'|\S+)""", " ", cmd)
    paths = [t for t in re.findall(r"(?<!\S)(?!-)[\w./-]+", cmd_no_msg) if ("/" in t or "." in t)]
    handoff_edit = tool in ("Edit", "Write") and "/handoff/" in path
    handoff_commit = (
        tool == "Bash"
        and only_add_commit
        and "handoff/" in cmd
        and all(t.startswith("handoff/") for t in paths)
    )
    if handoff_edit or handoff_commit:
        sys.exit(0)
    if not LEAD:
        block(
            f"Context cap reached ({used} > {CAP} tokens). "
            "Stop work now: update your hand-off file under handoff/, "
            "commit only that file, then give your final reply. Only hand"
            "-off edits and its commit are allowed."
        )
    spawn = (
        tool == "Bash"
        and re.fullmatch(
            "\\s*(?:cd\\s+\\S+\\s*(?:&&|;)\\s*)?(?:env\\s+)?(?:\\w+=\\S*\\s+)*[\"']"
            "?(?:/tmp/factory-fp-0925/swarm/bin|\\$\\{?BIN\\}?)/"
            "(?:lead-spawn|planner-spawn|interior-lead|interior-p2-lead|i"
            "nterior-p25-lead|interior-p3-lead)\\.sh[\"']?(?:\\s+-{1,2}[\\w-]"
            "+)*\\s*",
            cmd,
        )
        is not None
    )
    if spawn:
        sys.exit(0)
    if used > CAP + RESERVE:
        block(
            f"Context cap and its {RESERVE}-token hand-off reserve are spent "
            f"({used} > {CAP + RESERVE} tokens). "
            "Only the spawn script you were started with runs now: run it"
            " to open the next session and end this one."
        )
    segments = [
        s.strip()
        for s in re.split(r"\s*(?:&&|\|\||;|\||\n)\s*", HEREDOC.sub("", cmd_no_msg))
        if s.strip()
    ]
    handoff_shell = re.compile(
        "cd\\s+\\S+|echo\\b.*|cat\\s+[^|<>]+|head\\b[^|<>]*|tail\\s+(?!-[a-"
        "zA-Z]*f)[^|<>]*|sed\\s+-n\\b[^|<>]*|grep\\b[^|<>]*|wc\\b[^|<>]*|"
        "ls\\b[^|<>]*"
        "|shasum\\b[^|<>]*|git\\s+(?:-C\\s+\\S+\\s+)?(?:add|commit|status|"
        "log|diff|show|rev-parse)\\b[^|<>]*"
    )
    allowed = (
        tool in ("Read", "Grep", "Glob")
        or (
            tool in ("Edit", "Write")
            and bool(
                re.search(r"prompts/phase-[0-9.]+/progress\.md$", path)
                or re.search(r"/handoff/[^/]+\.md$", path)
                or ("/research/notes/" in path and path.endswith("-handoff.md"))
            )
        )
        or (tool == "Bash" and bool(segments) and all(handoff_shell.fullmatch(s) for s in segments))
    )
    if allowed:
        sys.exit(0)
    block(
        f"Context cap reached ({used} > {CAP} tokens). "
        f"The guard holds {CAP + RESERVE - used} of a {RESERVE}-token reserve "
        "for the hand-off and the successor only. Dispatch nothing ne"
        "w. Allowed now: read files, edit your progress.md or "
        "hand-off note, read-only shell (cat, grep, sed -n, ls, git s"
        "tatus/log/diff), git add and commit, "
        "`cat /tmp/factory-fp-0925/swarm/fp3d/lead-session-<your clau"
        "de pid>.txt`, then the spawn script you were started with "
        "(lead-spawn.sh, planner-spawn.sh, interior-lead.sh, interior"
        "-p2-lead.sh, interior-p25-lead.sh or interior-p3-lead.sh "
        "in /tmp/factory-fp-0925/swarm/bin)."
    )
