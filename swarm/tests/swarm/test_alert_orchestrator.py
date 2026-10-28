import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/swarm/alert-orchestrator.sh"

# A fake wezterm: list, get-text and send-text act on a pane screen kept in files.
# MODE=deliver records each submitted message in the transcript; MODE=ignore leaves
# the message in the composer, as when Enter is lost. UI=codex draws the Codex composer
# and records Codex user message items.
WEZTERM = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
d = Path(os.environ["FAKE"])
screen, composer = d / "screen", d / "composer"
args = sys.argv[1:]
if args[:2] == ["cli", "list"]:
    print(json.dumps([{"pane_id": 5}]))
elif args[:2] == ["cli", "get-text"] and os.environ.get("UI") == "codex":
    dim = "\033[0;2m" if "--escapes" in args else ""
    draft = composer.read_text() or dim + "Ask Codex to do anything"
    status = "  GPT-6-Astra low " + chr(0xB7) + " ~/repo " + chr(0xB7) + " title"
    hint = "  ? for shortcuts" + " " * 20 + "1 warning " + chr(0xB7) + " f2 to view"
    footer = [] if os.environ.get("FOOTER") in ("none", "noprompt") else [status, hint]
    queued = (d / "queue").read_text() if (d / "queue").exists() else ""
    queue = ["\u2022 Queued follow-up inputs"] if queued else []
    queue += ["    " + queued[i : i + 30] for i in range(0, len(queued), 30)]
    queue += ["    shift+left edit last queued message"] if queued else []
    wrap = os.environ.get("WRAP") == "1" and composer.read_text()
    text = draft if not wrap else "\n  ".join(
        draft[i : i + 25] for i in range(0, len(draft), 25)
    )
    prompt = [] if os.environ.get("FOOTER") == "noprompt" else [chr(0x203A) + " " + text]
    print(screen.read_text(), *queue, "", *prompt, "", *footer, sep="\n")
elif args[:2] == ["cli", "get-text"]:
    border = chr(0x2500) * 40
    box = chr(0x276F) + " " + composer.read_text()
    print(screen.read_text(), border, box, border, "  status", sep="\n")
elif args[:2] == ["cli", "send-text"]:
    text = args[-1]
    if text == "\t" and os.environ["MODE"] == "busy-vanish":
        screen.write_text(screen.read_text() + "\n" + composer.read_text())
        composer.write_text("")
    elif text == "\t" and os.environ["MODE"] == "busy":
        (d / "queue").write_text(composer.read_text())
        composer.write_text("")
    elif text == "\t":
        (d / "tabs").write_text("tab")
    elif text == "\r" and os.environ["MODE"] in ("busy", "busy-stuck", "busy-vanish"):
        pass
    elif text == "\r":
        if os.environ["MODE"] == "codex-toolout":
            item = {"type": "function_call_output", "output": composer.read_text()}
            with (d / "transcript.jsonl").open("a") as t:
                t.write(json.dumps({"type": "response_item", "payload": item}) + "\n")
        if os.environ["MODE"] == "toolresult":
            # The recipient reads questions.md: the marker lands in a tool result, unsubmitted.
            block = {"type": "tool_result", "content": composer.read_text()}
            row = {"type": "user", "message": {"content": [block]}}
            with (d / "transcript.jsonl").open("a") as t:
                t.write(json.dumps(row) + "\n")
        if os.environ["MODE"] == "deliver" and composer.read_text():
            row = {"type": "user", "message": {"content": composer.read_text()}}
            if os.environ.get("UI") == "codex":
                block = {"type": "input_text", "text": composer.read_text()}
                item = {"type": "message", "role": "user", "content": [block]}
                row = {"type": "response_item", "payload": item}
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
    codex_dir = fake / ".codex/sessions"
    codex_dir.mkdir(parents=True)
    (codex_dir / "transcript.jsonl").symlink_to(fake / "transcript.jsonl")
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

    def send(*args, mode="deliver", ui="claude", footer="status", wrap="0"):
        state = json.loads(run.read_text())
        thread = state["inventory"]["threads"][0]
        if "transcript_path" in thread:
            name = ".codex/sessions/transcript.jsonl" if ui == "codex" else "transcript.jsonl"
            thread["transcript_path"] = str(fake / name)
            run.write_text(json.dumps(state))
        env = dict(
            os.environ,
            PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
            PILOT_RUN=str(run),
            FAKE=str(fake),
            MODE=mode,
            UI=ui,
            FOOTER=footer,
            WRAP=wrap,
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


def test_codex_orchestrator_delivery_is_confirmed(pane):
    send, fake, art = pane
    result = send("--log", "Lead: pointer", ui="codex")
    assert result.returncode == 0, result.stderr
    marker = result.stdout.split("(alert ")[1].rstrip(")\n")
    row = json.loads((fake / "transcript.jsonl").read_text().splitlines()[-1])
    assert row["type"] == "response_item"
    assert marker in row["payload"]["content"][0]["text"]
    assert (art / "questions.md").read_text().startswith("Lead: pointer (alert ")


@pytest.mark.parametrize("draft", ["user is typing", "\nsecond line only"])
def test_codex_draft_is_preserved_and_nothing_logged(pane, draft):
    send, fake, art = pane
    (fake / "composer").write_text(draft)
    result = send("--log", "Lead: pointer", ui="codex")
    assert result.returncode == 3
    assert (fake / "composer").read_text() == draft
    assert (art / "questions.md").read_text() == ""


@pytest.mark.parametrize("footer", ["none", "noprompt"])
def test_codex_screen_without_composer_prompt_is_unknown(pane, footer):
    send, fake, _ = pane
    result = send("Lead: pointer", ui="codex", footer=footer)
    assert result.returncode == 3
    assert "composer state unknown" in result.stderr
    assert (fake / "composer").read_text() == ""


def test_codex_marker_outside_a_user_message_is_not_delivery(pane):
    send, fake, _ = pane
    result = send("Lead: pointer", ui="codex", mode="codex-toolout")
    rows = (fake / "transcript.jsonl").read_text()
    assert "(alert " in rows
    assert result.returncode == 1
    assert "not confirmed" in result.stderr


def test_busy_codex_orchestrator_queues_the_alert(pane):
    send, fake, art = pane
    result = send("--log", "Lead: pointer that is long enough to wrap", ui="codex", mode="busy")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("queued for orchestrator pane 5 (alert ")
    assert (fake / "composer").read_text() == ""
    assert "(alert " in (fake / "queue").read_text()
    assert (art / "questions.md").read_text().startswith("Lead: pointer that is long")


def test_busy_codex_alert_that_cannot_queue_is_not_confirmed(pane):
    send, fake, _ = pane
    result = send("Lead: pointer", ui="codex", mode="busy-stuck")
    assert result.returncode == 1
    assert "not confirmed" in result.stderr
    assert not (fake / "queue").exists()


@pytest.mark.parametrize("name", ["builder-settings.json", "orchestrator-settings.json"])
def test_claude_panes_launch_without_prompt_suggestions(name):
    settings = json.loads((ROOT / "scripts/swarm" / name).read_text())
    assert settings["promptSuggestionEnabled"] is False


def test_claude_draft_with_copied_codex_prompt_is_preserved(pane):
    send, fake, art = pane
    draft = "notes\n\u203a Ask Codex to do anything\n  GPT-6-Astra low \u00b7 ~/repo \u00b7 t"
    (fake / "composer").write_text(draft)
    result = send("--log", "Lead: pointer")
    assert result.returncode == 3
    assert (fake / "composer").read_text() == draft
    assert (art / "questions.md").read_text() == ""


def test_ignored_enter_on_claude_pane_sends_no_tab(pane):
    send, fake, _ = pane
    result = send("Q-lead-1: blocked", mode="ignore")
    assert result.returncode == 1
    assert not (fake / "tabs").exists()


def test_stale_queue_heading_in_history_is_not_queued(pane):
    send, fake, _ = pane
    (fake / "screen").write_text(
        "Queued follow-up inputs\nold (alert 0)\nshift+left edit last queued message"
    )
    result = send("Lead: pointer", ui="codex", mode="busy-vanish")
    assert "(alert " in (fake / "screen").read_text().splitlines()[-1]
    assert result.returncode == 1
    assert "not confirmed" in result.stderr


@pytest.mark.parametrize(
    "draft",
    [
        "\n  notes \u00b7 text",
        "\n  notes \u00b7 ~/repo",
        "private draft\n\u203a ",
        "Ask Codex to do anything",
        "first line\n\u203a Ask Codex to do anything",
    ],
)
def test_codex_draft_that_looks_like_boundaries_is_preserved(pane, draft):
    send, fake, art = pane
    (fake / "composer").write_text(draft)
    result = send("--log", "Lead: pointer", ui="codex")
    assert result.returncode == 3
    assert (fake / "composer").read_text() == draft
    assert (art / "questions.md").read_text() == ""


def test_history_heading_without_live_queue_is_not_queued(pane):
    send, fake, _ = pane
    (fake / "screen").write_text("Queued follow-up inputs\nold history line")
    result = send("Lead: pointer", ui="codex", mode="busy-vanish")
    assert "(alert " in (fake / "screen").read_text().splitlines()[-1]
    assert result.returncode == 1
    assert "not confirmed" in result.stderr


def test_busy_codex_alert_wrapped_in_the_composer_is_queued(pane):
    send, fake, _ = pane
    message = "Lead: pointer long enough to wrap the marker across composer lines"
    result = send(message, ui="codex", mode="busy", wrap="1")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("queued for orchestrator pane 5 (alert ")
    assert (fake / "composer").read_text() == ""
