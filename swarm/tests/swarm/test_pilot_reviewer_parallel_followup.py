import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "pilot_guard_followup", ROOT / "scripts/swarm/pilot_guard.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "originating_lead", lambda state: state["active_lead"])
        yield module
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize("reservation_count", [0, 1, 2, 3])
def test_unbound_stop_accounts_usage_without_releasing_reservations(
    guard: ModuleType, tmp_path: Path, reservation_count: int
) -> None:
    original_runs = {
        "run-" + str(index): {
            "allocation_id": str(index),
            "stage": "pilot",
            "new_tokens": 0,
            "cache_reads": 0,
        }
        for index in range(reservation_count)
    }
    state = {
        "active_lead": {"model_session_id": "lead"},
        "consumed": {"overall": 0, "pilot": 0},
        "pilot_started_at": "now",
        "inventory": {"sessions": [], "threads": []},
        "reservations": {
            str(index): {
                "role": "reviewer",
                "parent_session_id": "lead",
                "active_run": "run-" + str(index),
                "output_paths": [str(tmp_path / (str(index) + "-review.json"))],
                "observed": 0,
                "released": False,
            }
            for index in range(reservation_count)
        },
        "runs": original_runs,
    }
    path = tmp_path / "run.json"
    path.write_text(json.dumps(state))
    parent_transcript = tmp_path / "lead.jsonl"
    parent_transcript.write_text("")
    event = {
        "session_id": "lead",
        "agent_id": "unbound",
        "agent_type": "p1-pilot-reviewer",
        "hook_event_name": "SubagentStart",
        "transcript_path": str(parent_transcript),
        "agent_transcript_path": None,
    }
    guard.reviewer_event(event, path)
    transcript = tmp_path / "unbound.jsonl"
    message = {
        "message": {
            "id": "message-1",
            "usage": {
                "input_tokens": 7,
                "cache_creation_input_tokens": 3,
                "output_tokens": 2,
                "cache_read_input_tokens": 20,
            },
        }
    }
    transcript.write_text(json.dumps(message) + "\n" + json.dumps(message) + "\n")
    event.update(hook_event_name="SubagentStop", agent_transcript_path=str(transcript))
    guard.reviewer_event(event, path)
    saved = json.loads(path.read_text())
    assert saved["consumed"] == {"overall": 12, "pilot": 12}
    reviewer = saved["inventory"]["sessions"][0]
    assert reviewer["complete"] and not reviewer["pending"]
    assert reviewer["transcript_path"] == str(transcript)
    usage_runs = [run for run in saved["runs"].values() if run.get("session_id") == "lead/unbound"]
    assert len(usage_runs) == 1
    assert usage_runs[0]["new_tokens"] == 12
    assert usage_runs[0]["cache_reads"] == 20
    assert usage_runs[0]["complete"]
    assert not usage_runs[0].get("allocation_id"), "RP-F2: invalid prompt must stay unbound"
    assert usage_runs[0]["role"] == "reviewer"
    assert usage_runs[0]["agent_id"] == "unbound"
    assert saved["reservations"] == state["reservations"]
    for run_id, run in original_runs.items():
        assert saved["runs"][run_id] == run
    guard.reviewer_event(event, path)
    repeated = json.loads(path.read_text())
    assert repeated["consumed"] == saved["consumed"]
    assert repeated["reservations"] == saved["reservations"]
    assert repeated["inventory"] == saved["inventory"]
    message["message"]["id"] = "message-2"
    message["message"]["usage"] = {"input_tokens": 1, "output_tokens": 1}
    with transcript.open("a") as output:
        output.write(json.dumps(message) + "\n")
    guard.reviewer_event(event, path)
    guard.reviewer_event(event, path)
    saved = json.loads(path.read_text())
    assert saved["consumed"] == {"overall": 14, "pilot": 14}
    assert saved["reservations"] == state["reservations"]
    for run_id, run in original_runs.items():
        assert saved["runs"][run_id] == run


@pytest.fixture
def reviewer_state(tmp_path: Path) -> dict[str, object]:
    return {
        "lifecycle": "running",
        "blockers": [],
        "deadlines": {},
        "active_lead": {"model_session_id": "lead"},
        "dispatch_frozen": False,
        "consumed": {"overall": 0, "pilot": 0},
        "pilot_started_at": "now",
        "inventory": {"sessions": [], "threads": []},
        "reservations": {
            "first": {
                "role": "reviewer",
                "parent_session_id": "lead",
                "active_run": "first-run",
                "output_paths": [str(tmp_path / "first-review.json")],
                "observed": 0,
                "released": False,
            }
        },
        "runs": {
            "first-run": {
                "allocation_id": "first",
                "stage": "pilot",
                "new_tokens": 0,
                "cache_reads": 0,
            }
        },
    }


@pytest.mark.parametrize("allocation", ["second", "first"])
def test_second_pending_reviewer_stops_without_cross_assignment(
    guard: ModuleType, tmp_path: Path, reviewer_state: dict[str, object], allocation: str
) -> None:
    path = tmp_path / "run.json"
    path.write_text(json.dumps(reviewer_state))
    parent = tmp_path / "lead.jsonl"
    parent.write_text("")
    event = {
        "session_id": "lead",
        "agent_id": "agent-first",
        "agent_type": "p1-pilot-reviewer",
        "hook_event_name": "SubagentStart",
        "transcript_path": str(parent),
        "agent_transcript_path": None,
    }
    guard.reviewer_event(event, path)
    guard.reviewer_event({**event, "agent_id": "agent-second"}, path)
    before = json.loads(path.read_text())
    if allocation == "first":
        before["runs"]["first-run"].update(session_id="lead/agent-first", agent_id="agent-first")
        before["inventory"]["sessions"][0]["pending"] = False
    else:
        before["reservations"]["second"] = {
            **before["reservations"]["first"],
            "active_run": "second-run",
            "output_paths": [str(tmp_path / "second-review.json")],
        }
        before["runs"]["second-run"] = {**before["runs"]["first-run"], "allocation_id": "second"}
    path.write_text(json.dumps(before))
    transcript = tmp_path / "second.jsonl"
    transcript.write_text(
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": "ALLOCATION: " + allocation}}
        )
        + "\n"
        + json.dumps(
            {"message": {"id": "usage-1", "usage": {"input_tokens": 7, "output_tokens": 5}}}
        )
        + "\n"
    )
    stop = {
        **event,
        "agent_id": "agent-second",
        "hook_event_name": "SubagentStop",
        "agent_transcript_path": str(transcript),
    }
    try:
        guard.reviewer_event(stop, path)
    except ValueError as error:
        raise AssertionError("RP-F2: stop lost final usage: " + str(error)) from error
    saved = json.loads(path.read_text())
    assert saved["runs"]["first-run"] == before["runs"]["first-run"], "RP-F2: other run changed"
    assert saved["reservations"]["first"] == before["reservations"]["first"], (
        "RP-F2: other allocation released"
    )
    assert saved["inventory"]["sessions"][0] == before["inventory"]["sessions"][0]
    assert saved["consumed"] == {"overall": 12, "pilot": 12}
    run_id = "second-run" if allocation == "second" else "reviewer-lead/agent-second"
    assert saved["runs"][run_id]["new_tokens"] == 12, (
        "RP-F2: usage must follow the first-user allocation"
    )
    if allocation == "second":
        assert saved["reservations"]["second"]["released"]
    else:
        assert not saved["runs"][run_id].get("allocation_id")
    guard.reviewer_event(stop, path)
    repeated = json.loads(path.read_text())
    assert repeated["consumed"] == saved["consumed"]
    assert repeated["reservations"] == saved["reservations"]


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_registered_reviewer_reads_while_lead_dispatch_is_blocked(
    guard: ModuleType, tmp_path: Path, reviewer_state: dict[str, object], tool: str
) -> None:
    state = json.loads(json.dumps(reviewer_state))
    state["dispatch_frozen"] = True
    state["blockers"] = [{"id": "lead-frozen", "actions": ["dispatch"]}]
    state["runs"]["first-run"].update(session_id="lead/agent-first", agent_id="agent-first")
    state["inventory"]["sessions"] = [
        {
            "session_id": "lead/agent-first",
            "agent_id": "agent-first",
            "parent_session_id": "lead",
            "role": "reviewer",
            "pending": False,
            "complete": False,
            "output_paths": state["reservations"]["first"]["output_paths"],
        }
    ]
    path = tmp_path / "run.json"
    path.write_text(json.dumps(state))
    event = {
        "session_id": "lead",
        "agent_id": "agent-first",
        "tool_name": tool,
        "tool_input": {"file_path": str(ROOT / "scripts/swarm/pilot_guard.py")},
    }
    try:
        allowed = guard.check(event, state, path, 100, 83000)
    except ValueError as error:
        raise AssertionError("RP-F3: registered reviewer read was denied: " + str(error)) from error
    assert allowed, "RP-F3: registered reviewer must retain read access"
    with pytest.raises(ValueError, match="Blocked by lead-frozen"):
        guard.check(
            {
                **event,
                "tool_name": "Write",
                "tool_input": {"file_path": str(tmp_path / "first-review.json")},
            },
            state,
            path,
            100,
            83000,
        )


@pytest.mark.parametrize("identity", ["session", "subagent"])
@pytest.mark.parametrize(
    "command",
    [
        "shasum -a 256 tests/swarm/test_pilot_reviewer_parallel.py",
        "git show HEAD:scripts/swarm/pilot_guard.py",
        "git diff HEAD -- scripts/swarm/pilot_guard.py",
        "pytest tests/swarm/test_pilot_reviewer_parallel.py::"
        "test_parallel_starts_record_pending_agents",
    ],
)
def test_registered_reviewer_verifies_candidate_while_another_lead_drains(
    guard: ModuleType,
    tmp_path: Path,
    reviewer_state: dict[str, object],
    identity: str,
    command: str,
) -> None:
    state = json.loads(json.dumps(reviewer_state))
    state["draining_leads"] = {
        "old-lead": {
            "lead": {"model_session_id": "old-lead"},
            "scopes": [str(tmp_path / "old-candidate")],
        }
    }
    state["runs"]["first-run"].update(session_id="lead/agent-first", agent_id="agent-first")
    state["inventory"]["sessions"] = [
        {
            "session_id": "lead/agent-first",
            "agent_id": "agent-first",
            "parent_session_id": "lead",
            "role": "reviewer",
            "pending": False,
            "complete": False,
            "output_paths": state["reservations"]["first"]["output_paths"],
        }
    ]
    path = tmp_path / "run.json"
    path.write_text(json.dumps(state))
    event = {
        "session_id": "lead/agent-first" if identity == "session" else "lead",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    if identity == "subagent":
        event["agent_id"] = "agent-first"
    try:
        allowed = guard.check(event, state, path, 100, 83000)
    except ValueError as error:
        raise AssertionError("RP-F4: reviewer verification was denied: " + str(error)) from error
    assert allowed, "RP-F4: registered reviewer must verify the candidate"
    assert guard.check(
        {
            **event,
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / "first-review.json")},
        },
        state,
        path,
        100,
        83000,
    )
    for tool_input, tool in [
        ({"file_path": str(tmp_path / "unregistered.json")}, "Write"),
        ({"command": "touch unregistered.json"}, "Bash"),
        ({"command": command + " > unregistered.json"}, "Bash"),
    ]:
        with pytest.raises(ValueError):
            guard.check(
                {**event, "tool_name": tool, "tool_input": tool_input}, state, path, 100, 83000
            )


@pytest.fixture
def bound_reviewer_state(reviewer_state: dict[str, object], tmp_path: Path) -> dict[str, object]:
    state = json.loads(json.dumps(reviewer_state))
    state["draining_leads"] = {
        "old-lead": {
            "lead": {"model_session_id": "old-lead"},
            "scopes": [str(tmp_path / "old-candidate")],
        }
    }
    state["runs"]["first-run"].update(session_id="lead/agent-first", agent_id="agent-first")
    state["inventory"]["sessions"] = [
        {
            "session_id": "lead/agent-first",
            "agent_id": "agent-first",
            "parent_session_id": "lead",
            "role": "reviewer",
            "pending": False,
            "complete": False,
            "output_paths": state["reservations"]["first"]["output_paths"],
        }
    ]
    return dict(state)


@pytest.mark.parametrize("identity", ["session", "subagent"])
@pytest.mark.parametrize(
    "command",
    [
        "{root}/.venv/bin/pytest tests/swarm/test_pilot_reviewer_parallel.py::"
        "test_parallel_starts_record_pending_agents",
        "git -C {root} diff 7484d8e..HEAD -- scripts/swarm/pilot_guard.py",
        "git -C {root} show HEAD:scripts/swarm/pilot_guard.py",
        "shasum -a 256 tests/swarm/test_pilot_reviewer_parallel.py",
    ],
    ids=["venv-pytest", "git-C-diff", "git-C-show", "sha256"],
)
def test_bound_reviewer_runs_mapped_commands_during_drain(
    guard: ModuleType,
    tmp_path: Path,
    bound_reviewer_state: dict[str, object],
    identity: str,
    command: str,
) -> None:
    event = {
        "session_id": "lead/agent-first" if identity == "session" else "lead",
        "tool_name": "Bash",
        "tool_input": {"command": command.format(root=ROOT)},
    }
    if identity == "subagent":
        event["agent_id"] = "agent-first"
    try:
        allowed = guard.check(event, bound_reviewer_state, tmp_path / "run.json", 100, 83000)
    except ValueError as error:
        raise AssertionError("RP-F4: mapped verification denied: " + str(error)) from error
    assert allowed, "RP-F4: bound reviewer must run mapped verification during drain"


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_nonreviewer_subagent_keeps_base_read_tools(
    guard: ModuleType, tmp_path: Path, reviewer_state: dict[str, object], tool: str
) -> None:
    event = {
        "session_id": "lead",
        "agent_id": "builder-agent",
        "agent_type": "builder",
        "tool_name": tool,
        "tool_input": {"file_path": str(ROOT / "scripts/swarm/pilot_guard.py")},
    }
    try:
        allowed = guard.check(event, reviewer_state, tmp_path / "run.json", 100, 83000)
    except ValueError as error:
        raise AssertionError("RP-F5: base read tool denied: " + str(error)) from error
    assert allowed, "RP-F5: non-reviewer keeps base read tools"
    for tool_input, denied_tool in [
        ({"file_path": str(tmp_path / "first-review.json")}, "Write"),
        (
            {
                "command": "pytest tests/swarm/test_pilot_reviewer_parallel.py::"
                "test_parallel_starts_record_pending_agents"
            },
            "Bash",
        ),
        ({}, "SubagentHandback"),
    ]:
        with pytest.raises(ValueError):
            guard.check(
                {**event, "tool_name": denied_tool, "tool_input": tool_input},
                reviewer_state,
                tmp_path / "run.json",
                100,
                83000,
            )


@pytest.mark.parametrize("identity", ["session", "subagent"])
@pytest.mark.parametrize(
    "command",
    [
        "git diff --output={outside}",
        "git -C {root} diff --output {outside}",
        "git show --output={outside} HEAD",
        "git -C {root} show HEAD --output={outside}",
        "pytest --junitxml={outside} {node}",
        "pytest --junitxml {outside} {node}",
        "{root}/.venv/bin/pytest --basetemp={outside} {node}",
        "pytest --basetemp {outside} {node}",
        "npm run test",
    ],
    ids=[
        "diff-output-equals",
        "git-C-output",
        "show-output",
        "git-C-show-output",
        "junit-equals",
        "junit-separate",
        "venv-basetemp",
        "basetemp-separate",
        "npm-run",
    ],
)
def test_bound_reviewer_rejects_verification_writes_outside_outputs(
    guard: ModuleType,
    tmp_path: Path,
    bound_reviewer_state: dict[str, object],
    identity: str,
    command: str,
) -> None:
    event = {
        "session_id": "lead/agent-first" if identity == "session" else "lead",
        "tool_name": "Bash",
        "tool_input": {
            "command": command.format(
                root=ROOT,
                outside=tmp_path / "unregistered.json",
                node="tests/swarm/test_pilot_reviewer_parallel.py::"
                "test_parallel_starts_record_pending_agents",
            )
        },
    }
    if identity == "subagent":
        event["agent_id"] = "agent-first"
    try:
        guard.check(event, bound_reviewer_state, tmp_path / "run.json", 100, 83000)
    except ValueError:
        return
    raise AssertionError("RP-F6: reviewer command can write outside registered outputs")


@pytest.mark.parametrize("option", ["--junitxml", "--basetemp"])
@pytest.mark.parametrize("syntax", ["equals", "separate"])
def test_bound_reviewer_accepts_pytest_registered_output(
    guard: ModuleType,
    tmp_path: Path,
    bound_reviewer_state: dict[str, object],
    option: str,
    syntax: str,
) -> None:
    output = str(tmp_path / "first-review.json")
    argument = option + ("=" if syntax == "equals" else " ") + output
    event = {
        "session_id": "lead/agent-first",
        "tool_name": "Bash",
        "tool_input": {
            "command": str(ROOT / ".venv/bin/pytest")
            + " "
            + argument
            + " tests/swarm/test_pilot_reviewer_parallel.py::"
            "test_parallel_starts_record_pending_agents"
        },
    }
    try:
        allowed = guard.check(event, bound_reviewer_state, tmp_path / "run.json", 100, 83000)
    except ValueError as error:
        raise AssertionError("RP-F6: registered pytest output denied: " + str(error)) from error
    assert allowed, "RP-F6: pytest writes must stay in registered output paths"
