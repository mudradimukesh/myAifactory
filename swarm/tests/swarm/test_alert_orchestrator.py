import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/swarm/alert-orchestrator.sh"

# A fake wezterm: list, get-text and send-text act on a pane screen kept in files.
# MODE=deliver records each submitted message in the transcript; MODE=ignore leaves
# the message in the composer, as when Enter is lost.
WEZTERM = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
d = Path(os.environ["FAKE"])
screen, composer = d / "screen", d / "composer"
args = sys.argv[1:]
if args[:2] == ["cli", "list"]:
    print(json.dumps([{"pane_id": 5}]))
elif args[:2] == ["cli", "get-text"]:
    border = chr(0x2500) * 40
    box = chr(0x276F) + " " + composer.read_text()
    print(screen.read_text(), border, box, border, "  status", sep="\n")
elif args[:2] == ["cli", "send-text"]:
    text = args[-1]
    if text == "\r":
        if os.environ["MODE"] == "toolresult":
            # The recipient reads questions.md: the marker lands in a tool result, unsubmitted.
            block = {"type": "tool_result", "content": composer.read_text()}
            row = {"type": "user", "message": {"content": [block]}}
            with (d / "transcript.jsonl").open("a") as t:
                t.write(json.dumps(row) + "\n")
        if os.environ["MODE"] == "deliver" and composer.read_text():
            row = {"type": "user", "message": {"content": composer.read_text()}}
            with (d / "transcript.jsonl").open("a") as t:
                t.write(json.dumps(row) + "\n")
            screen.write_text(screen.read_text() + "\n> " + composer.read_text())
            composer.write_text("")
    else:
        composer.write_text(composer.read_text() + text)
"""


@pytest.fixture
def pane(tmp_path):
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "screen").write_text("history")
    (fake / "composer").write_text("")
    (fake / "transcript.jsonl").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "wezterm").write_text(WEZTERM)
    (bin_dir / "wezterm").chmod(0o755)
    run = tmp_path / "run.json"
    run.write_text(
        json.dumps(
            {
                "active_lead": {"pane": "5", "model_session_id": "lead"},
                "inventory": {
                    "threads": [
                        {
                            "role": "orchestrator",
                            "pane_id": 5,
                            "transcript_path": str(fake / "transcript.jsonl"),
                        }
                    ],
                    "sessions": [
                        {"session_id": "lead", "transcript_path": str(fake / "transcript.jsonl")}
                    ],
                },
            }
        )
    )
    (tmp_path / "questions.md").write_text("")

    def send(*args, mode="deliver"):
        env = dict(
            os.environ,
            PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
            PILOT_RUN=str(run),
            FAKE=str(fake),
            MODE=mode,
            ALERT_WAIT_S="2",
        )
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), *args], env=env, capture_output=True, text=True
        )

    return send, fake, tmp_path


def test_delivery_is_confirmed_from_the_transcript(pane):
    send, fake, art = pane
    result = send("--log", "Q-lead-1: blocked")
    assert result.returncode == 0, result.stderr
    marker = result.stdout.split("(alert ")[1].rstrip(")\n")
    assert marker in (fake / "transcript.jsonl").read_text()
    assert (art / "questions.md").read_text().startswith("Q-lead-1: blocked (alert ")
    assert send("--target", "lead", "RULING1a: go").returncode == 0


def test_unsent_draft_is_preserved_and_nothing_logged(pane):
    send, fake, art = pane
    (fake / "composer").write_text("user is typing")
    result = send("--log", "Q-lead-1: blocked")
    assert result.returncode == 3
    assert (fake / "composer").read_text() == "user is typing"
    assert (art / "questions.md").read_text() == ""


def test_ignored_enter_and_stale_history_are_not_delivery(pane):
    send, fake, _ = pane
    (fake / "screen").write_text("history Q-lead-1: blocked")
    result = send("--log", "Q-lead-1: blocked", mode="ignore")
    assert result.returncode == 1
    assert "retry without --log" in result.stderr
    assert (fake / "transcript.jsonl").read_text() == ""


def test_closed_pane_sends_nothing(pane):
    send, fake, art = pane
    run = art / "run.json"
    state = json.loads(run.read_text())
    state["inventory"]["threads"][0]["pane_id"] = 9
    run.write_text(json.dumps(state))
    result = send("Q-lead-1: blocked")
    assert result.returncode == 1
    assert "not open" in result.stderr
    assert (fake / "composer").read_text() == ""


def test_multiline_draft_with_empty_first_line_is_preserved(pane):
    send, fake, art = pane
    (fake / "composer").write_text("\nunrelated second line")
    result = send("--log", "Q-lead-1: blocked")
    assert result.returncode == 3
    assert (fake / "composer").read_text() == "\nunrelated second line"
    assert (art / "questions.md").read_text() == ""


def test_marker_in_tool_result_or_old_history_is_not_delivery(pane):
    send, fake, _ = pane
    old = {"type": "user", "message": {"content": "Q-lead-1: blocked (alert 00000000)"}}
    (fake / "transcript.jsonl").write_text(json.dumps(old) + "\n")
    result = send("Q-lead-1: blocked", mode="toolresult")
    assert result.returncode == 1
    assert "not confirmed" in result.stderr


def test_missing_transcript_sends_nothing(pane):
    send, fake, art = pane
    run = art / "run.json"
    state = json.loads(run.read_text())
    del state["inventory"]["threads"][0]["transcript_path"]
    run.write_text(json.dumps(state))
    result = send("Q-lead-1: blocked")
    assert result.returncode == 1
    assert "cannot be confirmed" in result.stderr
    assert (fake / "composer").read_text() == ""
