import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def worker():
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "codex_launch", ROOT / "scripts/swarm/codex_launch.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.pop(0)


def test_model_and_effort_require_authorized_assignment(worker):
    state = {
        "assignments": {
            "feature-builder": {"model": "gpt-6-astra", "effort": "low", "thread_id": "thread"}
        }
    }
    env = {"ASSIGNMENT_ID": "feature-builder"}
    assert worker.assignment(state, "gpt-6-astra", env)["effort"] == "low"
    assert worker.assignment(state, "resume:thread", env)["model"] == "gpt-6-astra"
    with pytest.raises(ValueError):
        worker.assignment(state, "resume:other", env)
    with pytest.raises(ValueError):
        worker.assignment(state, "resume:thread", {**env, "EFFORT": "medium"})
    state["assignments"]["feature-builder"]["effort"] = "medium"
    assert (
        worker.assignment(state, "resume:thread", {**env, "EFFORT": "medium"})["effort"] == "medium"
    )


def test_resume_command_always_sets_model_and_effort(worker, tmp_path):
    cmd = worker.command(
        "resume:thread",
        {"model": "gpt-6-astra", "effort": "medium"},
        tmp_path,
        tmp_path / "reply",
        "implement",
        {},
    )
    assert cmd[:3] == ["codex", "exec", "resume"]
    assert cmd[cmd.index("-m") + 1] == "gpt-6-astra"
    assert 'model_reasoning_effort="medium"' in cmd


def test_fresh_workspace_write_uses_automatic_review(worker, tmp_path):
    selected = {"model": "gpt-6.1-sol", "effort": "medium"}
    cmd = worker.command("gpt-6.1-sol", selected, tmp_path, tmp_path / "reply", "tests", {})
    assert "--approve-for-me" in cmd
    assert "-s" not in cmd
    assert cmd[cmd.index("-C") + 1] == str(tmp_path)
    assert cmd[cmd.index("--add-dir") + 1] == "/tmp/factory-fp-0925/swarm"
    env = {"SANDBOX": "read-only"}
    cmd = worker.command("gpt-6.1-sol", selected, tmp_path, tmp_path / "reply", "tests", env)
    assert "--approve-for-me" not in cmd
    assert cmd[cmd.index("-s") + 1] == "read-only"


def test_resume_workspace_write_keeps_automatic_review(worker, tmp_path):
    selected = {"model": "gpt-6.1-sol", "effort": "medium"}
    cmd = worker.command("resume:thread", selected, tmp_path, tmp_path / "reply", "go", {})
    assert 'approval_policy="on-request"' in cmd
    assert 'approvals_reviewer="auto_review"' in cmd
    assert 'sandbox_mode="workspace-write"' in cmd
    env = {"SANDBOX": "read-only"}
    cmd = worker.command("resume:thread", selected, tmp_path, tmp_path / "reply", "go", env)
    assert 'approval_policy="on-request"' not in cmd
    assert 'approvals_reviewer="auto_review"' not in cmd
    assert 'sandbox_mode="read-only"' in cmd


def test_only_new_turn_settings_trigger_drift(worker, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    old = json.dumps({"type": "turn_context", "payload": {"model": "old", "effort": "high"}}) + "\n"
    rollout.write_text(old)
    offset = len(old.encode())
    rollout.write_text(
        old
        + json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-astra", "effort": "low"}})
        + "\n"
    )
    assert worker.drift(rollout, offset, {"model": "gpt-6-astra", "effort": "low"}) == []
    assert worker.drift(rollout, offset, {"model": "gpt-6-astra", "effort": "medium"}) == [
        "EFFORT_DRIFT"
    ]


def test_supervisor_stub_scenarios(tmp_path):
    import os
    import subprocess
    import time

    script = ROOT / "scripts/swarm/codex-agent.sh"
    for scenario, expected in [
        ("early", 0),
        ("empty", 1),
        ("error", 1),
        ("idle", 0),
        ("start_timeout", 1),
        ("grandchild", 1),
        ("budget", 1),
        ("exec", 1),
    ]:
        work = tmp_path / scenario
        work.mkdir()
        stub = work / "codex"
        stub.write_text("""#!/usr/bin/env python3
import json,os,sys,time,subprocess,signal
from pathlib import Path
scenario=os.environ['SCENARIO']
if scenario=='start_timeout':
 signal.signal(signal.SIGINT,signal.SIG_IGN)
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
 time.sleep(20)
reply=Path(sys.argv[sys.argv.index('-o')+1])
rollout=Path(os.environ['CODEX_HOME'])/'sessions/rollout-test-thread.jsonl'
rollout.parent.mkdir(parents=True,exist_ok=True)
rollout.write_text(json.dumps({'type':'turn_context','payload':{'model':'gpt-6-astra','effort':'low'}})+'\\n'+json.dumps({'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':20,'cached_input_tokens':5,'output_tokens':3}}}})+'\\n')
print(json.dumps({'type':'thread.started','thread_id':'test-thread'}),flush=True)
if scenario=='grandchild':
 code='import signal,time; '
 code+='signal.signal(signal.SIGINT,signal.SIG_IGN); '
 code+='signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(20)'
 child=subprocess.Popen([sys.executable,'-c',code])
 Path(os.environ['CHILD_PID']).write_text(str(child.pid))
 time.sleep(.5)
if scenario=='exec':
 code='import os,time; time.sleep(.3); os.execv("/bin/sleep",["sleep","20"])'
 child=subprocess.Popen([sys.executable,'-c',code])
 Path(os.environ['CHILD_PID']).write_text(str(child.pid))
 time.sleep(.6)
if scenario=='budget': time.sleep(5)
reply.write_text('' if scenario=='empty' else 'done')
if scenario in ('early','idle'): time.sleep(.4)
sys.exit(7 if scenario=='error' else 0)
""")
        stub.write_text(stub.read_text().replace("#!/usr/bin/env python3", "#!" + sys.executable))
        stub.chmod(0o755)
        run = work / "run.json"
        run.write_text(
            json.dumps(
                {
                    "lifecycle": "preparing",
                    "blockers": [],
                    "deadlines": {},
                    "ceilings": {"overall": 1000, "preparation": 1000},
                    "consumed": {"overall": 0, "preparation": 0},
                    "active_lead": None,
                    "dispatch_frozen": False,
                    "reservations": {},
                    "inventory": {"threads": [], "sessions": []},
                    "assignments": {
                        "a": {"model": "gpt-6-astra", "effort": "low", "reason": "test"}
                    },
                }
            )
        )
        msg = work / "brief"
        msg.write_text("test")
        builder_brief = work / "prompts/phase-1/builder-brief.md"
        builder_brief.parent.mkdir(parents=True)
        builder_brief.write_text((ROOT / "prompts/phase-1/builder-brief.md").read_text())
        env = dict(
            os.environ,
            PATH=str(work) + os.pathsep + os.environ["PATH"],
            PILOT_RUN=str(run),
            ASSIGNMENT_ID="a",
            ALLOCATION_ID="allocation",
            ALLOWANCE="15" if scenario == "budget" else "100",
            ROLE="builder",
            FEATURE="fixture",
            REPO=str(work),
            CODEX_HOME=str(work / "codex-home"),
            SCENARIO=scenario,
            CHILD_PID=str(work / "child.pid"),
            START_S="0.2" if scenario == "start_timeout" else "2",
            IDLE_S="0.1",
            PILOT_TICK="0.02",
            PILOT_STOP_INT="0.1",
            PILOT_STOP_TERM="0.1",
        )
        # The early scenario passes its settings as KEY=VALUE arguments instead of env.
        settings = []
        if scenario == "early":
            for key in ("ASSIGNMENT_ID", "ALLOCATION_ID", "ROLE", "FEATURE", "REPO"):
                settings.append(key + "=" + env.pop(key))
        start = time.monotonic()
        result = subprocess.run(
            ["/bin/zsh", str(script), *settings, str(work), "launch", "gpt-6-astra", str(msg)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == expected, (scenario, result.stdout, result.stderr)
        if scenario in {"early", "idle"}:
            assert time.monotonic() - start >= 0.4
            assert "STALL_IDLE" in result.stdout
        receipt = json.loads((work / "launch-launch.json").read_text())
        assert receipt["repo"] == str(work)
        assert receipt["pid"] == receipt["pgid"]
        assert receipt["status"] == ("REPLY" if expected == 0 else "FAIL")
        assert receipt["ended_at"]
        if scenario == "exec":
            changed = int((work / "child.pid").read_text())
            os.kill(changed, 9)
            assert receipt["remaining_processes"] == [changed]
            assert receipt["ownership_blocked"] is True
            continue
        assert receipt["remaining_processes"] == []
        if scenario == "budget":
            assert receipt["stop_reason"] == "BUDGET"
            assert time.monotonic() - start < 4
        if scenario == "error":
            assert receipt["child_exit"] == 7
        saved = json.loads(run.read_text())
        if scenario != "start_timeout":
            assert receipt["usage_delta"]["new_tokens"] == 18
        if scenario in {"start_timeout", "grandchild", "error", "budget"}:
            assert saved["runs"]["launch"]["usage_status"] == "unknown"
        else:
            assert saved["runs"]["launch"]["usage_status"] == "known"


def test_exit_snapshot_rechecks_without_accepting_changed_identity(worker, monkeypatch):
    import owned_processes

    identity = {
        "pid": 42,
        "pgid": 42,
        "os_session": 1,
        "started_at": "original",
        "command": "python",
    }
    exiting = {**identity, "command": "(python)", "status": "Rs"}
    monkeypatch.setattr(owned_processes.time, "sleep", lambda duration: None)
    monkeypatch.setattr(owned_processes, "processes", lambda: {})
    assert owned_processes.survivors({42: identity}, {42: exiting}) == []
    monkeypatch.setattr(owned_processes, "processes", lambda: {42: exiting})
    with pytest.raises(ValueError, match="Uncertain"):
        owned_processes.survivors({42: identity}, {42: exiting})


def test_usage_skips_token_count_without_info(worker, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rows = [
        {
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 7}}},
        },
        {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert worker.usage(rollout) == {"input_tokens": 7}
