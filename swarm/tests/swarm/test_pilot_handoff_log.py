import errno
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from io import TextIOBase
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import IO

import pytest

from tests.swarm.test_pilot_handoff import lead as lead


@pytest.fixture
def handoff(
    lead: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, str]]:
    work_state = tmp_path / "state.md"
    work_state.write_bytes(b"complete fixture state\r\n")
    snapshot = tmp_path / "handoff/t.md"
    snapshot.parent.mkdir()
    snapshot.write_bytes(work_state.read_bytes())
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "handoff": {"token": "t"},
                "dispatch_frozen": True,
                "work_state_path": str(work_state),
            }
        )
    )

    def transact(state_path: Path, actor: str, operation: str, data: dict[str, object]) -> None:
        assert actor == "spawner"
        assert operation == "handoff-start"
        state = json.loads(state_path.read_text())
        state["handoff"].update(data)
        state_path.write_text(json.dumps(state, indent=2) + "\n")

    def spawn(command: list[str], **kwargs: object) -> SimpleNamespace:
        assert command[-2:] == ["--controller", "t"]
        output = kwargs["stdout"]
        assert isinstance(output, TextIOBase)
        output.write("fixture controller\n")
        return SimpleNamespace(pid=456)

    monkeypatch.setattr(lead, "originating_lead", lambda _: {"pid": 123})
    monkeypatch.setattr(lead, "drain_jobs", lambda *args: None)
    monkeypatch.setattr(lead, "capture", lambda *args: {})
    monkeypatch.setattr(lead, "processes", lambda: {})
    monkeypatch.setattr(lead, "transact", transact)
    monkeypatch.setattr(lead.subprocess, "Popen", spawn)
    return path, {"REPO": str(tmp_path), "LEAD_LOG": str(tmp_path / "lead.log")}


def test_log_open_failure_preserves_run_bytes(
    lead: ModuleType,
    handoff: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, env = handoff
    log = path.parent / "handoff/t-controller.log"
    before = path.read_bytes()
    original_open = Path.open

    def fail_open(
        file: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[str] | IO[bytes]:
        if file == log:
            assert mode == "x"
            raise PermissionError(errno.EACCES, "fixture log open denied", str(log))
        return original_open(file, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(PermissionError) as refused:
        lead.start_handoff(path, env, "t")
    assert refused.value.filename == str(log)
    assert path.read_bytes() == before, "HL-ORDER: log open failure changed run.json"
    assert not log.exists()


def test_missing_controller_identity_refuses_without_changes(
    lead: ModuleType, handoff: tuple[Path, dict[str, str]]
) -> None:
    path, env = handoff
    log = path.parent / "handoff/t-controller.log"
    log.write_bytes(b"REFUSED: Usage is unknown for an inventoried run\r\n")
    before = path.read_bytes()
    logs = {file.name: file.read_bytes() for file in log.parent.iterdir()}
    with pytest.raises((ValueError, OSError)) as refused:
        lead.start_handoff(path, env, "t")
    assert path.read_bytes() == before, "HL-LIVE: refusal changed run.json"
    assert isinstance(refused.value, ValueError), "HL-LIVE: controller refusal is not explicit"
    assert "controller" in str(refused.value).lower()
    assert str(log) in str(refused.value)
    assert {file.name: file.read_bytes() for file in log.parent.iterdir()} == logs


@pytest.mark.parametrize("archives", [(), (1, 3)], ids=["no_archives", "number_gap"])
def test_exited_controller_log_is_archived_and_retry_proceeds(
    lead: ModuleType,
    handoff: tuple[Path, dict[str, str]],
    archives: tuple[int, ...],
) -> None:
    path, env = handoff
    state = json.loads(path.read_text())
    state["handoff"]["controller_pid"] = 456
    path.write_text(json.dumps(state))
    log = path.parent / "handoff/t-controller.log"
    old = b"REFUSED: Usage is unknown for an inventoried run\r\n\x00"
    log.write_bytes(old)
    preserved = {}
    for number in archives:
        archive = log.with_name(f"t-controller-{number}.log")
        archive.write_bytes(f"earlier controller {number}\r\n".encode())
        preserved[archive] = archive.read_bytes()
    refusal = None
    try:
        lead.start_handoff(path, env, "t")
    except (ValueError, OSError) as error:
        refusal = error
    assert refusal is None, f"HL-RETRY: exited controller blocked retry: {refusal}"
    number = 2 if archives else 1
    assert log.with_name(f"t-controller-{number}.log").read_bytes() == old
    assert log.read_bytes() == b"fixture controller\n"
    assert {archive: archive.read_bytes() for archive in preserved} == preserved
    assert json.loads(path.read_text())["handoff"]["snapshot_path"] == str(log.with_name("t.md"))


def test_two_retries_archive_logs_in_order(
    lead: ModuleType, handoff: tuple[Path, dict[str, str]]
) -> None:
    path, env = handoff
    state = json.loads(path.read_text())
    state["handoff"]["controller_pid"] = 456
    path.write_text(json.dumps(state))
    log = path.parent / "handoff/t-controller.log"
    first = b"first refused controller\r\n"
    second = b"second refused controller\r\n"
    for content in (first, second):
        log.write_bytes(content)
        refusal = None
        try:
            lead.start_handoff(path, env, "t")
        except (ValueError, OSError) as error:
            refusal = error
        assert refusal is None, f"HL-RETRY: exited controller blocked retry: {refusal}"
        assert log.read_bytes() == b"fixture controller\n"
    assert log.with_name("t-controller-1.log").read_bytes() == first
    assert log.with_name("t-controller-2.log").read_bytes() == second


def test_live_controller_refuses_without_changes(
    lead: ModuleType,
    handoff: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, env = handoff
    state = json.loads(path.read_text())
    state["handoff"]["controller_pid"] = 456
    path.write_text(json.dumps(state))
    monkeypatch.setattr(
        lead,
        "processes",
        lambda: {
            456: {
                "pid": 456,
                "ppid": 123,
                "pgid": 456,
                "os_session": 0,
                "started_at": "fixture start",
                "command": "python pilot_lead.py --controller t",
                "status": "S",
            }
        },
    )
    log = path.parent / "handoff/t-controller.log"
    log.write_bytes(b"running controller\r\n")
    before = path.read_bytes()
    logs = {file.name: file.read_bytes() for file in log.parent.iterdir()}
    with pytest.raises((ValueError, OSError)) as refused:
        lead.start_handoff(path, env, "t")
    assert path.read_bytes() == before, "HL-LIVE: refusal changed run.json"
    assert isinstance(refused.value, ValueError), "HL-LIVE: controller refusal is not explicit"
    assert "controller" in str(refused.value).lower()
    assert "456" in str(refused.value)
    assert str(log) in str(refused.value)
    assert {file.name: file.read_bytes() for file in log.parent.iterdir()} == logs


@pytest.fixture
def production_handoff(
    lead: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, dict[str, str], list[subprocess.Popen[bytes]]]]:
    predecessor = {**lead.processes()[os.getpid()], "model_session_id": "predecessor"}
    work_state = tmp_path / "state.md"
    work_state.write_bytes(b"complete production-path state\r\n")
    snapshot = tmp_path / "handoff/t.md"
    snapshot.parent.mkdir()
    snapshot.write_bytes(work_state.read_bytes())
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "lifecycle": "running",
                "active_lead": predecessor,
                "handoff": {"token": "t", "predecessor": predecessor},
                "dispatch_frozen": True,
                "work_state_path": str(work_state),
                "reservations": {},
                "inventory": {"sessions": []},
            }
        )
    )
    children: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def spawn(
        command: list[str],
        *,
        env: dict[str, str],
        stdin: int,
        stdout: IO[str],
        stderr: int,
        start_new_session: bool,
    ) -> subprocess.Popen[bytes]:
        assert command[-2:] == ["--controller", "t"]
        ready = tmp_path / f"controller-{len(children)}-ready"
        child = popen(
            [
                sys.executable,
                "-c",
                "import pathlib, sys, time; "
                "sys.stdout.buffer.write(b'REFUSED: fixture controller\\r\\n\\x00'); "
                "sys.stdout.buffer.flush(); pathlib.Path(sys.argv[1]).touch(); time.sleep(60)",
                str(ready),
            ],
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=start_new_session,
        )
        children.append(child)
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "Fixture controller did not write its log"
        return child

    monkeypatch.setattr(
        lead,
        "subprocess",
        SimpleNamespace(Popen=spawn, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT),
    )
    env = {**os.environ, "REPO": str(tmp_path), "LEAD_LOG": str(tmp_path / "lead.log")}
    try:
        yield path, env, children
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


def test_start_records_spawned_controller_identity_through_transact(
    lead: ModuleType,
    production_handoff: tuple[Path, dict[str, str], list[subprocess.Popen[bytes]]],
) -> None:
    path, env, children = production_handoff
    lead.start_handoff(path, env, "t")
    transfer = json.loads(path.read_text())["handoff"]
    assert transfer.get("controller_pid") == children[0].pid, (
        "HL-CR-1 item 1: production handoff-start did not record the spawned controller PID"
    )
    live = lead.processes()[children[0].pid]
    assert lead.same_identity(transfer.get("controller", {}), live), (
        "HL-CR-1 item 1: controller identity must include "
        "pid, pgid, os_session, started_at, command"
    )


def test_production_controller_exit_allows_retry_without_injected_pid(
    lead: ModuleType,
    production_handoff: tuple[Path, dict[str, str], list[subprocess.Popen[bytes]]],
) -> None:
    path, env, children = production_handoff
    lead.start_handoff(path, env, "t")
    log = path.parent / "handoff/t-controller.log"
    previous = log.read_bytes()
    children[0].terminate()
    children[0].wait(timeout=5)
    lead.transact(path, "spawner", "handoff-abort", {"token": "t", "reason": "controller exited"})
    refusal = None
    try:
        lead.start_handoff(path, env, "t")
    except (ValueError, OSError) as error:
        refusal = error
    assert refusal is None, (
        f"HL-CR-1 item 1: production controller exit still blocks retry: {refusal}"
    )
    assert log.with_name("t-controller-1.log").read_bytes() == previous
    assert log.read_bytes() == previous
    transfer = json.loads(path.read_text())["handoff"]
    assert transfer.get("controller_pid") == children[1].pid
    assert lead.same_identity(transfer.get("controller", {}), lead.processes()[children[1].pid])


def test_production_controller_live_retry_preserves_state_and_logs(
    lead: ModuleType,
    production_handoff: tuple[Path, dict[str, str], list[subprocess.Popen[bytes]]],
) -> None:
    path, env, children = production_handoff
    lead.start_handoff(path, env, "t")
    before = path.read_bytes()
    log = path.parent / "handoff/t-controller.log"
    logs = {file.name: file.read_bytes() for file in log.parent.iterdir()}
    with pytest.raises(ValueError) as refused:
        lead.start_handoff(path, env, "t")
    assert str(children[0].pid) in str(refused.value), (
        "HL-CR-1 item 1: live-controller refusal did not name the production controller"
    )
    assert str(log) in str(refused.value)
    assert path.read_bytes() == before
    assert {file.name: file.read_bytes() for file in log.parent.iterdir()} == logs
    assert len(children) == 1
    assert children[0].poll() is None


@pytest.mark.parametrize("restriction", ["pause", "lifecycle", "blocker"])
def test_refused_transact_can_retry_same_token(
    lead: ModuleType,
    production_handoff: tuple[Path, dict[str, str], list[subprocess.Popen[bytes]]],
    restriction: str,
) -> None:
    path, env, children = production_handoff
    permitted = path.read_bytes()
    pause = path.with_name("PAUSE")
    state = json.loads(permitted)
    if restriction == "pause":
        pause.write_text("Pause fixture launches\n")
        message = "PAUSE exists"
    elif restriction == "lifecycle":
        state["lifecycle"] = "paused"
        path.write_text(json.dumps(state))
        message = "Lifecycle does not permit work"
    else:
        state["blockers"] = [{"id": "blocked-launch", "status": "open", "actions": ["launch:lead"]}]
        path.write_text(json.dumps(state))
        snapshot = path.parent / "handoff/t.md"
        snapshot.write_bytes(snapshot.read_bytes() + b"\nblocked-launch\n")
        message = "Blocked by blocked-launch"
    before = path.read_bytes()
    with pytest.raises(ValueError, match=message):
        lead.start_handoff(path, env, "t")
    assert path.read_bytes() == before
    assert children == []
    log = path.parent / "handoff/t-controller.log"
    refused_logs = {file.name: file.read_bytes() for file in log.parent.glob("*-controller*.log")}
    if restriction == "pause":
        pause.unlink()
    else:
        path.write_bytes(permitted)
    refusal = None
    try:
        lead.start_handoff(path, env, "t")
    except (ValueError, OSError) as error:
        refusal = error
    assert refusal is None, (
        f"HL-CR-1 item 2: refused transact left the token permanently blocked: {refusal}"
    )
    assert len(children) == 1
    assert log.read_bytes() == b"REFUSED: fixture controller\r\n\x00"
    for name, content in refused_logs.items():
        archives = list(log.parent.glob("t-controller-[0-9]*.log"))
        assert any(archive.read_bytes() == content for archive in archives), (
            f"HL-CR-1 item 2: retry deleted or truncated the refused transaction log {name}"
        )
