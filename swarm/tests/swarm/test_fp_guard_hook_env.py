"""GH regressions use real macOS processes, launch environments and transcripts."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict

import pytest

if TYPE_CHECKING:
    from run_state import RunRecord, RunState
    from test_fp_guard_hook_env_repair import Invoke, Result

    class Guard(Protocol):
        def __call__(
            self,
            session: str = "original",
            command: str | None = None,
            env: dict[str, str] | None = None,
            parent: subprocess.Popen[str] | None = None,
        ) -> Result: ...

    class Roster(TypedDict):
        proto: int
        workers: dict[str, dict[str, int | str]]

    class HookRun(TypedDict):
        invoke: Invoke
        start: Callable[[str, str, dict[str, str]], tuple[subprocess.Popen[str], dict[str, int]]]
        lead: subprocess.Popen[str]
        env: dict[str, str]
        tmp_path: Path
        continued: Path
        roster: Roster
        roster_path: Path
        guard: Guard
        state: RunState
        path: Path
        home: Path
        original: Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/swarm"
KEYS = ("FP_LEAD", "LEAD_AGENT", "FP_CAP_FRACTION", "FP_CONTEXT_CAP", "PILOT_RUN", "FP_LEAD_SPAWN")
HELPER = """import json, os, subprocess, sys
if sys.argv[1] in {"host", "headless"}:
    name = "claude bg-spare" if sys.argv[1] == "host" else "job-shell"
    child = subprocess.Popen(["/bin/zsh", "-c", "exec -a " + __import__("shlex").quote(name)
        + " " + __import__("shlex").join([sys.executable, __file__, "worker"])], env={})
    try:
        child.wait()
    finally:
        child.terminate()
else:
    print(json.dumps({"pid": os.getpid(), "ppid": os.getppid()}), flush=True)
    for line in sys.stdin:
        task = json.loads(line)
        result = subprocess.run([sys.executable, "-c", task["code"]],
            input=task.get("input", ""), text=True, capture_output=True, env=task.get("env", {}))
        print(json.dumps({"returncode": result.returncode, "stdout": result.stdout,
            "stderr": result.stderr}), flush=True)
"""


@pytest.fixture
def hook_run(tmp_path: Path) -> Iterator[HookRun]:
    helper = tmp_path / "helper.py"
    helper.write_text(HELPER)
    path = tmp_path / "run.json"
    home = tmp_path / "home"
    (home / ".claude/daemon").mkdir(parents=True)
    original = tmp_path / "original.jsonl"
    continued = tmp_path / "continued.jsonl"
    row = {
        "type": "assistant",
        "uuid": "message-1",
        "sessionId": "original",
        "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 196059}},
    }
    original.write_text(json.dumps(row) + "\n")
    continued.write_text(
        json.dumps({**row, "sessionId": "continued"})
        + "\n"
        + json.dumps({"type": "user", "uuid": "message-2", "sessionId": "continued", "message": {}})
        + "\n"
    )
    launch_env = dict(
        zip(
            KEYS,
            ("1", "p1-pilot-lead", "0.753012048192771", "180000", str(path), "launch-token"),
            strict=True,
        )
    )
    children: list[subprocess.Popen[str]] = []

    def start(
        name: str, mode: str, env: dict[str, str]
    ) -> tuple[subprocess.Popen[str], dict[str, int]]:
        child = subprocess.Popen(
            [
                "/bin/zsh",
                "-c",
                "exec -a "
                + shlex.quote(name)
                + " "
                + shlex.join([sys.executable, str(helper), mode]),
            ],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        children.append(child)
        assert child.stdout is not None
        ready = json.loads(child.stdout.readline())
        return child, ready

    lead, lead_ready = start("claude --session-id original", "worker", launch_env)
    daemon, spare = start("claude bg-pty-host", "host", {})
    table_code = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); "
        "import run_state; import json; print(json.dumps(run_state.processes()))"
    )
    table = json.loads(
        subprocess.check_output([sys.executable, "-c", table_code], env={}, text=True)
    )
    active: RunRecord = table[str(lead_ready["pid"])]
    active["model_session_id"] = "original"
    active["transcript_path"] = str(original)
    state: RunState = {
        "lifecycle": "running",
        "active_lead": active,
        "blockers": [],
        "deadlines": {},
        "dispatch_frozen": False,
        "handoff": {"token": "token"},
        "consumed": {"overall": 0, "preparation": 0},
        "ceilings": {"overall": 1000000, "preparation": 1000000},
        "reservations": {},
        "inventory": {
            "sessions": [
                {"role": "lead", "session_id": "original", "transcript_path": str(original)}
            ]
        },
    }
    path.write_text(json.dumps(state))
    roster: Roster = {
        "proto": 1,
        "workers": {
            "session": {
                "pid": spare["ppid"],
                "procStart": table[str(spare["ppid"])]["started_at"],
                "replPid": spare["pid"],
                "replProcStart": table[str(spare["pid"])]["started_at"],
                "sessionId": "original",
                "cwd": str(tmp_path),
            }
        },
    }
    roster_path = home / ".claude/daemon/roster.json"
    roster_path.write_text(json.dumps(roster))
    bootstrap = (
        f"import sys, pathlib; sys.path.insert(0, {str(SCRIPTS)!r}); import run_state; "
        f"run_state.DEFAULT_RUN = pathlib.Path({str(path)!r}); "
        f"pathlib.Path.home = classmethod(lambda cls: pathlib.Path({str(home)!r})); "
    )

    def invoke(
        code: str,
        event: dict[str, object] | None = None,
        env: dict[str, str] | None = None,
        parent: subprocess.Popen[str] | None = None,
    ) -> Result:
        parent = parent if parent is not None else daemon
        assert parent.stdin is not None and parent.stdout is not None
        task = {
            "code": bootstrap + code,
            "input": json.dumps(event) if event else "",
            "env": env or {},
        }
        parent.stdin.write(json.dumps(task) + "\n")
        parent.stdin.flush()
        result: Result = json.loads(parent.stdout.readline())
        return result

    def guard(
        session: str = "original",
        command: str | None = None,
        env: dict[str, str] | None = None,
        parent: subprocess.Popen[str] | None = None,
    ) -> Result:
        event: dict[str, object] = {
            "session_id": session,
            "transcript_path": str(tmp_path / (session + ".jsonl")),
            "tool_name": "Bash",
            "tool_input": {
                "command": command
                or "/tmp/factory-fp-0925/swarm/bin/interior-lead.sh --handoff-begin"
            },
        }
        code = (
            "import runpy, os, json\ntry:\n"
            f" runpy.run_path({str(SCRIPTS / 'fp-guard.py')!r}, run_name='__main__')\n"
            f"finally:\n keys={KEYS!r}\n"
            " print('HOOK_ENV=' + json.dumps({k: os.environ.get(k) for k in keys}), "
            "file=sys.stderr)\n"
        )
        return invoke(code, event, env, parent)

    try:
        yield {
            "guard": guard,
            "invoke": invoke,
            "start": start,
            "lead": lead,
            "state": state,
            "path": path,
            "home": home,
            "roster": roster,
            "roster_path": roster_path,
            "env": launch_env,
            "original": original,
            "continued": continued,
            "tmp_path": tmp_path,
        }
    finally:
        for child in children:
            assert child.stdin is not None
            child.stdin.close()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)
            assert child.stdout is not None and child.stderr is not None
            child.stdout.close()
            child.stderr.close()


@pytest.mark.parametrize("target", ["pilot", "generic"])
def test_gh_0_disables_slash_commands_in_both_built_launches(
    hook_run: HookRun, target: str
) -> None:
    result = hook_run["guard"]("unrelated", "git status")
    assert result["returncode"] == 0, result
    brief = hook_run["tmp_path"] / "brief.md"
    brief.write_text("Fixture brief")
    code = f"""import pilot_lead, json
class Captured(Exception):
    pass
def capture(command, **kwargs):
    print(json.dumps(command))
    raise Captured
pilot_lead.subprocess.check_output = capture
try:
    pilot_lead.launch(run_state.DEFAULT_RUN,
        {{"REPO": {str(hook_run["tmp_path"])!r}, "BIN": {str(SCRIPTS)!r},
          "LEAD_BRIEF": {str(brief)!r}}}, "token")
except Captured:
    pass
"""
    result = hook_run["invoke"](code)
    assert result["returncode"] == 0, result
    pilot = shlex.split(json.loads(result["stdout"])[-1])
    generic = subprocess.run(
        ["/bin/zsh", str(SCRIPTS / "lead-spawn.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        env={},
    )
    assert generic.returncode == 0, generic.stderr
    built = pilot if target == "pilot" else shlex.split(generic.stdout)
    assert "--disable-slash-commands" in built, built


@pytest.mark.parametrize("session", ["original", "continued"])
@pytest.mark.parametrize("command", ["--handoff-begin", "--handoff-start token"])
def test_gh_1_recovers_only_missing_launch_keys(
    hook_run: HookRun, session: str, command: str
) -> None:
    hook_run["roster"]["workers"]["session"]["sessionId"] = session
    hook_run["roster_path"].write_text(json.dumps(hook_run["roster"]))
    command = "/tmp/factory-fp-0925/swarm/bin/interior-lead.sh " + command
    result = hook_run["guard"](session, command)
    assert result["returncode"] == 0, result
    recovered = json.loads(result["stderr"].split("HOOK_ENV=")[1])
    assert recovered == hook_run["env"]
    for key in KEYS:
        override = {
            "FP_LEAD": "0",
            "LEAD_AGENT": "existing-agent",
            "FP_CAP_FRACTION": "0.50",
            "FP_CONTEXT_CAP": "170000",
            "PILOT_RUN": str(hook_run["path"]),
            "FP_LEAD_SPAWN": "existing-token",
        }[key]
        result = hook_run["guard"](session, "git status", env={key: override})
        seen = json.loads(result["stderr"].split("HOOK_ENV=")[1])
        assert seen[key] == override


@pytest.mark.parametrize("session", ["original", "continued"])
def test_gh_2_daemon_ancestry_requires_live_session_evidence(
    hook_run: HookRun, session: str
) -> None:
    worker = hook_run["roster"]["workers"]["session"]
    worker["sessionId"] = session
    hook_run["roster_path"].write_text(json.dumps(hook_run["roster"]))
    code = (
        "import json; state=json.loads(run_state.DEFAULT_RUN.read_text()); "
        "print(json.dumps(run_state.originating_lead(state)))"
    )
    result = hook_run["invoke"](code)
    assert result["returncode"] == 0, result
    proven = json.loads(result["stdout"])
    active = hook_run["state"]["active_lead"]
    assert active is not None
    assert proven["pid"] == active["pid"]
    assert proven["model_session_id"] == "original"
    invalid_fields: list[tuple[str, int | str]] = [
        ("sessionId", "unrelated"),
        ("replPid", os.getpid()),
        ("replProcStart", "stale"),
        ("procStart", "stale"),
    ]
    for key, bad in invalid_fields:
        saved = worker[key]
        worker[key] = bad
        hook_run["roster_path"].write_text(json.dumps(hook_run["roster"]))
        refused = hook_run["invoke"](code)
        assert refused["returncode"] != 0, refused
        assert "Caller is not a descendant" in refused["stderr"]
        worker[key] = saved
    hook_run["roster_path"].unlink()
    assert hook_run["invoke"](code)["returncode"] != 0


@pytest.mark.parametrize("failure", ["unmatched", "unreadable-env", "bad-prefix", "headless"])
def test_gh_3_preserves_non_lead_and_headless_limits(hook_run: HookRun, failure: str) -> None:
    session = "original"
    parent = None
    if failure == "unmatched":
        session = "unrelated"
    elif failure == "unreadable-env":
        active = hook_run["state"]["active_lead"]
        assert active is not None
        active["pid"] = 2147483647
        hook_run["path"].write_text(json.dumps(hook_run["state"]))
    elif failure == "bad-prefix":
        session = "continued"
        hook_run["continued"].write_text(json.dumps({"sessionId": session, "uuid": "other"}) + "\n")
    else:
        parent, _ = hook_run["start"]("claude -p", "headless", hook_run["env"])
    result = hook_run["guard"](session, "git push origin HEAD", parent=parent)
    assert result["returncode"] == 2, result
    assert "not available to builders" in result["stderr"], result
    if failure != "headless":
        seen = json.loads(result["stderr"].split("HOOK_ENV=")[1])
        assert all(value is None for value in seen.values())


def test_gh_pres_keeps_direct_lead_ancestry(hook_run: HookRun) -> None:
    result = hook_run["invoke"](
        "import json; print(json.dumps(run_state.originating_lead("
        "json.loads(run_state.DEFAULT_RUN.read_text()))))",
        parent=hook_run["lead"],
    )
    assert result["returncode"] == 0, result
    assert json.loads(result["stdout"])["model_session_id"] == "original"


def test_gh_3_inherited_pilot_headless_descendant_refuses_write(hook_run: HookRun) -> None:
    transcript = hook_run["tmp_path"] / "headless.jsonl"
    transcript.write_text(json.dumps({"sessionId": "headless", "message": {}}) + "\n")
    event: dict[str, object] = {
        "session_id": "headless",
        "transcript_path": str(transcript),
        "tool_name": "Write",
        "tool_input": {"file_path": str(hook_run["tmp_path"] / "output.txt"), "content": "write"},
    }
    guard_code = (
        f"import sys, runpy; sys.path.insert(0, {str(SCRIPTS)!r}); "
        f"runpy.run_path({str(SCRIPTS / 'fp-guard.py')!r}, run_name='__main__')"
    )
    command = "exec -a 'claude -p' " + shlex.join(
        [sys.executable, str(hook_run["tmp_path"] / "helper.py"), "headless"]
    )
    result = hook_run["invoke"](
        "import subprocess, json, sys\n"
        f"child = subprocess.Popen(['/bin/zsh', '-c', {command!r}], "
        "stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)\n"
        "try:\n"
        " json.loads(child.stdout.readline())\n"
        f" event = {event!r}\n"
        f" task = {{'code': {guard_code!r}, 'env': {hook_run['env']!r}}}\n"
        " for tool, args in [('Bash', {'command': 'git push origin HEAD'}), "
        "('Write', event['tool_input'])]:\n"
        "  event.update(tool_name=tool, tool_input=args)\n"
        "  task['input'] = json.dumps(event)\n"
        "  child.stdin.write(json.dumps(task) + '\\n'); child.stdin.flush()\n"
        "  print(child.stdout.readline(), end='')\n"
        "finally:\n"
        " child.stdin.close(); child.wait(timeout=5)\n",
        env=hook_run["env"],
        parent=hook_run["lead"],
    )
    assert result["returncode"] == 0, result
    bash, write = [json.loads(line) for line in result["stdout"].splitlines()]
    assert bash["returncode"] == 2, bash
    assert "not available to builders" in bash["stderr"], bash
    assert write["returncode"] == 2, write
    assert "Tool event session is not the active lead" in write["stderr"], write


@pytest.mark.parametrize("failure", ["bad-prefix", "worker-pid", "wrong-host"])
def test_gh_2_refuses_invalid_daemon_proof(hook_run: HookRun, failure: str) -> None:
    worker = hook_run["roster"]["workers"]["session"]
    parent = None
    if failure == "bad-prefix":
        worker["sessionId"] = "continued"
        hook_run["continued"].write_text(
            json.dumps({"sessionId": "continued", "uuid": "different-prefix"}) + "\n"
        )
    elif failure == "worker-pid":
        worker["pid"] = os.getpid()
    else:
        parent, ready = hook_run["start"]("claude other-host", "host", {})
        result = hook_run["invoke"](
            "import json; print(json.dumps(run_state.processes()))", parent=parent
        )
        assert result["returncode"] == 0, result
        table = json.loads(result["stdout"])
        worker["pid"] = ready["ppid"]
        worker["procStart"] = table[str(ready["ppid"])]["started_at"]
        worker["replPid"] = ready["pid"]
        worker["replProcStart"] = table[str(ready["pid"])]["started_at"]
    hook_run["roster_path"].write_text(json.dumps(hook_run["roster"]))
    code = (
        "import json; print(json.dumps(run_state.originating_lead("
        "json.loads(run_state.DEFAULT_RUN.read_text()))))"
    )
    result = hook_run["invoke"](code, parent=parent) if parent else hook_run["invoke"](code)
    assert result["returncode"] != 0, result
    assert "Caller is not a descendant of the active lead" in result["stderr"], result


@pytest.mark.parametrize("failure", ["shorter", "different-first"])
def test_gh_2_continuation_rejects_before_parsing_rest(hook_run: HookRun, failure: str) -> None:
    original = hook_run["tmp_path"] / "original.jsonl"
    with original.open("a") as history:
        history.write("unparsed original row\n")
    first = json.loads(original.read_text().splitlines()[0])
    first["sessionId"] = "continued"
    if failure == "different-first":
        first["uuid"] = "different-prefix"
    hook_run["continued"].write_text(
        json.dumps(first)
        + "\n"
        + ("unparsed continued row\n" if failure == "different-first" else "")
    )
    result = hook_run["invoke"](
        "import json; state=json.loads(run_state.DEFAULT_RUN.read_text()); "
        "original_loads=json.loads; parsed=[]\n"
        "def track(line):\n"
        " parsed.append(line); return original_loads(line)\n"
        "run_state.json.loads=track\n"
        f"assert not run_state.continues_lead_session(state, 'continued', "
        f"pathlib.Path({str(hook_run['continued'])!r}))\n"
        "print(len(parsed))"
    )
    assert result["returncode"] == 0, result
    assert int(result["stdout"]) == (0 if failure == "shorter" else 2), result


@pytest.mark.parametrize(
    ("settings", "model", "cap"),
    [
        ({}, "claude-opus-5-5", 149400),
        ({}, "unknown-model", 149400),
        ({"FP_CAP_FRACTION": "0.50"}, "claude-sonnet-5", 83000),
        ({"FP_CONTEXT_CAP": "180000", "FP_CAP_FRACTION": "0.50"}, "unknown-model", 180000),
        ({"FP_CONTEXT_CAP": "95000"}, "claude-opus-5-5", 95000),
        ({"FP_CONTEXT_CAP": "", "FP_CAP_FRACTION": "0.50"}, "unknown-model", 83000),
        ({"FP_CONTEXT_CAP": "0", "FP_CAP_FRACTION": "0.50"}, "unknown-model", 83000),
    ],
)
def test_bin_context_cap_boundary(
    tmp_path: Path, settings: dict[str, str], model: str, cap: int
) -> None:
    transcript = tmp_path / "builder.jsonl"
    event = {
        "session_id": "builder",
        "transcript_path": str(transcript),
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "output.txt"), "content": "work"},
    }
    for used in (cap, cap + 1):
        transcript.write_text(
            json.dumps({"message": {"usage": {"input_tokens": cap + 1000}}})
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "model": model,
                        "usage": {
                            "input_tokens": used - 30,
                            "cache_read_input_tokens": 20,
                            "cache_creation_input_tokens": 10,
                        },
                    }
                }
            )
            + '\ninvalid json\n{"message": null}\n{"message": {}}\n'
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "fp-guard.py")],
            input=json.dumps(event),
            env={"FP_LEAD": "0", "LEAD_AGENT": "builder", **settings},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == (0 if used == cap else 2), result.stderr
        if used > cap:
            assert f"Context cap reached ({used} > {cap} tokens)." in result.stderr
            assert "Only hand-off edits and its commit are allowed." in result.stderr


@pytest.mark.parametrize("failure", ["missing-path", "unreadable-path", "no-usage"])
def test_bin_context_without_usage_allows_builder(tmp_path: Path, failure: str) -> None:
    event: dict[str, object] = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "output.txt"), "content": "work"},
    }
    transcript = tmp_path / "builder.jsonl"
    if failure != "missing-path":
        event["transcript_path"] = str(transcript)
    if failure == "no-usage":
        transcript.write_text('invalid json\n{"message": {}}\n')
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "fp-guard.py")],
        input=json.dumps(event),
        env={"FP_LEAD": "0", "LEAD_AGENT": "builder", "FP_CONTEXT_CAP": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("override", [None, "180000", "95000"])
def test_bin_pilot_context_override_preserves_branch_default(
    hook_run: HookRun, override: str | None
) -> None:
    env = {key: value for key, value in hook_run["env"].items() if key != "FP_CONTEXT_CAP"}
    env["FP_LEAD"] = "0"
    if override is not None:
        env["FP_CONTEXT_CAP"] = override
    event: dict[str, object] = {
        "session_id": "original",
        "transcript_path": str(hook_run["original"]),
        "tool_name": "Write",
        "tool_input": {"file_path": str(hook_run["tmp_path"] / "output.txt"), "content": "work"},
    }
    for used in (90000, 120000, 120001) if override is None else (120000,):
        hook_run["original"].write_text(
            json.dumps({"message": {"usage": {"input_tokens": used}}}) + "\n"
        )
        result = hook_run["invoke"](
            f"import runpy; runpy.run_path({str(SCRIPTS / 'fp-guard.py')!r}, run_name='__main__')",
            event,
            env,
            hook_run["lead"],
        )
        denied = override == "95000" or used == 120001
        assert result["returncode"] == (2 if denied else 0), result
        if denied:
            assert "Context cap permits handoff only" in result["stderr"], result


@pytest.mark.parametrize("orchestrator_pane", [7, 8])
def test_bin_inherited_pilot_alert_precedes_poll_guard(
    tmp_path: Path, orchestrator_pane: int
) -> None:
    run = tmp_path / "run.json"
    run.write_text(
        json.dumps(
            {
                "active_lead": {"model_session_id": "original"},
                "inventory": {"threads": [{"role": "orchestrator", "pane_id": orchestrator_pane}]},
            }
        )
    )
    event = {
        "session_id": "original",
        "tool_name": "Bash",
        "tool_input": {"command": "wezterm cli send-text --pane-id 7 --no-paste wait"},
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "fp-guard.py")],
        input=json.dumps(event),
        env={"FP_LEAD": "0", "LEAD_AGENT": "p1-pilot-lead", "PILOT_RUN": str(run)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (0 if orchestrator_pane == 7 else 2), result.stderr
    if orchestrator_pane != 7:
        assert "Blocked: no sleeping, polling or waiting loops" in result.stderr


def test_bin_inherited_pilot_unreadable_state_blocks(tmp_path: Path) -> None:
    run = tmp_path / "missing.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "fp-guard.py")],
        input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": "output.txt"}}),
        env={"FP_LEAD": "0", "LEAD_AGENT": "p1-pilot-lead", "PILOT_RUN": str(run)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2, result.stderr
    assert "Pilot guard:" in result.stderr
    assert str(run) in result.stderr
