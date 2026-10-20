import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pilot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "pilot_guard", ROOT / "scripts/swarm/pilot_guard.py"
        )
        assert spec is not None and spec.loader is not None
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
        state = {
            "lifecycle": "running",
            "blockers": [],
            "deadlines": {},
            "active_lead": {"pid": 42, "model_session_id": "lead-session"},
            "dispatch_frozen": False,
            "handoff": {"token": "token", "ack_path": str(tmp_path / "ack-token.json")},
            "consumed": {"overall": 0, "pilot": 0},
            "pilot_started_at": "now",
            "runs": {
                side + "-run": {
                    "allocation_id": side,
                    "stage": "pilot",
                    "new_tokens": 0,
                    "cache_reads": 0,
                }
                for side in ("left", "right")
            },
            "reservations": {
                side: {
                    "role": "reviewer",
                    "active_run": side + "-run",
                    "allowance": 60,
                    "observed": 0,
                    "released": False,
                    "parent_session_id": "lead-session",
                    "output_paths": [str(tmp_path / (side + "-code-review.json"))],
                }
                for side in ("left", "right")
            },
            "inventory": {"sessions": [], "threads": []},
        }
        (tmp_path / "run.json").write_text(json.dumps(state))
        (tmp_path / "lead-session.jsonl").write_text("")
        (tmp_path / "lead-session" / "subagents").mkdir(parents=True)
        yield guard
    finally:
        sys.path.pop(0)


@pytest.fixture
def start_event(tmp_path: Path) -> dict[str, str | None]:
    return {
        "session_id": "lead-session",
        "agent_id": "agent-left",
        "agent_type": "p1-pilot-reviewer",
        "hook_event_name": "SubagentStart",
        "transcript_path": str(tmp_path / "lead-session.jsonl"),
        "agent_transcript_path": None,
    }


@pytest.fixture
def pretool_event(tmp_path: Path) -> dict[str, object]:
    return {
        "session_id": "lead-session",
        "agent_id": "agent-left",
        "agent_type": "p1-pilot-reviewer",
        "hook_event_name": "PreToolUse",
        "transcript_path": str(tmp_path / "lead-session" / "subagents" / "agent-agent-left.jsonl"),
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "left-code-review.json")},
    }


def test_parallel_starts_record_pending_agents(
    pilot: ModuleType, tmp_path: Path, start_event: dict[str, str | None]
) -> None:
    run = tmp_path / "run.json"
    before = json.loads(run.read_text())
    for side in ("right", "left"):
        try:
            pilot.reviewer_event({**start_event, "agent_id": "agent-" + side}, run)
        except ValueError as error:
            raise AssertionError("RP-PENDING: parallel start was refused: " + str(error)) from error
    saved = json.loads(run.read_text())
    assert {row["agent_id"] for row in saved["inventory"]["sessions"]} == {
        "agent-left",
        "agent-right",
    }, "RP-PENDING: both agents must be recorded before their transcripts exist"
    assert saved["runs"] == before["runs"], "RP-PENDING: start must not select an allocation"
    assert saved["reservations"] == before["reservations"], "RP-PENDING: reservations changed"
    assert all(
        row["session_id"] == "lead-session/" + row["agent_id"]
        and row["parent_session_id"] == "lead-session"
        and not row.get("complete")
        and not row.get("output_paths")
        for row in saved["inventory"]["sessions"]
    ), "RP-PENDING: pending identities must not have output permissions"


@pytest.mark.parametrize("first", ["left", "right"])
@pytest.mark.parametrize("tool", ["Read", "Write"])
def test_first_tool_binds_exact_allocation(
    pilot: ModuleType,
    tmp_path: Path,
    start_event: dict[str, str | None],
    pretool_event: dict[str, object],
    first: str,
    tool: str,
) -> None:
    run = tmp_path / "run.json"
    for side in ("left", "right"):
        try:
            pilot.reviewer_event({**start_event, "agent_id": "agent-" + side}, run)
        except ValueError as error:
            raise AssertionError("RP-MATCH: parallel start was refused: " + str(error)) from error
        transcript = tmp_path / "lead-session" / "subagents" / ("agent-agent-" + side + ".jsonl")
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "agentId": "agent-" + side,
                    "message": {"role": "user", "content": "ALLOCATION: " + side + "\nReview"},
                }
            )
            + "\n"
        )
    other = "right" if first == "left" else "left"
    for side in (first, other):
        before = json.loads(run.read_text())
        event = {
            **pretool_event,
            "agent_id": "agent-" + side,
            "transcript_path": str(
                tmp_path / "lead-session" / "subagents" / ("agent-agent-" + side + ".jsonl")
            ),
            "tool_name": tool,
            "tool_input": {"file_path": str(tmp_path / (side + "-code-review.json"))},
        }
        try:
            allowed = pilot.check(event, before, run, 100, 83000)
        except ValueError as error:
            raise AssertionError(
                "RP-MATCH: first tool did not bind its allocation: " + str(error)
            ) from error
        assert allowed, "RP-MATCH: the first tool must bind and pass in the same call"
        saved = json.loads(run.read_text())
        registered = next(
            row for row in saved["inventory"]["sessions"] if row["agent_id"] == "agent-" + side
        )
        assert registered["session_id"] == "lead-session/agent-" + side, "RP-MATCH: wrong session"
        assert registered["output_paths"] == [str(tmp_path / (side + "-code-review.json"))], (
            "RP-MATCH: wrong output permissions"
        )
        assert saved["runs"][side + "-run"]["session_id"] == registered["session_id"], (
            "RP-MATCH: run identity was not bound durably"
        )
        assert saved["runs"][side + "-run"]["agent_id"] == "agent-" + side, "RP-MATCH: wrong agent"
        untouched = "right" if side == "left" else "left"
        assert saved["runs"][untouched + "-run"] == before["runs"][untouched + "-run"], (
            "RP-MATCH: binding changed another run"
        )
        assert saved["reservations"][untouched] == before["reservations"][untouched], (
            "RP-MATCH: binding changed another reservation"
        )


@pytest.mark.parametrize(
    "bad_prompt",
    [
        "Review",
        "Review\nALLOCATION: left",
        "ALLOCATION: ",
        "ALLOCATION: unknown",
        "allocation: left",
        " ALLOCATION: left",
    ],
    ids=["missing", "later-line", "empty", "unknown", "wrong-case", "indented"],
)
@pytest.mark.parametrize(
    "tool", ["Read", "Grep", "Glob", "Bash", "Write", "Edit", "SubagentHandback"]
)
def test_invalid_first_user_prompt_denies_tools(
    pilot: ModuleType,
    tmp_path: Path,
    start_event: dict[str, str | None],
    pretool_event: dict[str, object],
    bad_prompt: str,
    tool: str,
) -> None:
    run = tmp_path / "run.json"
    state = json.loads(run.read_text())
    del state["reservations"]["right"]
    del state["runs"]["right-run"]
    run.write_text(json.dumps(state))
    pilot.reviewer_event(start_event, run)
    transcript = Path(str(pretool_event["transcript_path"]))
    transcript.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": "ALLOCATION: left"},
                },
                {"type": "user", "message": {"role": "user", "content": bad_prompt}},
                {"type": "user", "message": {"role": "user", "content": "ALLOCATION: left"}},
            )
        )
        + "\n"
    )
    before = json.loads(run.read_text())
    event = {
        **pretool_event,
        "tool_name": tool,
        "tool_input": {"file_path": str(tmp_path / "left-code-review.json"), "command": "git diff"},
    }
    denied = False
    try:
        pilot.check(event, before, run, 100, 83000)
    except ValueError:
        denied = True
    assert denied, "RP-AMBIGUOUS: an invalid first user allocation must deny every tool"
    assert json.loads(run.read_text()) == before, "RP-AMBIGUOUS: denial changed the run state"


@pytest.mark.parametrize("transcript_state", ["missing", "empty", "no-user"])
@pytest.mark.parametrize("tool", ["Read", "Bash", "Write"])
def test_unbound_agent_denies_tools_without_first_user_message(
    pilot: ModuleType,
    tmp_path: Path,
    start_event: dict[str, str | None],
    pretool_event: dict[str, object],
    transcript_state: str,
    tool: str,
) -> None:
    run = tmp_path / "run.json"
    state = json.loads(run.read_text())
    del state["reservations"]["right"]
    del state["runs"]["right-run"]
    run.write_text(json.dumps(state))
    pilot.reviewer_event(start_event, run)
    transcript = Path(str(pretool_event["transcript_path"]))
    if transcript_state == "empty":
        transcript.write_text("")
    elif transcript_state == "no-user":
        transcript.write_text(
            json.dumps({"type": "system", "message": {"content": "ALLOCATION: left"}})
        )
    before = json.loads(run.read_text())
    denied = False
    try:
        pilot.check(
            {
                **pretool_event,
                "tool_name": tool,
                "tool_input": {
                    "command": "git diff",
                    "file_path": str(tmp_path / "left-code-review.json"),
                },
            },
            before,
            run,
            100,
            83000,
        )
    except ValueError:
        denied = True
    assert denied, "RP-UNBOUND: tools require a first user allocation line"
    assert json.loads(run.read_text()) == before, "RP-UNBOUND: denial changed the run state"


@pytest.mark.parametrize(
    "invalid", ["released", "no-active-run", "wrong-parent", "wrong-role", "no-outputs"]
)
def test_allocation_must_be_active_and_owned_by_this_lead(
    pilot: ModuleType,
    tmp_path: Path,
    start_event: dict[str, str | None],
    pretool_event: dict[str, object],
    invalid: str,
) -> None:
    run = tmp_path / "run.json"
    state = json.loads(run.read_text())
    del state["reservations"]["right"]
    del state["runs"]["right-run"]
    run.write_text(json.dumps(state))
    pilot.reviewer_event(start_event, run)
    state = json.loads(run.read_text())
    reservation = state["reservations"]["left"]
    if invalid == "released":
        reservation["released"] = True
    elif invalid == "no-active-run":
        reservation["active_run"] = None
    elif invalid == "wrong-parent":
        reservation["parent_session_id"] = "another-lead"
    elif invalid == "wrong-role":
        reservation["role"] = "builder"
    else:
        reservation["output_paths"] = []
    run.write_text(json.dumps(state))
    Path(str(pretool_event["transcript_path"])).write_text(
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": "ALLOCATION: left\nReview"}}
        )
        + "\n"
    )
    denied = False
    try:
        pilot.check(pretool_event, state, run, 100, 83000)
    except ValueError:
        denied = True
    assert denied, "RP-ACTIVE: binding requires an active reviewer reservation for this lead"
    assert json.loads(run.read_text()) == state, "RP-ACTIVE: invalid binding changed the state"


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_bound_reviewers_write_only_their_own_outputs(
    pilot: ModuleType,
    tmp_path: Path,
    start_event: dict[str, str | None],
    pretool_event: dict[str, object],
    tool: str,
) -> None:
    run = tmp_path / "run.json"
    for side in ("left", "right"):
        try:
            pilot.reviewer_event({**start_event, "agent_id": "agent-" + side}, run)
        except ValueError as error:
            raise AssertionError("RP-WRITE: parallel start was refused: " + str(error)) from error
        transcript = tmp_path / "lead-session" / "subagents" / ("agent-agent-" + side + ".jsonl")
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": "ALLOCATION: " + side + "\nReview"},
                }
            )
            + "\n"
        )
        event = {
            **pretool_event,
            "agent_id": "agent-" + side,
            "transcript_path": str(transcript),
            "tool_name": tool,
            "tool_input": {"file_path": str(tmp_path / (side + "-code-review.json"))},
        }
        try:
            allowed = pilot.check(event, json.loads(run.read_text()), run, 100, 83000)
        except ValueError as error:
            raise AssertionError("RP-WRITE: own output was refused: " + str(error)) from error
        assert allowed, "RP-WRITE: own output must be allowed"
        other = "right" if side == "left" else "left"
        transcript.write_text(
            transcript.read_text()
            + json.dumps(
                {"type": "user", "message": {"role": "user", "content": "ALLOCATION: " + other}}
            )
            + "\n"
        )
        before = json.loads(run.read_text())
        with pytest.raises(ValueError):
            pilot.check(
                {
                    **event,
                    "tool_input": {"file_path": str(tmp_path / (other + "-code-review.json"))},
                },
                before,
                run,
                100,
                83000,
            )
        assert json.loads(run.read_text()) == before, "RP-WRITE: a later message rebound the agent"
        assert pilot.check(event, json.loads(run.read_text()), run, 100, 83000), (
            "RP-WRITE: the original output must remain allowed"
        )


def test_stop_records_usage_and_releases_only_bound_allocation(
    pilot: ModuleType,
    tmp_path: Path,
    start_event: dict[str, str | None],
    pretool_event: dict[str, object],
) -> None:
    run = tmp_path / "run.json"
    for side in ("left", "right"):
        try:
            pilot.reviewer_event({**start_event, "agent_id": "agent-" + side}, run)
        except ValueError as error:
            raise AssertionError("RP-STOP: parallel start was refused: " + str(error)) from error
        transcript = tmp_path / "lead-session" / "subagents" / ("agent-agent-" + side + ".jsonl")
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": "ALLOCATION: " + side + "\nReview"},
                }
            )
            + "\n"
        )
        try:
            allowed = pilot.check(
                {
                    **pretool_event,
                    "agent_id": "agent-" + side,
                    "transcript_path": str(transcript),
                    "tool_input": {"file_path": str(tmp_path / (side + "-code-review.json"))},
                },
                json.loads(run.read_text()),
                run,
                100,
                83000,
            )
        except ValueError as error:
            raise AssertionError("RP-STOP: first tool did not bind: " + str(error)) from error
        assert allowed, "RP-STOP: binding must allow the first tool"
    before = json.loads(run.read_text())
    transcript = Path(str(pretool_event["transcript_path"]))
    message = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "usage": {
                "input_tokens": 7,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 20,
                "output_tokens": 2,
            },
        },
    }
    with transcript.open("a") as output:
        output.write(json.dumps(message) + "\n" + json.dumps(message) + "\n")
    stop = {
        **start_event,
        "hook_event_name": "SubagentStop",
        "agent_transcript_path": str(transcript),
    }
    pilot.reviewer_event(stop, run)
    saved = json.loads(run.read_text())
    assert saved["consumed"]["overall"] == 12, "RP-STOP: final usage must be deduplicated"
    assert saved["runs"]["left-run"]["new_tokens"] == 12, "RP-STOP: wrong run usage"
    assert saved["runs"]["left-run"]["cache_reads"] == 20, "RP-STOP: wrong cache usage"
    assert saved["runs"]["left-run"]["complete"], "RP-STOP: bound run must complete"
    assert saved["reservations"]["left"]["released"], "RP-STOP: bound allocation must release"
    assert saved["reservations"]["left"]["active_run"] is None, (
        "RP-STOP: completed run remains active"
    )
    assert saved["runs"]["right-run"] == before["runs"]["right-run"], "RP-STOP: other run changed"
    assert saved["reservations"]["right"] == before["reservations"]["right"], (
        "RP-STOP: other allocation changed"
    )
    assert next(row for row in saved["inventory"]["sessions"] if row["agent_id"] == "agent-left")[
        "complete"
    ], "RP-STOP: stopped agent must complete"
    assert not next(
        row for row in saved["inventory"]["sessions"] if row["agent_id"] == "agent-right"
    )["complete"], "RP-STOP: other reviewer must remain active"
    pilot.reviewer_event(stop, run)
    repeated = json.loads(run.read_text())
    assert repeated["consumed"] == saved["consumed"], "RP-STOP: repeated stop counted usage twice"
    assert repeated["reservations"] == saved["reservations"], (
        "RP-STOP: repeated stop changed allocations"
    )
