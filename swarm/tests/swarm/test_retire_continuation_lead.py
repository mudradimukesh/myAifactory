import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest_plugins = ("test_pilot_handoff",)


@pytest.mark.parametrize("lead_location", ["active", "draining"])
@pytest.mark.parametrize(
    ("case", "expected_exit"),
    [
        ("user", 2),
        ("queued", 2),
        ("different-pane", 2),
        ("different-identity", 2),
        ("continued-inventory-other-pane", 2),
        ("continued-lead-other-pane", 2),
        ("unrelated-transcript", 2),
        ("altered-history", 2),
        ("missing-message", 2),
        ("inexact-message", 2),
    ],
)
def test_drain_retire_lead_continuation(
    lead: ModuleType, tmp_path: Path, lead_location: str, case: str, expected_exit: int
) -> None:
    handoff = importlib.import_module("test_pilot_handoff")
    _, path = handoff.drain_fixture(lead, tmp_path, pid_alive=False)
    report = tmp_path / "handoff/lead-drained-drain.md"
    report.parent.mkdir()
    report.write_text("drained\n")
    message = "DRAIN_COMPLETE drained n1 " + str(report)
    target_id = "726e6418-356b-418b-9ed2-1c271e13c3bb"
    continued_id = "1e088b4c-fa31-413f-b596-a4700d422940"
    target = tmp_path / (target_id + ".jsonl")
    continued = tmp_path / (continued_id + ".jsonl")
    history: dict[str, object] = {
        "type": "user",
        "sessionId": target_id,
        "uuid": "history-user",
        "parentUuid": None,
        "timestamp": "2026-10-12T23:00:00Z",
        "message": {"role": "user", "content": "Retire the drained lead when ready."},
    }
    target.write_text(json.dumps(history) + "\n")
    target_before = target.read_bytes()
    copied = {**history, "sessionId": continued_id}
    if case == "altered-history":
        copied["message"] = {"role": "user", "content": "A different conversation."}
    notification: dict[str, object] = {
        "type": "user",
        "sessionId": continued_id,
        "uuid": "completion-receipt",
        "parentUuid": "history-user",
        "timestamp": "2026-10-12T23:03:00Z",
        "message": {"role": "user", "content": message},
    }
    if case == "queued":
        del notification["message"]
        notification.update(
            type="attachment",
            attachment={"type": "queued_command", "prompt": message, "commandMode": "prompt"},
        )
    elif case == "inexact-message":
        notification["message"] = {"role": "user", "content": message + " extra"}
    rows = [] if case == "unrelated-transcript" else [copied]
    if case != "missing-message":
        rows.append(notification)
    continued.write_text("".join(json.dumps(row) + "\n" for row in rows))
    state = json.loads(path.read_text())
    recipient = {
        **state["draining_leads"]["drained"]["lead"],
        "model_session_id": "other-lead" if case == "different-identity" else target_id,
        "pane": 133 if case == "different-pane" else 132,
        "pid": 86416,
    }
    if lead_location == "active":
        state["active_lead"] = recipient
    else:
        state["draining_leads"][target_id] = {
            "lead": recipient,
            "jobs": [],
            "gates": [],
            "status": "draining",
        }
    assert all(
        r.get("session_id") not in {target_id, continued_id} for r in state["inventory"]["sessions"]
    )
    if case == "continued-inventory-other-pane":
        state["inventory"]["sessions"].append(
            {
                "session_id": continued_id,
                "pane_id": 133,
                "role": "lead",
                "transcript_path": str(continued),
            }
        )
    elif case == "continued-lead-other-pane":
        state["draining_leads"][continued_id] = {
            "lead": {**recipient, "model_session_id": continued_id, "pane": 133},
            "jobs": [],
            "gates": [],
            "status": "draining",
        }
    state["draining_leads"]["drained"]["completion"] = {
        "report": str(report),
        "report_sha256": lead.sha(report),
        "targets": [{"identity": target_id, "pane": "132", "transcript_path": str(target)}],
        "message": message,
    }
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            str(handoff.ROOT / "scripts/swarm/pilot_lead.py"),
            "--drain-retire",
            "drained",
        ],
        env={**os.environ, "PILOT_RUN": str(path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == expected_exit, (
        "Lead continuation receipt retirement returned the wrong exit code: " + result.stderr
    )
    receipt_path = tmp_path / "handoff/lead-drained-drain-retirement.json"
    assert target.read_bytes() == target_before
    if expected_exit:
        assert result.stderr.strip() == "REFUSED: Native completion notification receipt is missing"
        assert path.read_bytes() == before
        assert not receipt_path.exists()
        return
    receipt = json.loads(receipt_path.read_text())
    assert receipt["notification_receipts"] == [
        {
            "identity": target_id,
            "session_id": continued_id,
            "timestamp": "2026-10-12T23:03:00Z",
            "receipt_id": "completion-receipt",
        }
    ]
    saved = json.loads(path.read_text())
    assert "drained" not in saved["draining_leads"]
    if lead_location == "active":
        assert saved["active_lead"] == recipient
    else:
        assert saved["draining_leads"][target_id]["lead"] == recipient
    assert saved["runs"]["lead-drained"]["usage_status"] == "known"
    assert receipt["final_usage"]["new_tokens"] == 12
    assert receipt["remaining_processes"] == []
    assert (
        saved["drain_history"][-1]["retirement"]["notification_receipts"]
        == receipt["notification_receipts"]
    )


@pytest.mark.parametrize("location", ["active", "draining", "stopped", "history-only", "run-only"])
@pytest.mark.parametrize(
    ("case", "expected_exit"),
    [
        ("user", 0),
        ("queued", 0),
        ("unregistered", 2),
        ("missing-binding", 2),
        ("wrong-binding", 2),
        ("different-pane", 2),
        ("missing-pane", 2),
        ("different-role", 2),
        ("before-target", 2),
        ("with-target", 2),
        ("after-notification", 2),
        ("with-notification", 2),
        ("missing-start", 2),
        ("invalid-start", 2),
        ("missing-target-start", 2),
        ("altered-history", 2),
        ("short-history", 2),
        ("foreign-session", 2),
        ("malformed-transcript", 2),
    ],
)
def test_registered_lead_continuation(
    lead: ModuleType, tmp_path: Path, location: str, case: str, expected_exit: int
) -> None:
    handoff = importlib.import_module("test_pilot_handoff")
    _, path = handoff.drain_fixture(lead, tmp_path, pid_alive=False)
    report = tmp_path / "handoff/lead-drained-drain.md"
    report.parent.mkdir()
    report.write_text("drained\n")
    message = "DRAIN_COMPLETE drained n1 " + str(report)
    target_id = "recipient-lead"
    continued_id = "registered-continuation"
    target = tmp_path / (target_id + ".jsonl")
    continued = tmp_path / (continued_id + ".jsonl")
    history = {
        "type": "user",
        "sessionId": target_id,
        "uuid": "history-user",
        "timestamp": "2026-10-12T23:00:00Z",
        "message": {"role": "user", "content": "Wait for the drained lead."},
    }
    usage = {
        "type": "assistant",
        "sessionId": target_id,
        "uuid": "history-assistant",
        "timestamp": "2026-10-12T23:00:01Z",
        "message": {"id": "recipient-usage", "usage": {"input_tokens": 2, "output_tokens": 3}},
    }
    target.write_text("".join(json.dumps(row) + "\n" for row in [history, usage]))
    copied = [{**row, "sessionId": continued_id} for row in [history, usage]]
    if case == "altered-history":
        copied[0]["uuid"] = "different-history"
    elif case == "short-history":
        copied = copied[:1]
    notification: dict[str, object] = {
        "type": "user",
        "sessionId": continued_id,
        "uuid": "completion-receipt",
        "timestamp": "2026-10-12T23:03:00Z",
        "message": {"role": "user", "content": message},
    }
    if case == "queued":
        del notification["message"]
        notification.update(
            type="attachment",
            attachment={"type": "queued_command", "commandMode": "prompt", "prompt": message},
        )
    elif case == "foreign-session":
        notification["sessionId"] = "unrelated-session"
    continued.write_text("".join(json.dumps(row) + "\n" for row in [*copied, notification]))
    if case == "malformed-transcript":
        with continued.open("a") as stream:
            stream.write("not json\n")
    state = json.loads(path.read_text())
    recipient = {
        **state["draining_leads"]["drained"]["lead"],
        "model_session_id": target_id,
        "pane": 132,
    }
    state["active_lead"] = recipient
    target_registration = {
        "session_id": target_id,
        "role": "lead",
        "pid": recipient["pid"],
        "transcript_path": str(target),
        "started_at": "2026-10-12T23:00:00Z",
    }
    if case == "missing-target-start":
        del target_registration["started_at"]
    state["inventory"]["sessions"].append(target_registration)
    path.write_text(json.dumps(state))
    if location in {"stopped", "history-only", "run-only"}:
        stopped = subprocess.run(
            [sys.executable, str(handoff.ROOT / "scripts/swarm/pilot_lead.py"), "--stop"],
            env={**os.environ, "PILOT_RUN": str(path)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert stopped.returncode == 0, stopped.stderr
        state = json.loads(path.read_text())
        assert state["active_lead"] is None
        assert target_id not in state["draining_leads"]
        assert state["runs"]["lead-" + target_id]["complete"] is True
        assert state["retirement_history"][-1]["predecessor"] == recipient
        if location == "history-only":
            del state["runs"]["lead-" + target_id]
        elif location == "run-only":
            state["runs"]["lead-" + target_id].update(pane=132, transcript_path=str(target))
            state["retirement_history"][-1]["predecessor"]["pane"] = 133
    elif location == "draining":
        state["active_lead"] = None
        state["draining_leads"][target_id] = {
            "lead": recipient,
            "jobs": [],
            "gates": [],
            "status": "draining",
        }
    registration = {
        "session_id": continued_id,
        "continued_session_id": "other-target" if case == "wrong-binding" else target_id,
        "role": "orchestrator" if case == "different-role" else "lead",
        "pid": recipient["pid"],
        "pane_id": 133 if case == "different-pane" else 132,
        "transcript_path": str(continued),
        "started_at": {
            "before-target": "2026-10-12T22:59:00Z",
            "with-target": "2026-10-12T23:00:00Z",
            "after-notification": "2026-10-12T23:04:00Z",
            "with-notification": "2026-10-12T23:03:00Z",
            "invalid-start": "not a timestamp",
        }.get(case, "2026-10-12T23:02:00Z"),
    }
    if case == "missing-binding":
        del registration["continued_session_id"]
    elif case == "missing-pane":
        del registration["pane_id"]
    elif case == "missing-start":
        del registration["started_at"]
    if case != "unregistered":
        registered = subprocess.run(
            [
                sys.executable,
                str(handoff.ROOT / "scripts/swarm/run_state.py"),
                "--run",
                str(path),
                "--as",
                "orchestrator",
                "register-inventory",
                "--data",
                json.dumps(registration),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert registered.returncode == 0, registered.stderr
        inventory = json.loads(path.read_text())["inventory"]
        state["inventory"] = inventory
    state["draining_leads"]["drained"]["completion"] = {
        "report": str(report),
        "report_sha256": lead.sha(report),
        "targets": [{"identity": target_id, "pane": "132", "transcript_path": str(target)}],
        "message": message,
    }
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    target_before = target.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            str(handoff.ROOT / "scripts/swarm/pilot_lead.py"),
            "--drain-retire",
            "drained",
        ],
        env={**os.environ, "PILOT_RUN": str(path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == expected_exit, (
        "Registered lead continuation returned the wrong exit code: " + result.stderr
    )
    receipt_path = tmp_path / "handoff/lead-drained-drain-retirement.json"
    assert target.read_bytes() == target_before
    if expected_exit:
        assert result.stderr.strip() == "REFUSED: Native completion notification receipt is missing"
        assert path.read_bytes() == before
        assert not receipt_path.exists()
    else:
        receipt = json.loads(receipt_path.read_text())
        assert receipt["notification_receipts"] == [
            {
                "identity": target_id,
                "session_id": continued_id,
                "timestamp": "2026-10-12T23:03:00Z",
                "receipt_id": "completion-receipt",
            }
        ]
        saved = json.loads(path.read_text())
        assert "drained" not in saved["draining_leads"]
        assert saved["runs"]["lead-drained"]["usage_status"] == "known"
        assert (
            saved["drain_history"][-1]["retirement"]["notification_receipts"]
            == receipt["notification_receipts"]
        )
