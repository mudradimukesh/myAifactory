import importlib.util
import sys
from pathlib import Path

import pytest

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


def state():
    return {
        "lifecycle": "running",
        "blockers": [],
        "deadlines": {},
        "active_lead": {"pid": 42, "model_session_id": "lead-session"},
        "dispatch_frozen": False,
        "handoff": {"token": "token", "ack_path": "/tmp/ack-token.json"},
    }


def event(tool, **args):
    return {"tool_name": tool, "tool_input": args, "session_id": "lead-session"}


def test_frozen_lead_cannot_dispatch_but_can_snapshot(guard, tmp_path, monkeypatch):
    s = state()
    s["dispatch_frozen"] = True
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    with pytest.raises(ValueError):
        guard.check(event("Agent"), s, tmp_path / "run.json", 100, 83000)
    assert guard.check(
        event("Write", file_path=str(tmp_path / "handoff/token.md")),
        s,
        tmp_path / "run.json",
        84000,
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
            event("Bash", command="builder-start.sh x y"),
            s,
            tmp_path / "run.json",
            100,
            83000,
        )


def test_cap_allows_exact_handoff_but_no_dispatch(guard, tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    for command in [
        str(ROOT / "scripts/swarm/interior-lead.sh") + " --handoff-begin",
        str(ROOT / "scripts/swarm/interior-lead.sh") + " --handoff-start token",
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
            event("Write", file_path="state.md"),
            state(),
            tmp_path / "run.json",
            100,
            83000,
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


def test_reviewer_hooks_register_measure_and_release(guard, tmp_path, monkeypatch):
    import json

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
    run.write_text(json.dumps(s))
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
    transcript.write_text(json.dumps(message) + "\n" + json.dumps(message) + "\n")
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


def test_frozen_snapshot_accepts_symlinked_art_path(guard, tmp_path, monkeypatch):
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
        84000,
        83000,
    )
    for path in (alias / "state.md", alias / "handoff/other.md"):
        with pytest.raises(ValueError):
            guard.check(
                event("Write", file_path=str(path)), s, alias / "run.json", 84000, 83000
            )


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
        'wezterm cli send-text --pane-id 262 "ALERT: frozen; sleep until ruling"'
        " && wezterm cli send-text --pane-id 262 --no-paste $'\\r' && echo sent"
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/swarm/fp-guard.py")],
        input=json.dumps(
            {**event("Bash", command=command), "transcript_path": str(transcript)}
        ),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "LEAD_AGENT": "p1-pilot-lead",
            "PILOT_RUN": str(run),
            "ORCHESTRATOR_PANE": "262",
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "command",
    [
        'wezterm cli send-text --pane-id 999 "ALERT: wrong pane"',
        'wezterm cli send-text --pane-id 262 "ALERT: ok" && touch /tmp/unrelated',
        'wezterm cli send-text --pane-id 262 "ALERT: $(touch /tmp/unrelated)"',
        'wezterm cli send-text --pane-id 262 "ALERT: ok" > /tmp/unrelated',
    ],
)
def test_alert_exception_is_only_a_literal_message_to_orchestrator(guard, command):
    assert not guard.orchestrator_alert(event("Bash", command=command), state())


def test_alert_exception_rejects_unregistered_or_subagent_identity(guard):
    e = event("Bash", command='wezterm cli send-text --pane-id 262 "ALERT: held"')
    e["session_id"] = "unknown"
    assert not guard.orchestrator_alert(e, state())
    e["session_id"] = "lead-session"
    e["agent_id"] = "reviewer"
    assert not guard.orchestrator_alert(e, state())
