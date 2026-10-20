import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest_plugins = ("test_pilot_handoff",)


@pytest.mark.parametrize(
    ("case", "expected_exit"),
    [
        ("user", 0),
        ("queued", 0),
        ("different-pane", 2),
        ("registered-before-target", 2),
        ("registered-after-notification", 2),
        ("registered-with-target", 2),
        ("registered-with-notification", 2),
        ("unregistered", 2),
        ("unrelated-transcript", 2),
        ("altered-history", 2),
    ],
    ids=[
        "user",
        "queued",
        "different-pane",
        "registered-before-target",
        "registered-after-notification",
        "registered-with-target",
        "registered-with-notification",
        "unregistered",
        "unrelated-transcript",
        "altered-history",
    ],
)
def test_drain_retire_continuation(
    lead: ModuleType, tmp_path: Path, case: str, expected_exit: int
) -> None:
    handoff = importlib.import_module("test_pilot_handoff")
    _, path = handoff.drain_fixture(lead, tmp_path, pid_alive=False)
    report = tmp_path / "handoff/lead-drained-drain.md"
    report.parent.mkdir()
    report.write_text("drained\n")
    message = "DRAIN_COMPLETE drained n1 " + str(report)
    target_id = "1c9d9f40-6dfa-4e08-adad-90ea06e58bec"
    continued_id = "68d7cae1-0529-406c-a4ef-502483bfbae3"
    target = tmp_path / (target_id + ".jsonl")
    continued = tmp_path / (continued_id + ".jsonl")
    history: list[dict[str, object]] = [
        {
            "type": "user",
            "sessionId": target_id,
            "uuid": "history-user",
            "parentUuid": None,
            "timestamp": "2026-10-12T23:00:00Z",
            "message": {"role": "user", "content": "Retire the drained lead when ready."},
        },
        {
            "type": "assistant",
            "sessionId": target_id,
            "uuid": "history-assistant",
            "parentUuid": "history-user",
            "timestamp": "2026-10-12T23:00:01Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Waiting."}]},
        },
    ]
    target.write_text("".join(json.dumps(row) + "\n" for row in history))
    copied: list[dict[str, object]] = [{**row, "sessionId": continued_id} for row in history]
    if case == "unrelated-transcript":
        copied = []
    elif case == "altered-history":
        copied[0] = {
            **copied[0],
            "message": {"role": "user", "content": "A different conversation."},
        }
    notification: dict[str, object] = {
        "type": "user",
        "sessionId": continued_id,
        "uuid": "completion-receipt",
        "parentUuid": "history-assistant",
        "timestamp": "2026-10-12T23:03:00Z",
        "message": {"role": "user", "content": message},
    }
    if case == "queued":
        notification = {
            **notification,
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "prompt": message,
                "commandMode": "prompt",
                "origin": {"kind": "human"},
            },
        }
        del notification["message"]
    continued.write_text("".join(json.dumps(row) + "\n" for row in [*copied, notification]))
    target_registration = {
        "session_id": target_id,
        "thread_id": target_id,
        "role": "orchestrator",
        "pane_id": 111,
        "transcript_path": str(target),
        "started_at": "2026-10-12T23:00:00Z",
    }
    continued_registration = {
        "session_id": continued_id,
        "thread_id": continued_id,
        "role": "orchestrator",
        "pane_id": 112 if case == "different-pane" else 111,
        "transcript_path": str(continued),
        "started_at": {
            "registered-before-target": "2026-10-12T22:59:00Z",
            "registered-after-notification": "2026-10-12T23:04:00Z",
            "registered-with-target": "2026-10-12T23:00:00Z",
            "registered-with-notification": "2026-10-12T23:03:00Z",
        }.get(case, "2026-10-12T23:02:00Z"),
    }
    state = json.loads(path.read_text())
    registrations = [target_registration, continued_registration]
    if case == "registered-before-target":
        registrations.reverse()
    elif case == "unregistered":
        registrations.pop()
    state["inventory"]["threads"] = registrations
    state["draining_leads"]["drained"]["completion"] = {
        "report": str(report),
        "report_sha256": lead.sha(report),
        "targets": [{"identity": target_id, "pane": "111", "transcript_path": str(target)}],
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
        "Continuation receipt retirement returned the wrong exit code: " + result.stderr
    )
    receipt_path = tmp_path / "handoff/lead-drained-drain-retirement.json"
    if expected_exit:
        assert result.stderr.strip() == "REFUSED: Native completion notification receipt is missing"
        assert path.read_bytes() == before
        assert not receipt_path.exists()
        return
    saved = json.loads(path.read_text())
    assert saved["draining_leads"] == {}
    assert saved["runs"]["lead-drained"]["usage_status"] == "known"
    receipt = json.loads(receipt_path.read_text())
    notifications = receipt["notification_receipts"]
    assert len(notifications) == 1
    assert {target_id, continued_id} <= set(notifications[0].values())
    assert notifications[0]["receipt_id"] == "completion-receipt"
    assert notifications[0]["timestamp"] == "2026-10-12T23:03:00Z"
    assert saved["drain_history"][-1]["retirement"]["notification_receipts"] == notifications
    assert receipt["remaining_processes"] == []
    assert receipt["final_usage"]["new_tokens"] == 12
    assert target.read_text() == "".join(json.dumps(row) + "\n" for row in history)
