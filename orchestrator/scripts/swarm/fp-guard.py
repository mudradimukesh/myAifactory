"""PreToolUse guard for floorplan-render builder agents (orchestrator/GUIDE.md).

Blocks waiting and polling. Past the context cap a builder may only hand off; a lead gets a reserve of 10% of its
cap for its hand-off and successor only, and past the reserve only the spawn script.
Exit 2 blocks the tool call and shows stderr to the agent.
"""

import json
import os
import re
import sys

CAP_BASE = {"claude-opus-5-5": 166000, "claude-sonnet-5": 166000}
CAP_BASE_DEFAULT = 166000
FRACTION = float(os.environ.get("FP_CAP_FRACTION", "0.90"))
CAP_OVERRIDE = (
    int(os.environ["FP_CONTEXT_CAP"]) if os.environ.get("FP_CONTEXT_CAP") else None
)
import subprocess

from swarm_config import ART


def parent_is_headless():
    try:
        cmd = subprocess.run(
            ["ps", "-o", "command=", "-p", str(os.getppid())],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    argv = cmd.split(" -- ", 1)[0]
    return (
        re.search(r"(^|/)claude(\s|$)", argv) is not None
        and re.search(r"\s(-p|--print)(\s|$)", argv) is not None
    )


LEAD = os.environ.get("FP_LEAD") == "1" and not parent_is_headless()
POLL = re.compile(
    r"\bsleep\b|\buntil\b|\bwhile\s+(true|:)|\bwatch\b|\binotifywait\b|tail\s+-[a-zA-Z]*f|\bwait\b\s*($|;|&&)"
)
# Heuristic, not a shell parser: catches the common spellings of push, discard and delete. Global options
# such as `-C <dir>` or `-c k=v` may sit between `git` and the subcommand; flags may come in any order.
_G = r"\bgit(?:\s+-[cC]\s*\S+|\s+--\S+)*\s+"
GIT_DANGER = re.compile(
    _G + r"(?:push\b"
    r"|reset\b[^|;&]*--hard"
    r"|clean\b[^|;&]*(?:-[a-zA-Z]*f|--force)"
    r"|branch\b[^|;&]*(?:-[a-zA-Z]*D\b|--delete[^|;&]*--force|--force[^|;&]*--delete|-d[^|;&]*--force|--force[^|;&]*-d\b)"
    r"|checkout\s+(?:\.|--\s|[^-\s][^|;&]*\s--\s)"
    r"|restore\b(?![^|;&]*--staged\b)"
    r"|worktree\s+remove\b"
    r"|stash\s+(?:drop|clear)\b)"
)
HEREDOC = re.compile(
    r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^\s*\2\s*$", re.DOTALL | re.MULTILINE
)
SHELL_BG = re.compile(
    r"(?<!&)&(?!&)\s*($|;|\n)|\bnohup\b|\bdisown\b"
)  # a trailing single & backgrounds the job

event = json.load(sys.stdin)
tool, args = event.get("tool_name", ""), event.get("tool_input") or {}
if LEAD and event.get("session_id"):
    # Record the lead's session id, keyed by the Claude process (this hook's parent), so the lead can quote it in its hand-off.
    try:
        ART.mkdir(parents=True, exist_ok=True)
        with open(ART / f"lead-session-{os.getppid()}.txt", "w") as f:
            f.write(event["session_id"] + "\n")
    except OSError:
        pass


def block(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def context():
    try:
        lines = (ART / event["transcript_path"]).read_text().splitlines()
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


def cap_for(model):
    if CAP_OVERRIDE:
        return CAP_OVERRIDE
    return int(CAP_BASE.get(model or "", CAP_BASE_DEFAULT) * FRACTION)


if os.environ.get("LEAD_AGENT") == "p1-pilot-lead":
    from pathlib import Path

    from pilot_guard import orchestrator_alert
    from run_state import DEFAULT_RUN

    try:
        run_path = Path(os.environ.get("PILOT_RUN", str(DEFAULT_RUN)))
        if orchestrator_alert(event, json.loads(run_path.read_text())):
            sys.exit(0)
    except (ValueError, OSError, KeyError):
        pass

if tool == "Bash":
    cmd = args.get("command", "")
    if args.get("run_in_background") and not LEAD:
        block(
            "Blocked: background jobs die when a -p turn ends. Run it in the foreground with a timeout up to 600000 ms."
        )
    if GIT_DANGER.search(cmd) and not LEAD:
        block(
            "Blocked: git push, reset --hard, clean -f, branch -D, checkout ./restore . and worktree remove are not available to builders. Commit on your branch; the lead merges."
        )
    if SHELL_BG.search(cmd) and not LEAD:
        block(
            "Blocked: shell background jobs (a trailing &, nohup, disown) die when a -p turn ends. Run it in the foreground."
        )
    if POLL.search(HEREDOC.sub("", cmd)):
        block(
            "Blocked: no sleeping, polling or waiting loops (orchestrator/GUIDE.md). Use event notifications or run the actual command in the foreground."
        )

used, model = context()
CAP = cap_for(model)
RESERVE = CAP // 10
if os.environ.get("LEAD_AGENT") == "p1-pilot-lead":
    from pathlib import Path

    from pilot_guard import check, record_lead
    from run_state import DEFAULT_RUN

    try:
        run_path = Path(os.environ.get("PILOT_RUN", str(DEFAULT_RUN)))
        record_lead(event, run_path)
        check(event, json.loads(run_path.read_text()), run_path, used, CAP)
    except (ValueError, OSError, KeyError) as error:
        block(f"Pilot guard: {error}")
    sys.exit(0)
if used > CAP:
    path = args.get("file_path", "")
    cmd = args.get("command", "")
    only_add_commit = (
        re.fullmatch(
            r"\s*git\s+(add|commit)\b[^|;&]*(\s*(&&|;)\s*git\s+(add|commit)\b[^|;&]*)*\s*",
            cmd,
        )
        is not None
    )
    cmd_no_msg = re.sub(
        r"""(-m|--message|-[a-z]*m)(\s+|=)("[^"]*"|'[^']*'|\S+)""", " ", cmd
    )
    paths = [
        t
        for t in re.findall(r"(?<!\S)(?!-)[\w./-]+", cmd_no_msg)
        if ("/" in t or "." in t)
    ]
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
            f"Context cap reached ({used} > {CAP} tokens). Stop work now: update your hand-off file under handoff/, "
            "commit only that file, then give your final reply. Only hand-off edits and its commit are allowed."
        )
    spawn = (
        tool == "Bash"
        and re.fullmatch(
            r"""\s*(?:cd\s+\S+\s*(?:&&|;)\s*)?(?:env\s+)?(?:\w+=\S*\s+)*["']?(?:${BIN}|\$\{?BIN\}?)/"""
            r"""(?:lead-spawn|planner-spawn|interior-lead|interior-p2-lead|interior-p25-lead|interior-p3-lead)\.sh["']?(?:\s+-{1,2}[\w-]+)*\s*""",
            cmd,
        )
        is not None
    )
    if spawn:
        sys.exit(0)
    if used > CAP + RESERVE:
        block(
            f"Context cap and its {RESERVE}-token hand-off reserve are spent ({used} > {CAP + RESERVE} tokens). "
            "Only the spawn script you were started with runs now: run it to open the next session and end this one."
        )
    segments = [
        s.strip()
        for s in re.split(r"\s*(?:&&|\|\||;|\||\n)\s*", HEREDOC.sub("", cmd_no_msg))
        if s.strip()
    ]
    handoff_shell = re.compile(
        r"cd\s+\S+|echo\b.*|cat\s+[^|<>]+|head\b[^|<>]*|tail\s+(?!-[a-zA-Z]*f)[^|<>]*|sed\s+-n\b[^|<>]*|grep\b[^|<>]*|wc\b[^|<>]*|ls\b[^|<>]*"
        r"|shasum\b[^|<>]*|git\s+(?:-C\s+\S+\s+)?(?:add|commit|status|log|diff|show|rev-parse)\b[^|<>]*"
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
        or (
            tool == "Bash"
            and bool(segments)
            and all(handoff_shell.fullmatch(s) for s in segments)
        )
    )
    if allowed:
        sys.exit(0)
    block(
        f"Context cap reached ({used} > {CAP} tokens). The guard holds {CAP + RESERVE - used} of a {RESERVE}-token reserve "
        "for the hand-off and the successor only. Dispatch nothing new. Allowed now: read files, edit your progress.md or "
        "hand-off note, read-only shell (cat, grep, sed -n, ls, git status/log/diff), git add and commit, "
        "`cat $ART/lead-session-<your claude pid>.txt`, then the spawn script you were started with "
        "(lead-spawn.sh, planner-spawn.sh, interior-lead.sh, interior-p2-lead.sh, interior-p25-lead.sh or interior-p3-lead.sh "
        "in ${BIN})."
    )
