import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest_plugins = ("test_pilot_handoff",)


@pytest.mark.parametrize("content_as_list", [False, True], ids=["string", "list"])
@pytest.mark.parametrize("continued_delivery", [False, True], ids=["original", "continued"])
def test_drain_retire_continuation_response_item(
    lead: ModuleType, tmp_path: Path, content_as_list: bool, continued_delivery: bool
) -> None:
    handoff = importlib.import_module("test_pilot_handoff")
    _, path = handoff.drain_fixture(lead, tmp_path, pid_alive=False)
    report = tmp_path / "handoff/lead-drained-drain.md"
    report.parent.mkdir()
    report.write_text("drained\n")
    message = "DRAIN_COMPLETE drained n1 " + str(report)
    target_id = "target-session"
    continued_id = "continued-session"
    target = tmp_path / "target.jsonl"
    continued = tmp_path / "continued.jsonl"
    history = {
        "type": "user",
        "sessionId": target_id,
        "uuid": "history-user",
        "timestamp": "2026-10-12T23:00:00Z",
        "message": {"role": "user", "content": "Retire the drained lead when ready."},
    }
    target.write_text(json.dumps(history) + "\n")
    notification = {
        "type": "response_item",
        "sessionId": continued_id if continued_delivery else target_id,
        "ordinal": 2,
        "timestamp": "2026-10-12T23:03:00Z",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": message}] if content_as_list else message,
        },
    }
    continued.write_text(
        json.dumps({**history, "sessionId": continued_id}) + "\n" + json.dumps(notification) + "\n"
    )
    if not continued_delivery:
        with target.open("a") as stream:
            stream.write(json.dumps(notification) + "\n")
    before = target.read_bytes()
    state = json.loads(path.read_text())
    state["inventory"]["sessions"].extend(
        [
            {
                "session_id": identity,
                "role": "orchestrator",
                "pane_id": 111,
                "transcript_path": str(transcript),
                "started_at": started,
            }
            for identity, transcript, started in [
                (target_id, target, "2026-10-12T23:00:00Z"),
                (continued_id, continued, "2026-10-12T23:02:00Z"),
            ]
        ]
    )
    state["draining_leads"]["drained"]["completion"] = {
        "report": str(report),
        "report_sha256": lead.sha(report),
        "targets": [{"identity": target_id, "pane": "111", "transcript_path": str(target)}],
        "message": message,
    }
    path.write_text(json.dumps(state))
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
    assert result.returncode == 0, result.stderr
    receipt = json.loads((tmp_path / "handoff/lead-drained-drain-retirement.json").read_text())
    assert receipt["notification_receipts"] == [
        {
            "identity": target_id,
            **({"session_id": continued_id} if continued_delivery else {}),
            "timestamp": notification["timestamp"],
            "receipt_id": 2,
        }
    ]
    saved = json.loads(path.read_text())
    assert saved["draining_leads"] == {}
    assert (
        saved["drain_history"][-1]["retirement"]["notification_receipts"]
        == receipt["notification_receipts"]
    )
    assert target.read_bytes() == before
