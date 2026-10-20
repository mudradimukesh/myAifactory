import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.swarm.run_state import processes

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("wezterm cli send-text --pane-id 170 --no-paste $'\\r'", True),
        ("wezterm cli send-text --pane-id 171 --no-paste $'\\r'", True),
        ("wezterm cli send-text --pane-id 999 --no-paste $'\\r'", False),
        ("wezterm cli send-text --pane-id 170 --no-paste $'\\r' extra", False),
        ("wezterm cli send-text --pane-id 170 --no-paste $'\\n'", False),
        ("wezterm cli send-text --pane-id 170 --no-paste $'\\rmore'", False),
        ("wezterm cli send-text --pane-id 170 --no-paste '$\\r'", False),
        ("wezterm cli send-text --pane-id 170 --no-paste \"$'\\r'\"", False),
        ("wezterm cli send-text --pane-id 170 --no-paste $'\\r' && echo sent", False),
        ("wezterm cli send-text --pane-id 170 --no-paste $'\\r'; true", False),
        ("wezterm cli send-text --pane-id 170 --no-paste $'\\r' > /tmp/output", False),
        ("wezterm cli send-text --pane-id 170 --no-paste $'\\r'$(echo extra)", False),
        ("wezterm cli send-text --pane-id 170 --no-paste 'DRAIN_COMPLETE old'", True),
        ("wezterm cli send-text --pane-id 170 --no-paste '\r'", True),
        ("printf '\\r' | wezterm cli send-text --pane-id 170 --no-paste", True),
    ],
)
def test_draining_completion_submit_installed_hooks(
    tmp_path: Path, command: str, allowed: bool
) -> None:
    lead = {**processes()[os.getpid()], "model_session_id": "old"}
    run = tmp_path / "run.json"
    run.write_text(
        json.dumps(
            {
                "lifecycle": "running",
                "active_lead": {"model_session_id": "successor"},
                "draining_leads": {
                    "old": {
                        "lead": lead,
                        "completion": {
                            "message": "DRAIN_COMPLETE old",
                            "targets": [{"pane": 170}, {"pane": 171}],
                        },
                    }
                },
            }
        )
    )
    transcript = tmp_path / "old.jsonl"
    transcript.write_text("")
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "old",
            "transcript_path": str(transcript),
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )
    results = [
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/swarm" / hook)],
            input=payload,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PILOT_RUN": str(run),
                "LEAD_AGENT": "p1-pilot-lead",
                "FP_LEAD": "0",
            },
        )
        for hook in ("fp-guard.py", "pilot_guard.py")
    ]
    assert results[1].returncode == 0, results[1].stderr
    assert results[0].returncode == (0 if allowed else 2), results[0].stderr
    if not allowed:
        assert results[0].stderr.splitlines()[0] == (
            "Pilot guard: A draining lead may only report and finalize its owned jobs"
        )


@pytest.mark.parametrize("session", ["active", "old"])
def test_orchestrator_alert_submit_installed_hooks(tmp_path: Path, session: str) -> None:
    run = tmp_path / "run.json"
    run.write_text(
        json.dumps(
            {
                "lifecycle": "paused",
                "dispatch_frozen": True,
                "active_lead": {"model_session_id": "active"},
                "inventory": {
                    "threads": [{"role": "orchestrator", "pane_id": 283, "complete": False}],
                    "sessions": [{"role": "lead", "session_id": "old"}],
                },
            }
        )
    )
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Bash",
            "tool_input": {"command": "wezterm cli send-text --pane-id 283 --no-paste $'\\r'"},
        }
    )
    for hook in ("fp-guard.py", "pilot_guard.py"):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/swarm" / hook)],
            input=payload,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PILOT_RUN": str(run),
                "LEAD_AGENT": "p1-pilot-lead",
                "FP_LEAD": "0",
            },
        )
        assert result.returncode == 0, result.stderr
