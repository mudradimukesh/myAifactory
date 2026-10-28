import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAKE_CLAUDE = """import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
Path('claude-argv.json').write_text(json.dumps(args))
Path('claude-env.json').write_text(json.dumps(dict(os.environ)))
flag = '--resume' if '--resume' in args else '--session-id'
session = args[args.index(flag) + 1]
mode = os.environ.get('FAKE_MODE', 'ok')
transcript = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects' / '-work' / (session + '.jsonl')
transcript.parent.mkdir(parents=True, exist_ok=True)
count = len(transcript.read_text().splitlines()) if transcript.exists() else 0
usage = {
    'input_tokens': 10,
    'cache_creation_input_tokens': 4,
    'output_tokens': 6,
    'cache_read_input_tokens': 7,
}
if mode in ('budget', 'budget-truncated', 'drift-budget') or (
    mode == 'handoff-budget' and '--resume' in args
):
    usage['output_tokens'] = 500
if mode == 'context':
    usage['cache_read_input_tokens'] = 5000
if mode == 'context-sum':
    usage.update(input_tokens=400, cache_creation_input_tokens=400, cache_read_input_tokens=400)
drifted = mode in ('drift', 'drift-slow', 'drift-budget')
model = 'claude-sonnet-5-5' if drifted else 'claude-opus-5-5'
message = {'id': 'msg-' + str(count), 'model': model, 'usage': usage}
partial = {**message, 'usage': {**usage, 'output_tokens': 1}}
real_synthetic = {
    'output_tokens_details': None,
    'input_tokens': 0,
    'output_tokens': 0,
    'cache_creation_input_tokens': 0,
    'cache_read_input_tokens': 0,
    'server_tool_use': {'web_search_requests': 0, 'web_fetch_requests': 0},
    'service_tier': None,
    'cache_creation': {'ephemeral_1h_input_tokens': 0, 'ephemeral_5m_input_tokens': 0},
    'speed': None,
}
synthetic_usage = {
    'synthetic-usage': {'input_tokens': 9, 'output_tokens': 0},
    'synthetic-float': {'input_tokens': 0.0, 'output_tokens': 0},
    'synthetic-missing': None,
    'synthetic-nested': {**real_synthetic, 'server_tool_use': {'web_search_requests': 1}},
    'synthetic-no-counters': {'service_tier': None},
    'synthetic-bool': {**real_synthetic, 'server_tool_use': {'web_search_requests': False}},
}.get(mode, real_synthetic)
synthetic = {'id': 'syn-' + str(count), 'model': '<synthetic>', 'usage': synthetic_usage}
rows = [
    {'type': 'user', 'message': {'role': 'user', 'content': 'brief'}},
    {'type': 'assistant', 'message': partial},
    {'type': 'assistant', 'message': message},
    {'type': 'assistant', 'message': synthetic},
]
with transcript.open('a') as output:
    output.write(''.join(json.dumps(row) + '\\n' for row in rows))
    if mode == 'budget-truncated':
        output.write(json.dumps({'type': 'assistant', 'message': message})[:40])
if mode == 'handoff' and '--resume' in args:
    import re

    target = re.search(r'write (\\S+) with', args[-1]).group(1)
    Path(target).write_text('handoff')
elif mode in (
    'budget',
    'budget-truncated',
    'drift-budget',
    'context',
    'context-sum',
    'handoff',
    'handoff-budget',
    'drift-slow',
):
    time.sleep(30)
error = mode == 'error'
print(json.dumps({
    'type': 'result',
    'subtype': 'error_max_turns' if error else 'success',
    'is_error': error,
    'result': 'builder done',
    'session_id': session,
}), flush=True)
"""


@pytest.fixture
def launch(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    source = tmp_path / "source/scripts/swarm"
    source.mkdir(parents=True)
    for name in ("codex_launch.py", "claude-builder-settings.json"):
        (source / name).write_text((ROOT / "scripts/swarm" / name).read_text())
    spec = importlib.util.spec_from_file_location("claude_codex_launch", source / "codex_launch.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    brief = (ROOT / "prompts/phase-1/builder-brief.md").read_text()

    def run(
        tag, mode="ok", target="claude-opus-5-5", assignment=None, extra=None, state=None, git=True
    ):
        work = tmp_path / tag
        work.mkdir()
        if git:
            subprocess.run(["git", "init", "-q", str(work)], check=True)
        (work / "prompts/phase-1").mkdir(parents=True)
        (work / "prompts/phase-1/builder-brief.md").write_text(brief)
        stub = work / "claude"
        stub.write_text("#!" + sys.executable + "\n" + FAKE_CLAUDE)
        stub.chmod(0o755)
        run_path = work / "run.json"
        selected = assignment or {
            "provider": "claude",
            "model": "claude-opus-5-5",
            "effort": "medium",
            "reason": "test",
        }
        run_path.write_text(
            json.dumps(
                state
                or {
                    "lifecycle": "preparing",
                    "blockers": [],
                    "deadlines": {},
                    "ceilings": {"overall": 100000, "preparation": 100000},
                    "consumed": {"overall": 0, "preparation": 0},
                    "active_lead": None,
                    "dispatch_frozen": False,
                    "reservations": {},
                    "inventory": {"threads": [], "sessions": []},
                    "assignments": {"a": selected},
                }
            )
        )
        message = work / "brief.txt"
        message.write_text("Implement the assigned task.")
        env = {
            "PATH": str(work) + os.pathsep + os.environ["PATH"],
            "PILOT_RUN": str(run_path),
            "ASSIGNMENT_ID": "a",
            "ALLOCATION_ID": "allocation",
            "ALLOWANCE": "100",
            "ROLE": "builder",
            "FEATURE": "fixture",
            "REPO": str(work),
            "CLAUDE_CONFIG_DIR": str(work / "claude-home"),
            "FAKE_MODE": mode,
            "FP_LEAD": "1",
            "LEAD_AGENT": "p1-pilot-lead",
            "PILOT_LEAD_OVERLAP": "1",
            "FP_LEAD_SPAWN": "token",
            "FP_CAP_FRACTION": "0.5",
            "START_S": "5",
            "IDLE_S": "5",
            "PILOT_TICK": "0.02",
            "PILOT_STOP_INT": "0.2",
            "PILOT_STOP_TERM": "0.2",
            "HANDOFF_RESERVE": "0",
            **(extra or {}),
        }
        for name in ("MODEL", "EFFORT", "OUTPUT_SCHEMA", "KEEP_ALLOCATION", "SANDBOX", "TRIGGER"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(sys, "argv", [str(source), str(work), tag, target, str(message)])
        result = worker.main()
        output = capsys.readouterr()
        receipt_path = work / (tag + "-launch.json")
        argv_path = work / "claude-argv.json"
        env_path = work / "claude-env.json"
        return {
            "code": result,
            "stdout": output.out,
            "stderr": output.err,
            "work": work,
            "receipt": json.loads(receipt_path.read_text()) if receipt_path.exists() else None,
            "state": json.loads(run_path.read_text()),
            "argv": json.loads(argv_path.read_text()) if argv_path.exists() else None,
            "env": json.loads(env_path.read_text()) if env_path.exists() else None,
            "reply": work / (tag + "-reply.txt"),
            "settings": source / "claude-builder-settings.json",
        }

    run.worker = worker
    return run


def test_claude_builder_launches_sandboxed_opus_session(launch):
    result = launch("new")
    argv = result["argv"]
    session = argv[argv.index("--session-id") + 1]
    common = subprocess.run(
        [
            "git",
            "-C",
            str(result["work"]),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert result["receipt"]["argv"][:2] == ["claude", "-p"]
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--settings") + 1] == str(result["settings"])
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--disable-slash-commands" in argv
    added = [argv[i + 1] for i, word in enumerate(argv) if word == "--add-dir"]
    assert added == [common, str(result["work"])]
    assert argv[-1].startswith("Implement the assigned task.")
    assert "RULING573j" in argv[-1]
    assert result["receipt"]["thread_id"] == session
    assert result["state"]["assignments"]["a"]["thread_id"] == session


def test_claude_builder_settings_enforce_sandbox(launch):
    settings = json.loads((ROOT / "scripts/swarm/claude-builder-settings.json").read_text())
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert sorted(settings["permissions"]["deny"]) == sorted(
        tool + "(/" + root + "/factory-fp-0925/swarm/bin/**)"
        for tool in ("Edit", "Write")
        for root in ("/tmp", "/private/tmp")
    )
    assert settings["permissions"]["allow"] == ["Read", "Glob", "Grep"]
    assert "hooks" not in settings


def test_claude_builder_does_not_inherit_lead_identity(launch):
    child = launch("env")["env"]
    for key in ("FP_LEAD", "LEAD_AGENT", "FP_LEAD_SPAWN", "PILOT_LEAD_OVERLAP", "FP_CAP_FRACTION"):
        assert key not in child
    assert child["ALLOCATION_ID"] == "allocation"


def test_claude_reply_counts_each_message_once_and_releases(launch):
    result = launch("reply")
    receipt = result["receipt"]
    assert result["code"] == 0
    assert receipt["status"] == "REPLY"
    assert result["reply"].read_text() == "builder done"
    assert receipt["usage_delta"] == {"new_tokens": 20, "cache_reads": 7}
    assert receipt["usage_status"] == "known"
    reservation = result["state"]["reservations"]["allocation"]
    assert reservation["observed"] == 20
    assert reservation["released"]


def test_claude_reply_keeps_allocation_when_requested(launch):
    result = launch("keep", extra={"KEEP_ALLOCATION": "1"})
    assert result["receipt"]["status"] == "REPLY"
    assert not result["state"]["reservations"]["allocation"].get("released")


def test_claude_resume_charges_only_new_usage(launch):
    first = launch("first", extra={"KEEP_ALLOCATION": "1"})
    session = first["receipt"]["thread_id"]
    state = first["state"]
    state["assignments"]["a"]["thread_id"] = session
    for record in state["reservations"].values():
        record["active_run"] = None
    work = first["work"]
    result = launch(
        "second",
        target="resume:" + session,
        state=state,
        extra={"CLAUDE_CONFIG_DIR": str(work / "claude-home"), "ALLOCATION_ID": "second"},
    )
    argv = result["argv"]
    assert argv[argv.index("--resume") + 1] == session
    assert "--session-id" not in argv
    assert result["receipt"]["usage_delta"] == {"new_tokens": 20, "cache_reads": 7}
    assert result["receipt"]["status"] == "REPLY"


def test_claude_resume_refuses_other_session(launch):
    result = launch("other", target="resume:not-the-assigned-session")
    assert result["code"] == 1
    assert "Resume thread does not match" in result["stderr"]
    assert result["argv"] is None


@pytest.mark.parametrize(
    ("assignment", "extra", "message"),
    [
        (
            {"provider": "claude", "model": "gpt-6-astra", "effort": "low", "reason": "t"},
            {},
            "does not match provider",
        ),
        (
            {"model": "claude-opus-5-5", "effort": "medium", "reason": "t"},
            {},
            "does not match provider",
        ),
        (
            {"provider": "gemini", "model": "claude-opus-5-5", "effort": "medium", "reason": "t"},
            {},
            "Unknown provider",
        ),
        (None, {"OUTPUT_SCHEMA": "/tmp/schema.json"}, "OUTPUT_SCHEMA"),
        (None, {"MODEL": "claude-sonnet-5-5"}, "MODEL does not match"),
        (None, {"EFFORT": "high"}, "EFFORT does not match"),
    ],
)
def test_claude_refuses_mismatched_assignment_before_launch(launch, assignment, extra, message):
    model = (assignment or {}).get("model", "claude-opus-5-5")
    result = launch("refuse", target=model, assignment=assignment, extra=extra)
    assert result["code"] == 1
    assert message in result["stderr"]
    assert result["argv"] is None
    assert result["state"]["reservations"] == {}


@pytest.mark.parametrize(
    ("mode", "extra", "reason"),
    [
        ("drift", {}, "MODEL_DRIFT"),
        ("synthetic-usage", {}, "MODEL_DRIFT"),
        ("synthetic-float", {}, "MODEL_DRIFT"),
        ("synthetic-missing", {}, "MODEL_DRIFT"),
        ("synthetic-nested", {}, "MODEL_DRIFT"),
        ("synthetic-no-counters", {}, "MODEL_DRIFT"),
        ("synthetic-bool", {}, "MODEL_DRIFT"),
        ("error", {}, "error_max_turns"),
    ],
)
def test_claude_drift_fails_with_unknown_usage(launch, mode, extra, reason):
    result = launch("stop-" + mode, mode=mode, extra=extra)
    receipt = result["receipt"]
    assert result["code"] == 1
    assert receipt["stop_reason"] == reason
    assert receipt["status"] == "FAIL"
    assert receipt["usage_status"] == "unknown"
    assert receipt["remaining_processes"] == []
    assert result["state"]["reservations"]["allocation"]["released"]


@pytest.mark.parametrize(
    ("mode", "extra", "reason"),
    [
        ("budget", {}, "BUDGET"),
        ("context", {"TRIGGER": "1000", "ALLOWANCE": "100000"}, "CONTEXT"),
        ("context-sum", {"TRIGGER": "1000", "ALLOWANCE": "100000"}, "CONTEXT"),
    ],
)
def test_clean_stop_records_final_transcript_usage(launch, mode, extra, reason):
    result = launch("clean-" + mode, mode=mode, extra=extra)
    receipt = result["receipt"]
    run = result["state"]["runs"]["clean-" + mode]
    assert result["code"] == 1
    assert receipt["stop_reason"] == reason
    assert receipt["status"] == "FAIL"
    assert receipt["remaining_processes"] == []
    assert receipt["usage_status"] == "known"
    assert run["usage_status"] == "known"
    assert run["complete"] is True
    assert receipt["usage_delta"] == {
        "new_tokens": run["new_tokens"],
        "cache_reads": run["cache_reads"],
    }
    assert result["state"]["reservations"]["allocation"]["released"]


@pytest.mark.parametrize("mode", ["budget-truncated", "drift-budget"])
def test_untrustworthy_final_transcript_stays_unknown(launch, mode):
    result = launch("untrusted-" + mode, mode=mode)
    receipt = result["receipt"]
    assert receipt["status"] == "FAIL"
    assert receipt["remaining_processes"] == []
    assert receipt["usage_status"] == "unknown"
    assert result["state"]["runs"]["untrusted-" + mode]["usage_status"] == "unknown"


def test_cleaned_lingering_descendant_records_known_usage(launch, monkeypatch):
    import owned_processes

    calls = []
    original = owned_processes.survivors

    def lingering_once(*args):
        calls.append(1)
        return [999999999] if len(calls) == 1 else original(*args)

    monkeypatch.setattr(owned_processes, "survivors", lingering_once)
    result = launch("lingering")
    receipt = result["receipt"]
    assert receipt["stop_reason"] == "SURVIVING_DESCENDANT"
    assert receipt["remaining_processes"] == []
    assert receipt["usage_status"] == "known"
    assert result["state"]["runs"]["lingering"]["usage_status"] == "known"


def test_error_result_behind_a_supervisor_stop_stays_unknown(launch, monkeypatch):
    import owned_processes

    calls = []
    original = owned_processes.survivors

    def lingering_once(*args):
        calls.append(1)
        return [999999999] if len(calls) == 1 else original(*args)

    monkeypatch.setattr(owned_processes, "survivors", lingering_once)
    result = launch("lingering-error", mode="error")
    receipt = result["receipt"]
    assert receipt["stop_reason"] == "SURVIVING_DESCENDANT"
    assert receipt["remaining_processes"] == []
    assert receipt["usage_status"] == "unknown"
    assert result["state"]["runs"]["lingering-error"]["usage_status"] == "unknown"


def test_claude_refuses_worktree_without_git_before_admission(launch):
    result = launch("no-git", git=False, extra={"GIT_CEILING_DIRECTORIES": str(Path("/"))})
    assert result["code"] == 1
    assert "no git directory" in result["stderr"]
    assert result["argv"] is None
    assert result["state"]["reservations"] == {}


def test_supervision_error_never_records_known_usage(launch, monkeypatch):
    def failing(path, offset):
        raise ValueError("supervision failed")

    monkeypatch.setattr(launch.worker, "claude_context", failing)
    result = launch("broken")
    receipt = result["receipt"]
    assert result["code"] == 1
    assert receipt["usage_delta"] == {"new_tokens": 20, "cache_reads": 7}
    assert receipt["usage_status"] == "unknown"
    assert receipt["remaining_processes"] == []
    assert result["state"]["reservations"]["allocation"]["released"]


def test_surviving_descendant_never_records_known_usage(launch, monkeypatch):
    import owned_processes

    original = owned_processes.stop

    def leaves_survivor(root, owned, *args, **kwargs):
        signals, _ = original(root, owned, *args, **kwargs)
        return signals, [root]

    monkeypatch.setattr(owned_processes, "stop", leaves_survivor)
    result = launch("survivor")
    receipt = result["receipt"]
    assert receipt["usage_delta"] == {"new_tokens": 20, "cache_reads": 7}
    assert receipt["usage_status"] == "unknown"
    assert receipt["status"] == "FAIL"
    assert not result["state"]["reservations"]["allocation"].get("released")


def test_claude_builder_hands_off_at_cutoff(launch):
    result = launch(
        "cutoff", mode="handoff", extra={"HANDOFF_RESERVE": "80", "KEEP_ALLOCATION": "1"}
    )
    receipt = result["receipt"]
    argv = receipt["handoff_argv"]
    handoff = result["work"] / "cutoff-handoff.md"
    assert result["code"] == 0
    assert "HANDOFF_CUTOFF cutoff" in result["stdout"]
    assert receipt["status"] == "HANDOFF"
    assert receipt["stop_reason"] is None
    assert argv[argv.index("--resume") + 1] == receipt["thread_id"]
    assert str(handoff) in argv[-1]
    assert receipt["handoff_path"] == str(handoff)
    assert handoff.read_text() == "handoff"
    assert receipt["usage_status"] == "known"
    assert receipt["usage_delta"] == {"new_tokens": 40, "cache_reads": 14}
    assert receipt["remaining_processes"] == []
    assert result["state"]["reservations"]["allocation"]["released"]


def test_claude_reviewer_never_hands_off(launch):
    result = launch(
        "reviewer-cutoff",
        extra={"HANDOFF_RESERVE": "90", "ROLE": "reviewer"},
    )
    receipt = result["receipt"]
    assert "handoff_argv" not in receipt
    assert receipt["status"] == "REPLY"


@pytest.mark.parametrize(
    ("mode", "extra", "reason"),
    [
        ("drift-slow", {}, "MODEL_DRIFT"),
        (
            "context",
            {"TRIGGER": "1000", "ALLOWANCE": "100000", "HANDOFF_RESERVE": "99990"},
            "CONTEXT",
        ),
    ],
)
def test_claude_hard_stop_wins_over_cutoff(launch, mode, extra, reason):
    result = launch("hard-" + mode, mode=mode, extra={"HANDOFF_RESERVE": "90", **extra})
    receipt = result["receipt"]
    assert "handoff_argv" not in receipt
    assert receipt["stop_reason"] == reason
    assert receipt["status"] == "FAIL"


def test_claude_budget_still_stops_during_handoff(launch):
    result = launch("late", mode="handoff-budget", extra={"HANDOFF_RESERVE": "90"})
    receipt = result["receipt"]
    assert "handoff_argv" in receipt
    assert receipt["stop_reason"] == "BUDGET"
    assert receipt["status"] == "FAIL"
    assert receipt["usage_status"] == "known"
    assert receipt["remaining_processes"] == []
