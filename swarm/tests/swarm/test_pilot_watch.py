import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def watcher(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "pilot_watch", ROOT / "scripts/swarm/pilot_watch.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    transcript = tmp_path / "lead.jsonl"
    transcript.write_text("")
    state = tmp_path / "state.md"
    state.write_text("Next: dispatch builder\n")
    run = tmp_path / "run.json"
    run.write_text(
        json.dumps(
            {
                "lifecycle": "running",
                "pilot_started_at": "1970-01-01T00:00:00Z",
                "consumed": {"overall": 91, "pilot": 91},
                "ceilings": {"overall": 100, "pilot": 100},
                "deadlines": {},
                "reservations": {},
                "runs": {},
                "work_state_path": str(state),
                "inventory": {
                    "sessions": [
                        {"role": "lead", "session_id": "lead", "transcript_path": str(transcript)}
                    ],
                    "threads": [],
                },
            }
        )
    )
    return module, run, transcript


def test_usage_and_pause_persist_without_repeating_unchanged_alerts(watcher):
    module, run, _ = watcher
    first = module.poll(run, now=1000)
    assert "W1" in {a["check"] for a in first}
    assert module.poll(run, now=1001) == []
    run.with_name("PAUSE").write_text("hold")
    assert "W0" in {a["check"] for a in module.poll(run, now=1002)}
    assert module.poll(run, now=1003) == []


def test_command_repetition_permissions_status_and_partial_lines(watcher):
    module, run, transcript = watcher
    rows = []
    for index in range(3):
        rows.append(
            {
                "timestamp": "1970-01-01T00:15:00Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": str(index),
                            "name": "Bash",
                            "input": {"command": "pytest tests/example.py"},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": str(index),
                            "content": "PermissionError: EPERM\nFAIL launch",
                        },
                    ]
                },
            }
        )
    text = "\n".join(json.dumps(row) for row in rows)
    transcript.write_text(text)
    alerts = module.poll(run, now=1000)
    assert {"W2", "W5"} <= {a["check"] for a in alerts}
    with transcript.open("a") as stream:
        stream.write("\n")
    assert "W4" in {a["check"] for a in module.poll(run, now=1001)}
    assert module.poll(run, now=1002) == []


def test_waiting_reply_and_idle_lead_exclude_pending_gate(watcher):
    import os

    module, run, transcript = watcher
    os.utime(transcript, (100, 100))
    state = json.loads(run.read_text())
    state["runs"] = {
        "builder": {"complete": True, "ended_at": "1970-01-01T00:02:00Z", "status": "REPLY"}
    }
    run.write_text(json.dumps(state))
    alerts = module.poll(run, now=1000)
    assert {"W3", "W6"} <= {a["check"] for a in alerts}
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "1970-01-01T00:02:00Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "gate",
                            "name": "Bash",
                            "input": {"command": "unit_gate.sh pre feature base sha"},
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    os.utime(transcript, (100, 100))
    assert "W3" not in {a["check"] for a in module.poll(run, now=1001)}


def test_inbox_and_repeated_gate_failures(watcher):
    module, run, transcript = watcher
    run.with_name("inbox.md").write_text("1. Run the next gate.\n")
    module.poll(run, now=1000)
    rows = [
        {
            "timestamp": "1970-01-01T00:16:40Z",
            "message": {
                "content": [
                    {"type": "tool_use", "id": str(i), "name": "Agent", "input": {}},
                    {"type": "tool_result", "tool_use_id": str(i), "content": "done"},
                ]
            },
        }
        for i in range(2)
    ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert "W6" in {a["check"] for a in module.poll(run, now=1001)}
    log = run.with_name("gate.log")
    log.write_text("FAILED tests/api/test_example.py::test_signin - assertion\n")
    gate = {
        "tree": "unchanged",
        "gates": [{"id": "G2", "exit_code": 1, "evidence": str(log)}],
        "duration": 1,
    }
    run.with_name("feature-gate.json").write_text(json.dumps(gate))
    module.poll(run, now=1002)
    gate["duration"] = 2
    run.with_name("feature-gate.json").write_text(json.dumps(gate))
    assert "W4" in {a["check"] for a in module.poll(run, now=1003)}
