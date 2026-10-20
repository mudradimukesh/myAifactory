import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import TypedDict

import pytest

from scripts.swarm.run_state import RunState

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def guard():
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "pilot_guard", ROOT / "scripts/swarm/pilot_guard.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.pop(0)


def state() -> RunState:
    return {
        "lifecycle": "running",
        "blockers": [],
        "deadlines": {},
        "active_lead": {"pid": 42, "model_session_id": "lead-session"},
        "dispatch_frozen": False,
        "inventory": {
            "threads": [
                {"role": "orchestrator", "thread_id": "current", "pane_id": 283, "complete": False}
            ]
        },
        "handoff": {"token": "token", "ack_path": "/tmp/ack-token.json"},
    }


class ToolEvent(TypedDict, total=False):
    tool_name: str
    tool_input: dict[str, object]
    session_id: str
    agent_id: str
    transcript_path: str


def event(tool: str, **args: object) -> ToolEvent:
    return {"tool_name": tool, "tool_input": args, "session_id": "lead-session"}


@pytest.mark.parametrize("block", [None, "identity", "transfer", "worker", "pause", "role"])
def test_reconcile_changed_session_preserves_process_and_guard(guard, tmp_path, monkeypatch, block):
    import run_state

    s = state()
    s["handoff"] = None
    s["active_lead"].update(
        pgid=42, os_session=0, started_at="start", command="claude --session-id lead-session"
    )
    s["consumed"] = {"overall": 123}
    s["runs"] = {"old": {"new_tokens": 123}}
    s["reservations"] = {}
    path = tmp_path / "run.json"
    transcript = tmp_path / "new-session.jsonl"
    transcript.write_text('{"sessionId":"new-session"}\n')
    live = s["active_lead"].copy()
    if block == "identity":
        live["started_at"] = "different"
    elif block == "transfer":
        s["dispatch_frozen"] = True
    elif block == "worker":
        s["reservations"]["worker"] = {"active_run": "running"}
    elif block == "pause":
        (tmp_path / "PAUSE").write_text("pause")
    monkeypatch.setattr(run_state, "processes", lambda: {42: live})
    monkeypatch.setattr(run_state, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    e = event("Bash", command="python3 red_check.py --help")
    e["session_id"] = "new-session"
    with pytest.raises(ValueError):
        guard.check(e, s, path, 100, 95000)
    data = {
        "previous_session_id": "lead-session",
        "session_id": "new-session",
        "pid": 42,
        "transcript_path": str(transcript),
        "reason": "Verified tool events changed session within the same live process",
    }
    if block:
        with pytest.raises(ValueError):
            run_state.apply(
                s,
                "launcher" if block == "role" else "orchestrator",
                "reconcile-lead-session",
                data,
                path,
            )
        assert s["active_lead"]["model_session_id"] == "lead-session"
        return
    run_state.apply(s, "orchestrator", "reconcile-lead-session", data, path)
    assert guard.check(e, s, path, 100, 95000)
    with pytest.raises(ValueError, match="Tool event session"):
        guard.check(event("Bash", command="python3 red_check.py --help"), s, path, 100, 95000)
    assert {k: v for k, v in s["active_lead"].items() if k != "model_session_id"} == {
        k: v for k, v in live.items() if k != "model_session_id"
    }
    assert s["consumed"] == {"overall": 123}
    assert s["runs"] == {"old": {"new_tokens": 123}}
    assert s["lead_session_history"][0]["previous_session_id"] == "lead-session"


@pytest.mark.parametrize("used", [84000, 115885])
def test_frozen_lead_cannot_dispatch_but_can_snapshot(guard, tmp_path, monkeypatch, used):
    s = state()
    s["dispatch_frozen"] = True
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    with pytest.raises(ValueError):
        guard.check(event("Agent"), s, tmp_path / "run.json", used, 83000)
    assert guard.check(
        event("Write", file_path=str(tmp_path / "handoff/token.md")),
        s,
        tmp_path / "run.json",
        used,
        83000,
    )


def test_inactive_successor_can_only_ack_or_read(guard, tmp_path, monkeypatch):
    s = state()
    s["handoff"]["successor_session_id"] = "successor"

    def refuse(state):
        raise ValueError("inactive")

    monkeypatch.setattr(guard, "originating_lead", refuse)
    e = event("Write", file_path="/tmp/ack-token.json")
    e["session_id"] = "successor"
    assert guard.check(e, s, tmp_path / "run.json", 100, 83000)
    with pytest.raises(ValueError):
        guard.check(
            event("Bash", command="builder-start.sh x y"), s, tmp_path / "run.json", 100, 83000
        )


def test_cap_allows_exact_handoff_but_no_dispatch(guard, tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    for command in [
        "/tmp/factory-fp-0925/swarm/bin/interior-lead.sh --handoff-begin",
        "/tmp/factory-fp-0925/swarm/bin/interior-lead.sh --handoff-start token",
    ]:
        assert guard.check(
            event("Bash", command=command), state(), tmp_path / "run.json", 95000, 83000
        )
    with pytest.raises(ValueError):
        guard.check(event("Agent"), state(), tmp_path / "run.json", 84000, 83000)


def test_pause_blocks_all_mutations(guard, tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    (tmp_path / "PAUSE").write_text("stop")
    with pytest.raises(ValueError):
        guard.check(
            event("Write", file_path="state.md"), state(), tmp_path / "run.json", 100, 83000
        )


def test_reviewer_output_requires_registered_session_and_exact_path(guard, tmp_path):
    s = state()
    s["inventory"] = {
        "sessions": [
            {
                "session_id": "review",
                "role": "reviewer",
                "parent_session_id": "lead-session",
                "output_paths": [str(tmp_path / "tag-code-review.json")],
            }
        ]
    }
    e = event("Write", file_path=str(tmp_path / "tag-code-review.json"))
    e["session_id"] = "review"
    assert guard.check(e, s, tmp_path / "run.json", 100, 83000)
    e["tool_input"]["file_path"] = str(tmp_path / "state.md")
    with pytest.raises(ValueError):
        guard.check(e, s, tmp_path / "run.json", 100, 83000)


def test_subagent_cannot_inherit_parent_dispatch_rights(guard, tmp_path, monkeypatch):
    s = state()
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(guard, "check_action", lambda *args, **kwargs: None)
    e = event("Bash", command="prepare_candidate.sh tag builder")
    e["agent_id"] = "reviewer-id"
    e["agent_type"] = "p1-pilot-reviewer"
    with pytest.raises(ValueError):
        guard.check(e, s, tmp_path / "run.json", 100, 83000)


def test_registered_agent_id_can_write_only_packet_output(guard, tmp_path):
    s = state()
    output = str(tmp_path / "tag-code-review.json")
    s["inventory"] = {
        "sessions": [
            {
                "session_id": "lead-session/reviewer-id",
                "agent_id": "reviewer-id",
                "parent_session_id": "lead-session",
                "role": "reviewer",
                "output_paths": [output],
            }
        ]
    }
    e = event("Write", file_path=output)
    e.update(agent_id="reviewer-id", agent_type="p1-pilot-reviewer")
    assert guard.check(e, s, tmp_path / "run.json", 100, 83000)
    e["tool_input"]["file_path"] = str(tmp_path / "state.md")
    with pytest.raises(ValueError):
        guard.check(e, s, tmp_path / "run.json", 100, 83000)


def test_originating_lead_keeps_model_session_identity(tmp_path, monkeypatch):
    import os

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        import run_state

        identity = {
            "pid": os.getpid(),
            "ppid": 1,
            "pgid": 42,
            "os_session": 42,
            "started_at": "now",
            "command": "stub",
        }
        monkeypatch.setattr(run_state, "processes", lambda: {os.getpid(): identity})
        active = {**identity, "model_session_id": "lead-session"}
        assert (
            run_state.originating_lead({"active_lead": active})["model_session_id"]
            == "lead-session"
        )
    finally:
        sys.path.pop(0)


def test_reviewer_hooks_consume_transact_state_with_short_cli_output(
    guard: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json
    import subprocess

    s = state()
    s.update(
        consumed={"overall": 0, "pilot": 0},
        pilot_started_at="now",
        runs={
            "review-run": {
                "allocation_id": "review",
                "stage": "pilot",
                "new_tokens": 0,
                "cache_reads": 0,
            }
        },
        reservations={
            "review": {
                "role": "reviewer",
                "active_run": "review-run",
                "allowance": 60,
                "observed": 0,
                "released": False,
                "parent_session_id": "lead-session",
                "output_paths": [str(tmp_path / "feature-code-review.json")],
            }
        },
        inventory={"sessions": [], "threads": []},
    )
    run = tmp_path / "run.json"
    s["assignment_history"] = [
        {"assignment_id": f"old-{i}", "reason": "UNRELATED_HISTORY_" + "x" * 1024}
        for i in range(1100)
    ]
    run.write_text(json.dumps(s))
    assert run.stat().st_size >= 1024 * 1024
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    parent = tmp_path / "lead-session.jsonl"
    parent.write_text("")
    event = {
        "session_id": "lead-session",
        "agent_id": "reviewer",
        "agent_type": "p1-pilot-reviewer",
        "transcript_path": str(parent),
        "hook_event_name": "SubagentStart",
    }
    guard.reviewer_event(event, run)
    registered = json.loads(run.read_text())["inventory"]["sessions"][0]
    assert registered["agent_id"] == "reviewer"
    assert registered["output_paths"] == s["reservations"]["review"]["output_paths"]
    transcript = tmp_path / "reviewer.jsonl"
    message = {
        "message": {
            "id": "m1",
            "usage": {
                "input_tokens": 7,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 20,
                "output_tokens": 2,
            },
        }
    }
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "ALLOCATION: review\nReview the changes."},
            }
        )
        + "\n"
        + json.dumps(message)
        + "\n"
        + json.dumps(message)
        + "\n"
    )
    event.update(hook_event_name="SubagentStop", agent_transcript_path=str(transcript))
    guard.reviewer_event(event, run)
    saved = json.loads(run.read_text())
    assert saved["consumed"]["overall"] == 12
    assert saved["runs"]["review-run"]["cache_reads"] == 20
    assert saved["reservations"]["review"]["released"]
    assert saved["inventory"]["sessions"][0]["complete"]
    message["message"]["id"] = "m2"
    message["message"]["usage"] = {"input_tokens": 1, "output_tokens": 1}
    with transcript.open("a") as output:
        output.write(json.dumps(message) + "\n")
    guard.reviewer_event(event, run)
    guard.reviewer_event(event, run)
    saved = json.loads(run.read_text())
    assert saved["consumed"]["overall"] == 14
    assert saved["reservations"]["review"]["released"]
    payload = {"allocation_id": "review", "run_id": "review-run"}
    released = guard.transact(run, "launcher", "release", payload)
    handback: ToolEvent = {
        "session_id": "lead-session",
        "agent_id": "reviewer",
        "tool_name": "SubagentHandback",
        "tool_input": {},
    }
    assert guard.check(handback, released, run, 100, 83000)
    handback.update(
        tool_name="Write", tool_input={"file_path": s["reservations"]["review"]["output_paths"][0]}
    )
    with pytest.raises(ValueError, match="Completed reviewer may only hand back its result"):
        guard.check(handback, released, run, 100, 83000)
    assert capsys.readouterr() == ("", "")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/swarm/run_state.py"),
            "--run",
            str(run),
            "--as",
            "launcher",
            "release",
            "--data",
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert len(result.stdout.encode("utf-8")) <= 512
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout) == {"ok": True, "operation": "release", "key": "review-run"}
    saved = json.loads(run.read_text())
    assert saved["consumed"] == released["consumed"]
    assert saved["reservations"] == released["reservations"]
    assert saved["inventory"] == released["inventory"]


@pytest.mark.parametrize("caller", ["fp-guard.py", "pilot_lead.py"])
def test_caller_denial_outputs_only_reason_and_related_rows(tmp_path: Path, caller: str) -> None:
    import json
    import os
    import subprocess

    blocker = {
        "id": "blocked-launch",
        "status": "open",
        "actions": ["dispatch", "launch:lead"],
    }
    s = state()
    s.update(
        active_lead=None,
        handoff=None,
        blockers=[blocker],
        assignment_history=[
            {"assignment_id": f"old-{i}", "reason": "UNRELATED_HISTORY_" + "x" * 1024}
            for i in range(1100)
        ],
    )
    run = tmp_path / "run.json"
    run.write_text(json.dumps(s))
    assert run.stat().st_size >= 1024 * 1024
    before = run.read_bytes()
    command = [sys.executable, str(ROOT / "scripts/swarm" / caller)]
    if caller == "pilot_lead.py":
        command.append("--start")
    result = subprocess.run(
        command,
        input=json.dumps(event("Write", file_path=str(tmp_path / "state.md"))),
        capture_output=True,
        text=True,
        env={**os.environ, "PILOT_RUN": str(run), "LEAD_AGENT": "p1-pilot-lead", "FP_LEAD": "0"},
    )
    assert result.returncode == 2
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert "Blocked by blocked-launch" in lines[0]
    assert len(lines) == 2
    assert all(len(line.encode("utf-8")) <= 2048 for line in lines)
    assert json.loads(lines[1]) == {"blockers": [blocker]}
    assert run.read_bytes() == before


@pytest.mark.parametrize("used", [84000, 115885])
def test_frozen_snapshot_accepts_symlinked_art_path(guard, tmp_path, monkeypatch, used):
    s = state()
    s["dispatch_frozen"] = True
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert guard.check(
        event("Write", file_path=str(alias / "handoff/token.md")),
        s,
        alias / "run.json",
        used,
        83000,
    )
    for path in (alias / "state.md", alias / "handoff/other.md"):
        with pytest.raises(ValueError):
            guard.check(event("Write", file_path=str(path)), s, alias / "run.json", used, 83000)


def test_registered_reviewer_can_hand_back_after_stop_only(guard, tmp_path):
    s = state()
    output = str(tmp_path / "tag-code-review.json")
    s["inventory"] = {
        "sessions": [
            {
                "session_id": "lead-session/reviewer-id",
                "agent_id": "reviewer-id",
                "parent_session_id": "lead-session",
                "role": "reviewer",
                "output_paths": [output],
                "complete": True,
            }
        ]
    }
    e = event("SubagentHandback", message="Review files written")
    e.update(agent_id="reviewer-id", agent_type="p1-pilot-reviewer")
    assert guard.check(e, s, tmp_path / "run.json", 100, 83000)
    e["tool_name"] = "Write"
    e["tool_input"] = {"file_path": output}
    with pytest.raises(ValueError):
        guard.check(e, s, tmp_path / "run.json", 100, 83000)
    e["tool_name"] = "SubagentHandback"
    e["agent_id"] = "unregistered"
    with pytest.raises(ValueError):
        guard.check(e, s, tmp_path / "run.json", 100, 83000)


@pytest.mark.parametrize("blocked", [False, True])
def test_lead_alert_reaches_orchestrator_even_when_frozen_or_paused(tmp_path, blocked):
    import json
    import os
    import subprocess

    s = state()
    s.update(dispatch_frozen=True, lifecycle="paused" if blocked else "running")
    run = tmp_path / "run.json"
    run.write_text(json.dumps(s))
    transcript = tmp_path / "lead.jsonl"
    transcript.write_text(json.dumps({"message": {"usage": {"input_tokens": 200000}}}))
    command = (
        'wezterm cli send-text --pane-id 283 "ALERT: frozen; sleep until ruling"'
        " && wezterm cli send-text --pane-id 283 --no-paste $'\\r' && echo sent"
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/swarm/fp-guard.py")],
        input=json.dumps({**event("Bash", command=command), "transcript_path": str(transcript)}),
        capture_output=True,
        text=True,
        env={**os.environ, "LEAD_AGENT": "p1-pilot-lead", "PILOT_RUN": str(run)},
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "command",
    [
        'wezterm cli send-text --pane-id 999 "ALERT: wrong pane"',
        'wezterm cli send-text --pane-id 283 "ALERT: ok" && touch /tmp/unrelated',
        'wezterm cli send-text --pane-id 283 "ALERT: $(touch /tmp/unrelated)"',
        'wezterm cli send-text --pane-id 283 "ALERT: ok" > /tmp/unrelated',
    ],
)
def test_alert_exception_is_only_a_literal_message_to_orchestrator(guard, command):
    assert not guard.orchestrator_alert(event("Bash", command=command), state())


def test_alert_exception_rejects_unregistered_or_subagent_identity(guard):
    e = event("Bash", command='wezterm cli send-text --pane-id 283 "ALERT: held"')
    e["session_id"] = "unknown"
    assert not guard.orchestrator_alert(e, state())
    e["session_id"] = "lead-session"
    e["agent_id"] = "reviewer"
    assert not guard.orchestrator_alert(e, state())


def test_alert_follows_latest_registered_orchestrator(guard):
    s = state()
    s["inventory"]["threads"].append(
        {"role": "orchestrator", "thread_id": "next", "pane_id": 284, "complete": False}
    )
    assert not guard.orchestrator_alert(
        event("Bash", command='wezterm cli send-text --pane-id 283 "old"'), s
    )
    assert guard.orchestrator_alert(
        event("Bash", command='wezterm cli send-text --pane-id 284 "new"'), s
    )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"role": "orchestrator", "pane_id": 283, "complete": True}],
        [{"role": "builder", "pane_id": 283}],
        [{"role": "orchestrator", "pane_id": "283; touch /tmp/x"}],
        [{"role": "orchestrator", "pane_id": 283}, {"role": "orchestrator"}],
    ],
)
def test_alert_fails_closed_without_current_pane(guard, rows):
    s = state()
    s["inventory"]["threads"] = rows
    assert not guard.orchestrator_alert(
        event("Bash", command='wezterm cli send-text --pane-id 283 "alert"'), s
    )


@pytest.mark.parametrize(
    "used,allowed", [(95000, False), (95001, True), (104500, True), (104501, False)]
)
def test_orchestrator_report_reserve_after_normal_handoff(
    guard, tmp_path, monkeypatch, used, allowed
):
    s = state()
    s["dispatch_frozen"] = True
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    (tmp_path / "handoff").mkdir()
    (tmp_path / "handoff/token.md").write_text("normal handoff")
    e = event("Write", file_path=str(tmp_path / "handoff/lead-lead-session-orchestrator.md"))
    if allowed:
        assert guard.check(e, s, tmp_path / "run.json", used, 95000)
    else:
        with pytest.raises(ValueError):
            guard.check(e, s, tmp_path / "run.json", used, 95000)


@pytest.mark.parametrize(
    "block", ["missing", "unfrozen", "other_path", "session", "agent", "pause"]
)
def test_orchestrator_report_reserve_preserves_controls(guard, tmp_path, monkeypatch, block):
    s = state()
    s["dispatch_frozen"] = True
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    (tmp_path / "handoff").mkdir()
    (tmp_path / "handoff/token.md").write_text("normal handoff")
    e = event("Edit", file_path=str(tmp_path / "handoff/lead-lead-session-orchestrator.md"))
    if block == "missing":
        (tmp_path / "handoff/token.md").unlink()
    elif block == "unfrozen":
        s["dispatch_frozen"] = False
    elif block == "other_path":
        e["tool_input"]["file_path"] = str(tmp_path / "state.md")
    elif block == "session":
        e["session_id"] = "other"
    elif block == "agent":
        e["agent_id"] = "unregistered"
    else:
        (tmp_path / "PAUSE").write_text("pause")
    with pytest.raises(ValueError):
        guard.check(e, s, tmp_path / "run.json", 95001, 95000)


@pytest.mark.parametrize("used,allowed", [(121072, True), (127620, True), (127621, False)])
def test_report_reserve_starts_after_normal_handoff(guard, tmp_path, monkeypatch, used, allowed):
    import json

    s = state()
    s["dispatch_frozen"] = True
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    snapshot = tmp_path / "handoff/token.md"
    snapshot.parent.mkdir()
    snapshot.write_text("normal handoff")
    transcript = tmp_path / "lead.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "message": {
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 2693,
                        "cache_read_input_tokens": 115425,
                    },
                    "content": [
                        {"type": "tool_use", "name": "Write", "input": {"file_path": str(snapshot)}}
                    ],
                }
            }
        )
        + "\n"
    )
    e = event("Write", file_path=str(tmp_path / "handoff/lead-lead-session-orchestrator.md"))
    e["transcript_path"] = str(transcript)
    if allowed:
        assert guard.check(e, s, tmp_path / "run.json", used, 95000)
    else:
        with pytest.raises(ValueError):
            guard.check(e, s, tmp_path / "run.json", used, 95000)


@pytest.mark.parametrize(
    "command, allowed",
    [
        ("ART={run} {bin}/unit_gate.sh fast poc-save-repair base cand", True),
        ("{bin}/red_check.py poc-save-repair pred tests --art {run}", True),
        ("ART=/tmp/elsewhere {bin}/unit_gate.sh fast poc-save-repair base cand", False),
        ("{bin}/unit_gate.sh pre drain-tag base cand", False),
        ("{bin}/red_check.py drain-tag pred tests", False),
        ("{bin}/unit_gate.sh fast poc-save-repair {scope}/x cand", False),
        ("{bin}/merge_unit.sh poc-save-repair", True),
        ("{bin}/merge_unit.sh drain-tag", False),
        ("ART={run} {bin}/prepare_candidate.sh poc-save poc-save-repair/1", True),
        ("{bin}/codex-agent.sh {run} slot-2", True),
        ("{bin}/codex-agent.sh {run}/scope/x slot-2", False),
        ("{bin}/codex-agent.sh {run}/scope slot-2", False),
        ("{bin}/codex-agent.sh FEATURE=f REPO=/tmp/other {run} slot-2", True),
        ("{bin}/codex-agent.sh FEATURE=f REPO={scope}/x {run} slot-2", False),
        ("{bin}/codex-agent.sh PATH=/tmp/x {run} slot-2", False),
        ("{bin}/alert-orchestrator.sh --log 'Q-lead-1: done'", True),
    ],
)
def test_drain_gate_blocks_only_its_own_tag(guard, tmp_path, monkeypatch, command, allowed):
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(guard, "check_action", lambda *args, **kwargs: None)
    s = state()
    scope = tmp_path / "run" / "scope"
    gate = "/repo/.venv/bin/python /tmp/factory-fp-0925/swarm/bin/unit_gate.py pre drain-tag a b"
    s["draining_leads"] = {"old": {"scopes": [str(scope)], "gates": [{"pid": 7, "command": gate}]}}
    monkeypatch.setattr(guard, "processes", lambda: {7: {"pid": 7, "command": gate, "status": "S"}})
    path = tmp_path / "run" / "run.json"
    e = event(
        "Bash",
        command=command.format(run=path.parent, bin="/tmp/factory-fp-0925/swarm/bin", scope=scope),
    )
    if allowed:
        assert guard.check(e, s, path, 100, 95000)
    else:
        with pytest.raises(ValueError):
            guard.check(e, s, path, 100, 95000)


def test_dead_drain_gate_does_not_block_merge(guard, tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(guard, "check_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(guard, "processes", lambda: {})
    s = state()
    gate = "/repo/.venv/bin/python /tmp/factory-fp-0925/swarm/bin/unit_gate.py pre drain-tag a b"
    s["draining_leads"] = {"old": {"scopes": [], "gates": [{"pid": 7, "command": gate}]}}
    path = tmp_path / "run" / "run.json"
    e = event("Bash", command="/tmp/factory-fp-0925/swarm/bin/merge_unit.sh drain-tag")
    assert guard.check(e, s, path, 100, 95000)


@pytest.fixture
def drain_repos(tmp_path):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "sub" / "file").write_text("x")

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    git("init", "-b", "main")
    git("add", ".")
    git("commit", "-m", "base")
    scope = tmp_path / "run" / "scope"
    lead = tmp_path / "wt" / "lead"
    git("worktree", "add", "-b", "drain-branch", str(scope))
    git("worktree", "add", "-b", "lead-branch", str(lead))
    return repo, scope, lead


@pytest.mark.parametrize(
    "command, allowed",
    [
        ("git -C {lead} status", True),
        ("git -C {lead} merge main", True),
        ("git -C {lead} branch new-branch", True),
        ("git -C {lead} worktree add {tmp}/wt/other", True),
        ("git -C {lead} commit -m message", True),
        ("git -C {repo} log", True),
        ("git -C {repo} diff main lead-branch", True),
        ("git commit -m message", False),
        ("git -C wt/lead status", False),
        ("git -C {scope} status", False),
        ("git -C {tmp}/run status", False),
        ("git -C {repo} commit -m message", False),
        ("git -C {repo}/sub restore file", False),
        ("git -C {lead} merge drain-branch", False),
        ("git -C {lead} diff main..drain-branch", False),
        ("git -C {lead} show refs/heads/drain-branch:sub/file", False),
        ("git -C {lead} branch -D new-branch", False),
        ("git -C {lead} branch -f lead-branch main", False),
        ("git -C {lead} branch --delete x", False),
        ("git -C {lead} branch -m a b", False),
        ("git -C {lead} branch --list x -D", False),
        ("git -C {repo} branch x", False),
        ("git -C {repo} branch -D x", False),
        ("git -C {repo} branch -f x", False),
        ("git -C {repo} branch --delete x", False),
        ("git -C {repo} branch -m a b", False),
        ("git -C {repo} branch --list x -D", False),
        ("git -C {lead} for-each-ref refs/heads | cat", False),
        ("git -C {lead} for-each-ref refs/heads > {tmp}/refs", False),
        ("git -C {repo} for-each-ref refs/heads | cat", False),
        ("git -C {repo} for-each-ref refs/heads > {tmp}/refs", False),
        ("git -C {lead} checkout -B lead-branch", False),
        ("git -C {lead} switch -C lead-branch", False),
        ("git -C {lead} checkout --force main", False),
        ("git -C {lead} worktree remove {scope}", False),
        ("git -C {lead} worktree prune", False),
        ("git -C {lead} worktree add {scope}/x", False),
        ("git -C {lead} checkout ../../run/scope", False),
        ("git -C {lead} reset --hard main", False),
        ("git -C {lead} push interior lead-branch", False),
        ("git -C {lead} fetch interior main:main", False),
        ("git -C {lead} -c core.hooksPath=/x commit -m message", False),
        ("GIT_DIR=/x git -C {lead} status", False),
        ("git -C {lead} status; rm -rf {scope}", False),
    ],
)
def test_concurrent_drain_git(guard, tmp_path, monkeypatch, drain_repos, command, allowed):
    repo, scope, lead = drain_repos
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(guard, "check_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(guard, "processes", lambda: {})
    s = state()
    s["draining_leads"] = {"old": {"scopes": [str(scope)], "gates": []}}
    path = tmp_path / "run" / "run.json"
    e = event("Bash", command=command.format(repo=repo, scope=scope, lead=lead, tmp=tmp_path))
    if allowed:
        assert guard.check(e, s, path, 100, 95000)
    else:
        with pytest.raises(ValueError):
            guard.check(e, s, path, 100, 95000)


@pytest.mark.parametrize("checkout", ["lead", "repo"])
@pytest.mark.parametrize(
    "command",
    [
        "branch --list",
        "branch --list lead-*",
        "branch --show-current",
        "for-each-ref refs/heads",
        "for-each-ref --format=%(refname) refs/heads",
    ],
)
def test_concurrent_drain_git_ref_listing(
    guard, tmp_path, monkeypatch, drain_repos, checkout, command
):
    repo, scope, lead = drain_repos
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(guard, "check_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(guard, "processes", lambda: {})
    s = state()
    s["draining_leads"] = {"old": {"scopes": [str(scope)], "gates": []}}
    root = lead if checkout == "lead" else repo
    e = event("Bash", command=f"git -C {root} {command}")
    try:
        decision = guard.check(e, s, tmp_path / "run" / "run.json", 100, 95000)
    except ValueError as exc:
        decision = str(exc)
    assert decision is True, decision


def test_reviewer_hook_prints_related_row_note(
    guard: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import io
    import json
    import runpy

    import run_state

    from scripts.swarm.run_state import RunRecord

    related: RunRecord = {"usage_status": "unknown"}
    s = state()
    transcript = tmp_path / "lead-session/subagents/agent-reviewer.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "ALLOCATION: review"}})
        + "\n"
    )
    s["inventory"]["sessions"] = [
        {
            "session_id": "lead-session/reviewer",
            "agent_id": "reviewer",
            "parent_session_id": "lead-session",
            "role": "reviewer",
            "transcript_path": str(transcript),
            "pending": True,
            "complete": False,
        }
    ]
    s.update(
        runs={"unknown-run": related, "review-run": {"allocation_id": "review"}},
        reservations={
            "review": {
                "role": "reviewer",
                "parent_session_id": "lead-session",
                "active_run": "review-run",
                "output_paths": [str(tmp_path / "review.json")],
            }
        },
        assignment_history=[
            {"assignment_id": f"old-{i}", "reason": "UNRELATED_HISTORY_" + "x" * 1024}
            for i in range(1100)
        ],
    )
    run = tmp_path / "run.json"
    run.write_text(json.dumps(s))
    assert run.stat().st_size >= 1024 * 1024
    before = run.read_bytes()

    def refuse_record(
        current: RunState, role: str, operation: str, data: RunRecord, path: Path
    ) -> RunState:
        assert path == run
        assert role == "launcher"
        assert operation == "record-run"
        assert data["allocation_id"] == "review"
        run_state.budget(current)
        return current

    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    monkeypatch.setattr(run_state, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(run_state, "apply", refuse_record)
    monkeypatch.setenv("PILOT_RUN", str(run))
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "lead-session",
                    "agent_id": "reviewer",
                    "agent_type": "p1-pilot-reviewer",
                    "hook_event_name": "PreToolUse",
                    "transcript_path": str(tmp_path / "lead-session.jsonl"),
                }
            )
        ),
    )
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(ROOT / "scripts/swarm/pilot_guard.py"), run_name="__main__")
    assert stopped.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert lines[0] == "Pilot reviewer hook: Usage is unknown for an inventoried run"
    assert len(lines) == 2
    assert all(len(line.encode("utf-8")) <= 2048 for line in lines)
    assert json.loads(lines[1]) == {"runs": {"unknown-run": related}}
    assert run.read_bytes() == before


@pytest.mark.parametrize(
    "command, completion, allowed",
    [
        ('wezterm cli kill-pane --pane-id "$WEZTERM_PANE"', True, True),
        ("wezterm cli kill-pane --pane-id 232", True, True),
        ("wezterm cli kill-pane --pane-id 239", True, False),
        ('wezterm cli kill-pane --pane-id "$WEZTERM_PANE"', False, False),
    ],
)
def test_draining_lead_closes_only_its_own_pane_after_drain_ready(
    guard, tmp_path, monkeypatch, command, completion, allowed
):
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(guard, "check_action", lambda *args, **kwargs: None)
    s = state()
    s["draining_leads"] = {
        "lead-session": {
            "scopes": [],
            "gates": [],
            "lead": {"pid": 42, "pane": "232"},
            **(
                {"completion": {"targets": [], "message": "DRAIN_COMPLETE x"}} if completion else {}
            ),
        }
    }
    e = event("Bash", command=command)
    if allowed:
        assert guard.check(e, s, tmp_path / "run.json", 100, 95000)
    else:
        with pytest.raises(ValueError):
            guard.check(e, s, tmp_path / "run.json", 100, 95000)


@pytest.mark.parametrize(
    "images, allowed",
    [("/repo/data/licensed-floorplans/images", True), ("/repo/data/other", False)],
)
def test_concurrent_drain_allows_only_licensed_image_dir(
    guard, tmp_path, monkeypatch, images, allowed
):
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(guard, "check_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(guard, "processes", lambda: {})
    s = state()
    s["draining_leads"] = {"old": {"scopes": [], "gates": []}}
    path = tmp_path / "run" / "run.json"
    e = event(
        "Bash",
        command="PILOT_LICENSED_IMAGES="
        + images
        + " /tmp/factory-fp-0925/swarm/bin/merge_unit.sh tag",
    )
    if allowed:
        assert guard.check(e, s, path, 100, 95000)
    else:
        with pytest.raises(ValueError, match="unknown environment key"):
            guard.check(e, s, path, 100, 95000)


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("rg -n RULING ART/questions.md | head -20", True),
        ("sed -n '10,40p' ART/questions.md", True),
        ("tail -n 25 ART/report.md", True),
        ("ps -o pid=,stat= -p 20355", True),
        ("wezterm cli get-text --pane-id 256 --start-line -6", True),
        ("git log -1 --format=%h", True),
        ("jq -c .lifecycle ART/run.json | cut -c1-80", True),
        ("sed -i '' s/a/b/ ART/questions.md", False),
        ("sed 's/a/b/' ART/questions.md", False),
        ("tail -f ART/logs/gate.log", False),
        ("sort -o ART/out.txt ART/in.txt", False),
        ("rg x ART | sh", False),
        ("git -C /tmp/wt status", False),
        ("cat ART/a > ART/b", False),
        ("wezterm cli send-text --pane-id 1 hi", False),
        ("wc -l ART/a && sed -n '1,5p' ART/b; git status\nps -p 1", True),
        ("cat ART/a && rm ART/a", False),
        ("cat ART/a & sleep 1", False),
        ("rg x ART;", False),
        ("rg 'RULING|BLOCKER' ART/questions.md", True),
        ("rg -n 'a;b' ART/q.md | head -3", True),
        ("sed -n 'w /tmp/out' ART/q.md", False),
        ("sed -n '1,5p;w /tmp/out' ART/q.md", False),
        ("sed -n -f script.sed ART/q.md", False),
        ("sed -n 'e touch /tmp/x' ART/q.md", False),
        ("tail --follow ART/log", False),
        ("tail -F ART/log", False),
        ("git diff --output=/tmp/x", False),
        ("git log --ext-diff -p", False),
        ("rg --pre /bin/sh x ART", False),
        ("cat a\nrm b", False),
        ("rg '>' ART/q.md", False),
        ("sort -ro /tmp/out ART/in", False),
        ("sort ART/in", False),
        ("date -s 0", False),
        ("tail -n5f ART/log", False),
        ("tail -qF ART/log", False),
        ("rg -nz x ART", False),
        ("rg --pre=/bin/sh x ART", False),
        ("git log --output /tmp/x", False),
        ("rg -n -A 3 -m 2 x ART/q.md", True),
        ("tail -n 20 ART/log", True),
        ("rg -- -f ART/q.md", True),
        ("head -c 100 ART/q.md | wc -c", True),
    ],
)
def test_readonly_covers_common_reads(guard, command, allowed):
    assert guard.readonly({"tool_name": "Bash", "tool_input": {"command": command}}) is allowed


def test_write_capable_read_forms_keep_full_checks(guard, tmp_path):
    s = state()
    s["dispatch_frozen"] = True
    path = tmp_path / "run.json"
    with pytest.raises(ValueError):
        guard.check(
            event("Bash", command="sed -n 'w " + str(tmp_path / "x") + "' " + str(path)),
            s,
            path,
            999999,
            95000,
        )


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("/tmp/factory-fp-0925/swarm/bin/alert-orchestrator.sh 'Q-lead-1: blocked'", True),
        ("/tmp/factory-fp-0925/swarm/bin/alert-orchestrator.sh --log 'Q-lead-1: blocked'", True),
        ("/tmp/factory-fp-0925/swarm/bin/alert-orchestrator.sh --target lead 'x'", False),
        ("/tmp/factory-fp-0925/swarm/bin/alert-orchestrator.sh 'x' && rm -rf /tmp/y", False),
        ("/tmp/factory-fp-0925/swarm/bin/alert-orchestrator.sh 'x' > /tmp/y", False),
        ("/tmp/factory-fp-0925/swarm/bin/alert-orchestrator.sh", False),
        ("/tmp/elsewhere/alert-orchestrator.sh 'x'", False),
    ],
)
def test_alert_helper_passes_the_alert_exemption_only_exactly(guard, command, allowed):
    assert guard.orchestrator_alert(event("Bash", command=command), state()) is allowed
