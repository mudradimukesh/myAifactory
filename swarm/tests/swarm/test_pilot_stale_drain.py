import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lead() -> Iterator[ModuleType]:
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "pilot_lead", ROOT / "scripts/swarm/pilot_lead.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.pop(0)


@pytest.fixture
def drain(lead: ModuleType, tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], Path]]:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    identity = {**lead.processes()[child.pid], "model_session_id": "drained"}
    child.terminate()
    child.wait(timeout=3)
    transcript = tmp_path / "drained.jsonl"
    transcript.write_text(
        json.dumps({"message": {"id": "m1", "usage": {"input_tokens": 5, "output_tokens": 7}}})
        + "\n"
    )
    report = tmp_path / "drain.md"
    report.write_text("drained\n")
    targets = []
    history = []
    for session, pane in [("old-orch", "1"), ("old-lead", "2")]:
        target = tmp_path / (session + ".jsonl")
        target.write_text(json.dumps({"type": "user", "sessionId": session}) + "\n")
        targets.append({"identity": session, "pane": pane, "transcript_path": str(target)})
        history.append(
            {
                "predecessor": {"model_session_id": session, "pane": pane},
                "receipt": str(tmp_path / (session + "-retirement.json")),
                "completed_at": "2026-10-13T00:00:00Z",
            }
        )
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "lifecycle": "running",
                "active_lead": None,
                "inventory": {
                    "sessions": [
                        {
                            "session_id": "drained",
                            "role": "lead",
                            "transcript_path": str(transcript),
                        }
                    ]
                },
                "reservations": {"builder": {"parent_session_id": "drained", "active_run": None}},
                "lead_scopes": {"drained": ["builder"]},
                "runs": {
                    "builder-1": {
                        "allocation_id": "builder",
                        "parent_session_id": "drained",
                        "complete": True,
                        "usage_status": "known",
                    }
                },
                "consumed": {},
                "retirement_history": history,
                "draining_leads": {
                    "drained": {
                        "lead": identity,
                        "jobs": [],
                        "gates": [],
                        "status": "draining",
                        "completion": {
                            "report": str(report),
                            "report_sha256": lead.sha(report),
                            "message": "DRAIN_COMPLETE drained n1 " + str(report),
                            "targets": targets,
                        },
                    }
                },
            }
        )
    )
    try:
        yield child, path
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


@pytest.mark.parametrize("transcripts_exist", [True, False])
@pytest.mark.parametrize("history_receipt", [True, False])
def test_dead_lead_with_retired_targets_retires(
    lead: ModuleType,
    drain: tuple[subprocess.Popen[bytes], Path],
    monkeypatch: pytest.MonkeyPatch,
    transcripts_exist: bool,
    history_receipt: bool,
) -> None:
    _, path = drain
    before = json.loads(path.read_text())
    if not history_receipt:
        for retirement in before["retirement_history"]:
            del retirement["receipt"]
        path.write_text(json.dumps(before))
    if not transcripts_exist:
        for target in before["draining_leads"]["drained"]["completion"]["targets"]:
            Path(target["transcript_path"]).unlink()
    monkeypatch.setenv("PILOT_RUN", str(path))
    monkeypatch.setattr(sys, "argv", ["pilot_lead.py", "--drain-retire", "drained"])
    try:
        lead.main()
    except ValueError as error:
        pytest.fail(f"Supported retirement refused a dead lead with retired targets: {error}")
    saved = json.loads(path.read_text())
    assert saved["draining_leads"] == {}
    receipt_path = path.parent / "handoff/lead-drained-drain-retirement.json"
    receipt = json.loads(receipt_path.read_text())
    assert saved["drain_history"][-1]["receipt"] == str(receipt_path)
    assert receipt["signals"] == receipt["remaining_processes"] == []
    assert receipt["final_usage"]["new_tokens"] == 12
    assert not receipt["exited_without_completion"]
    assert {row["identity"] for row in receipt["notification_receipts"]} == {"old-orch", "old-lead"}
    assert all(row["retired"] for row in receipt["notification_receipts"])
    assert all(
        row["timestamp"] == "2026-10-13T00:00:00Z" for row in receipt["notification_receipts"]
    )
    assert {row["retirement_receipt"] for row in receipt["notification_receipts"]} == {
        row.get("receipt") for row in before["retirement_history"]
    }
    assert saved["inventory"]["sessions"] == [
        {**before["inventory"]["sessions"][0], "complete": True}
    ]
    for key in ("active_lead", "reservations", "lead_scopes", "retirement_history"):
        assert saved[key] == before[key]
    assert saved["runs"]["builder-1"] == before["runs"]["builder-1"]


def test_dead_lead_without_completion_retires(
    lead: ModuleType, drain: tuple[subprocess.Popen[bytes], Path]
) -> None:
    _, path = drain
    state = json.loads(path.read_text())
    del state["draining_leads"]["drained"]["completion"]
    path.write_text(json.dumps(state))
    lead.retire_draining(path, "drained")
    saved = json.loads(path.read_text())
    receipt = json.loads((path.parent / "handoff/lead-drained-drain-retirement.json").read_text())
    assert saved["draining_leads"] == {}
    assert saved["drain_history"][-1]["retirement"]["exited_without_completion"]
    assert receipt["exited_without_completion"]
    assert receipt["notification_receipts"] == []
    assert receipt["final_usage"]["new_tokens"] == 12


@pytest.mark.parametrize("target_alive", [False, True])
def test_registered_target_retired_through_drain_history(
    lead: ModuleType, drain: tuple[subprocess.Popen[bytes], Path], target_alive: bool
) -> None:
    _, path = drain
    state = json.loads(path.read_text())
    original = state["draining_leads"]["drained"]
    target = original["completion"]["targets"][1]
    target_transcript = Path(target["transcript_path"])
    target_transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "old-lead",
                "message": {"id": "target-usage", "usage": {"input_tokens": 2, "output_tokens": 3}},
            }
        )
        + "\n"
    )
    state["inventory"]["sessions"].append(
        {
            "session_id": "old-lead",
            "role": "lead",
            "pid": original["lead"]["pid"],
            "pane_id": "2",
            "transcript_path": str(target_transcript),
        }
    )
    state["retirement_history"] = state["retirement_history"][:1]
    state["draining_leads"]["old-lead"] = {
        "lead": {**original["lead"], "model_session_id": "old-lead", "pane": "2"},
        "jobs": [],
        "gates": [],
        "status": "draining",
        "completion": {
            **original["completion"],
            "targets": original["completion"]["targets"][:1],
            "message": "DRAIN_COMPLETE old-lead n2 " + original["completion"]["report"],
        },
    }
    path.write_text(json.dumps(state))
    retired = subprocess.run(
        [sys.executable, str(ROOT / "scripts/swarm/pilot_lead.py"), "--drain-retire", "old-lead"],
        env={**os.environ, "PILOT_RUN": str(path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert retired.returncode == 0, retired.stderr
    state = json.loads(path.read_text())
    target_history = state["drain_history"][-1]
    assert target_history["lead"]["model_session_id"] == "old-lead"
    assert all(
        r["predecessor"]["model_session_id"] != "old-lead" for r in state["retirement_history"]
    )
    child = None
    try:
        if target_alive:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            target_history["lead"].update(lead.processes()[child.pid])
            path.write_text(json.dumps(state))
        before = path.read_bytes()
        target_before = target_transcript.read_bytes()
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/swarm/pilot_lead.py"),
                "--drain-retire",
                "drained",
            ],
            env={**os.environ, "PILOT_RUN": str(path)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert target_transcript.read_bytes() == target_before
        receipt_path = path.parent / "handoff/lead-drained-drain-retirement.json"
        if target_alive:
            assert result.returncode == 2
            assert (
                result.stderr.strip()
                == "REFUSED: Native completion notification receipt is missing"
            )
            assert path.read_bytes() == before
            assert not receipt_path.exists()
            assert child is not None and child.poll() is None
        else:
            assert result.returncode == 0, result.stderr
            receipt = json.loads(receipt_path.read_text())
            assert receipt["notification_receipts"][1] == {
                "identity": "old-lead",
                "retired": True,
                "retirement_receipt": target_history["receipt"],
                "timestamp": target_history["retired_at"],
            }
            saved = json.loads(path.read_text())
            assert saved["draining_leads"] == {}
            assert saved["drain_history"][0] == target_history
            assert saved["retirement_history"] == state["retirement_history"]
            assert saved["inventory"]["sessions"][1] == state["inventory"]["sessions"][1]
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=3)


@pytest.mark.parametrize(
    ("completion", "blocker"),
    [
        (completion, blocker)
        for completion in [True, False]
        for blocker in ["alive", "incomplete", "unknown", "missing_usage", "reservation"]
    ]
    + [(True, "live_target")],
)
def test_refusal_preserves_ledger(
    lead: ModuleType,
    drain: tuple[subprocess.Popen[bytes], Path],
    completion: bool,
    blocker: str,
) -> None:
    _, path = drain
    state = json.loads(path.read_text())
    if not completion:
        del state["draining_leads"]["drained"]["completion"]
    child = None
    if blocker == "alive":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        state["draining_leads"]["drained"]["lead"] = {
            **lead.processes()[child.pid],
            "model_session_id": "drained",
        }
    elif blocker == "incomplete":
        state["runs"]["builder-1"]["complete"] = False
    elif blocker == "unknown":
        state["runs"]["builder-1"]["usage_status"] = "unknown"
    elif blocker == "missing_usage":
        del state["runs"]["builder-1"]["usage_status"]
    elif blocker == "reservation":
        state["reservations"]["builder"]["active_run"] = "builder-1"
    else:
        state["retirement_history"] = []
        state["inventory"]["sessions"].append(
            {"session_id": "old-lead", "role": "lead", "complete": False}
        )
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    try:
        with pytest.raises(ValueError):
            lead.retire_draining(path, "drained")
        assert path.read_bytes() == before
        assert not (path.parent / "handoff/lead-drained-drain-retirement.json").exists()
        if child is not None:
            assert child.poll() is None
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=3)


@pytest.mark.parametrize("ownership", ["run", "scope"])
def test_final_accounting_checks_owned_work(
    lead: ModuleType, drain: tuple[subprocess.Popen[bytes], Path], ownership: str
) -> None:
    _, path = drain
    state = json.loads(path.read_text())
    if ownership == "run":
        state["reservations"] = {}
        state["runs"]["builder-1"]["complete"] = False
    else:
        state["reservations"]["builder"] = {
            "worktree": str(path.parent / "builder"),
            "active_run": "builder-1",
        }
        state["draining_leads"]["drained"]["scopes"] = [str(path.parent)]
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        lead.retire_draining(path, "drained")
    assert path.read_bytes() == before


def test_queued_command_is_notification_receipt(
    lead: ModuleType, drain: tuple[subprocess.Popen[bytes], Path]
) -> None:
    _, path = drain
    state = json.loads(path.read_text())
    completion = state["draining_leads"]["drained"]["completion"]
    for target in completion["targets"]:
        Path(target["transcript_path"]).write_text(
            json.dumps(
                {
                    "type": "attachment",
                    "sessionId": target["identity"],
                    "uuid": "queued-" + target["identity"],
                    "attachment": {
                        "type": "queued_command",
                        "commandMode": "prompt",
                        "prompt": completion["message"],
                    },
                }
            )
            + "\n"
        )
    lead.retire_draining(path, "drained")
    saved = json.loads(path.read_text())
    assert {
        row["receipt_id"]
        for row in saved["drain_history"][-1]["retirement"]["notification_receipts"]
    } == {"queued-old-orch", "queued-old-lead"}
