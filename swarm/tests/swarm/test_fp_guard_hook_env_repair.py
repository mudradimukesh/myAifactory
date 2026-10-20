import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TypedDict

import pytest
from test_fp_guard_hook_env import hook_run as hook_run

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/swarm"


class Result(TypedDict):
    returncode: int
    stdout: str
    stderr: str


class Invoke(Protocol):
    def __call__(
        self,
        code: str,
        event: dict[str, object] | None = None,
        env: dict[str, str] | None = None,
        parent: subprocess.Popen[str] | None = None,
    ) -> Result: ...


class Worker(TypedDict):
    pid: int
    procStart: str
    replPid: int
    replProcStart: str
    sessionId: str


class Roster(TypedDict):
    workers: dict[str, Worker]


class HookRun(TypedDict):
    invoke: Invoke
    start: Callable[[str, str, dict[str, str]], tuple[subprocess.Popen[str], dict[str, int]]]
    lead: subprocess.Popen[str]
    env: dict[str, str]
    tmp_path: Path
    continued: Path
    roster: Roster
    roster_path: Path


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


def test_gh_2_continuation_reads_only_required_prefix(hook_run: HookRun) -> None:
    hook_run["roster"]["workers"]["session"]["sessionId"] = "continued"
    hook_run["roster_path"].write_text(json.dumps(hook_run["roster"]))
    with hook_run["continued"].open("a") as transcript:
        transcript.write("unparsed suffix\n")
    result = hook_run["invoke"](
        "import json; print(json.dumps(run_state.originating_lead("
        "json.loads(run_state.DEFAULT_RUN.read_text()))))"
    )
    assert result["returncode"] == 0, result
    assert json.loads(result["stdout"])["model_session_id"] == "original"
