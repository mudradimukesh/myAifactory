import hashlib
import json
import os
import re
import runpy
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest
import test_task_env as base

from scripts.swarm import task_env

Environment = tuple[Path, dict[str, str], list[str], list[Path], int]


@contextmanager
def allocation_env(
    tmp_path: Path, count: int = 2
) -> Iterator[tuple[Path, dict[str, str], list[str], int]]:
    with base.allocation_env(tmp_path, count) as result:
        art, env, names, occupied = result
        assert isinstance(art, Path)
        assert isinstance(env, dict)
        checked_env = {task_env.string(key): task_env.string(value) for key, value in env.items()}
        assert isinstance(names, list)
        checked_names = [task_env.string(name) for name in names]
        assert isinstance(occupied, int)
        yield art, checked_env, checked_names, occupied


def invoke(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    result = base.invoke(env, *args)
    assert isinstance(result, subprocess.CompletedProcess)
    assert isinstance(result.stdout, str)
    assert isinstance(result.stderr, str)
    assert isinstance(result.returncode, int)
    return subprocess.CompletedProcess(result.args, result.returncode, result.stdout, result.stderr)


def successful(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    result = base.successful(env, *args)
    assert isinstance(result, subprocess.CompletedProcess)
    assert isinstance(result.stdout, str)
    assert isinstance(result.stderr, str)
    assert isinstance(result.returncode, int)
    return subprocess.CompletedProcess(result.args, result.returncode, result.stdout, result.stderr)


def sql(database: str, statement: str) -> str:
    result = base.sql(database, statement)
    assert isinstance(result, str)
    return result


def alive(pid: int) -> bool:
    result = base.alive(pid)
    assert isinstance(result, bool)
    return result


def wait_dead(pids: Iterable[int]) -> None:
    result = base.wait_dead(pids)
    assert result is None


def fetch(url: str) -> tuple[int, bytes]:
    result = base.fetch(url)
    assert isinstance(result, tuple) and len(result) == 2
    status, body = result
    assert isinstance(status, int)
    assert isinstance(body, bytes)
    return status, body


def worktree(path: Path, *, dependencies: bool = True) -> Path:
    path.mkdir(parents=True)
    for name in ("src", "alembic", "alembic.ini", "config", "corpus", ".venv"):
        target = base.ROOT / name
        if target.exists():
            (path / name).symlink_to(target, target_is_directory=target.is_dir())
    web = path / "web"
    shutil.copytree(
        base.ROOT / "web",
        web,
        ignore=shutil.ignore_patterns("node_modules", "dist", "test-results", "playwright-report"),
    )
    fixtures = (base.ROOT / "tests/fixtures").resolve()
    for source in (base.ROOT / "web/src").rglob("*"):
        if source.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        for imported in re.findall(r"(?:from\s+|import\s*)[\"']([^\"']+)[\"']", source.read_text()):
            target = (source.parent / imported).resolve()
            if target.is_relative_to(fixtures) and target.is_file():
                destination = path / "tests/fixtures" / target.relative_to(fixtures)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, destination)
    if dependencies:
        (web / "node_modules").symlink_to(base.ROOT / "web/node_modules", target_is_directory=True)
    return path.resolve()


@pytest.fixture
def units(tmp_path: Path) -> Iterator[Environment]:
    with allocation_env(tmp_path, 2) as (art, env, names, occupied):
        basename = "unit." + "continuation_" * 6
        roots = [worktree(tmp_path / side / basename) for side in ("left", "right")]
        run = Path(env["PILOT_RUN"])
        run.write_text(
            json.dumps(
                {
                    "reservations": {
                        name: {"worktree": str(root), "released": False, "active_run": None}
                        for name, root in zip(names, roots, strict=True)
                    }
                }
            )
        )
        try:
            yield art, env, names, roots, occupied
        finally:
            for name in names:
                successful(env, "down", name)


def up(env: dict[str, str], name: str, root: Path) -> task_env.Receipt:
    result = successful(env, "up", name, str(root))
    assert len(result.stdout.strip().splitlines()) == 1, "TC: up must emit one JSON line"
    item = task_env.receipt(json.loads(result.stdout))
    assert item.worktree == str(root.resolve())
    assert set(item.ports) == base.SERVICES
    assert set(item.pids) == base.SERVICES
    assert all(alive(pid) for pid in item.pids.values())
    assert fetch(item.exports["INTERIOR_JWKS_URL"])[0] == 200
    assert fetch(f"http://127.0.0.1:{item.ports['web']}/bench")[0] == 200
    return item


def database(item: task_env.Receipt) -> str:
    return urlparse(item.database_url).path.removeprefix("/")


def registry(art: Path) -> dict[str, task_env.Receipt]:
    data = task_env.mapping(json.loads((art / "task-env/registry.json").read_text()))
    return {key: task_env.receipt(value) for key, value in data.items()}


def same_unit(env: dict[str, str], names: list[str], root: Path) -> None:
    run = Path(env["PILOT_RUN"])
    state = json.loads(run.read_text())
    for name in names:
        state["reservations"][name]["worktree"] = str(root)
    run.write_text(json.dumps(state))


def test_tc1_different_worktrees_have_disjoint_unit_resources(units: Environment) -> None:
    art, env, names, roots, occupied = units
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(lambda pair: up(env, *pair), zip(names, roots, strict=True)))
    first, second = receipts
    assert set(first.ports.values()).isdisjoint(second.ports.values())
    assert occupied not in set(first.ports.values()) | set(second.ports.values())
    assert set(first.pids.values()).isdisjoint(second.pids.values())
    assert first.database_url != second.database_url
    saved = registry(art)
    assert len(saved) == 2
    assert set(saved).isdisjoint(names), "TC-1: registry keys still depend on allocation ids"
    for item in receipts:
        key = next(key for key, value in saved.items() if value.worktree == item.worktree)
        assert re.fullmatch(r"[a-zA-Z0-9_]+", key), "TC-1: unit key is not sanitized"
        assert database(item) == f"p1_{key}", "TC-1: database does not use the unit key"
        assert len(database(item).encode()) <= 63
        assert sql(database(item), "SELECT current_database()") == database(item)
        assert task_env.receipt(json.loads((art / "task-env" / f"{key}.json").read_text())) == item
        for service, pid in item.pids.items():
            assert int((art / "task-env" / key / f"{service}.pid").read_text()) == pid


def test_tc2_continuation_reuses_live_unit_and_database_row(units: Environment) -> None:
    art, env, names, roots, _ = units
    same_unit(env, names, roots[0])
    first = up(env, names[0], roots[0])
    sql(
        database(first), "CREATE TABLE tc_survives (n integer); INSERT INTO tc_survives VALUES (37)"
    )
    files = {
        path: path.read_bytes()
        for path in (art / "task-env").rglob("*")
        if path.is_file() and path.suffix in {".pid", ".log"}
    }
    second = up(env, names[1], roots[0] / ".." / roots[0].name)
    for actual, expected, field in (
        (second.ports, first.ports, "ports"),
        (second.pids, first.pids, "pids"),
        (second.database_url, first.database_url, "database_url"),
        (second.exports, first.exports, "exports"),
    ):
        assert actual == expected, f"TC-2: continuation changed {field}"
    assert set(second.allocation_ids) == set(names), "TC-2: receipt omits attached allocations"
    assert len(second.allocation_ids) == 2
    assert sql(database(second), "SELECT n FROM tc_survives") == "37"
    assert_tc2_continuation_files(art / "task-env", files)
    saved = registry(art)
    assert len(saved) == 1, "TC-2: same worktree created a second environment"
    key, item = next(iter(saved.items()))
    assert item == second
    assert task_env.receipt(json.loads((art / "task-env" / f"{key}.json").read_text())) == second
    third = up(env, names[1], roots[0])
    assert third == second, "TC-2: repeated continuation is not idempotent"


def assert_tc2_continuation_files(directory: Path, before: dict[Path, bytes]) -> None:
    message = "TC-2: continuation restarted work"
    after = {
        path: path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file() and path.suffix in {".pid", ".log"}
    }
    assert after.keys() == before.keys(), message
    for path, old in before.items():
        new = after[path]
        if path.suffix == ".pid":
            assert new == old, message
        else:
            assert new.startswith(old), message
            assert re.fullmatch(
                rb'(?:INFO:     127\.0\.0\.1:[0-9]+ - "GET '
                rb'(?:/api/me HTTP/1\.1" 401 Unauthorized|/idp/jwks\.json HTTP/1\.1" 200 OK)\n)*',
                new[len(old) :],
            ), message


@pytest.mark.parametrize(
    "appended",
    [
        b"",
        b'INFO:     127.0.0.1:60727 - "GET /api/me HTTP/1.1" 401 Unauthorized\n'
        b'INFO:     127.0.0.1:60728 - "GET /idp/jwks.json HTTP/1.1" 200 OK\n',
    ],
    ids=["empty", "probe-requests"],
)
def test_tc2_file_oracle_accepts_probe_requests(tmp_path: Path, appended: bytes) -> None:
    pid = tmp_path / "api.pid"
    log = tmp_path / "runtime.log"
    before = {pid: b"123\n", log: b"existing log\n"}
    for path, content in before.items():
        path.write_bytes(content)
    log.write_bytes(before[log] + appended)
    assert_tc2_continuation_files(tmp_path, before)


@pytest.mark.parametrize(
    "appended",
    [
        b"INFO:     Started server process [123]\n",
        b'INFO:     127.0.0.1:60727 - "GET /api/me HTTP/1.1" 401 Unauthorized',
        b'INFO:     127.0.0.1:60727 - "GET /bench HTTP/1.1" 200 OK\n',
        b'INFO:     127.0.0.1:60727 - "POST /api/me HTTP/1.1" 401 Unauthorized\n',
    ],
    ids=["startup", "incomplete-request", "other-path", "other-method"],
)
def test_tc2_file_oracle_rejects_non_probe_bytes(tmp_path: Path, appended: bytes) -> None:
    log = tmp_path / "runtime.log"
    before = {log: b"existing log\n"}
    log.write_bytes(before[log] + appended)
    with pytest.raises(AssertionError, match=r"^TC-2: continuation restarted work"):
        assert_tc2_continuation_files(tmp_path, before)


def test_tc2_file_oracle_rejects_changed_pidfile(tmp_path: Path) -> None:
    pid = tmp_path / "api.pid"
    before = {pid: b"123\n"}
    pid.write_bytes(b"124\n")
    with pytest.raises(AssertionError, match=r"^TC-2: continuation restarted work"):
        assert_tc2_continuation_files(tmp_path, before)


@pytest.mark.parametrize(
    "content", [b"existing", b"rewritten log\n"], ids=["truncated", "rewritten"]
)
def test_tc2_file_oracle_rejects_changed_log(tmp_path: Path, content: bytes) -> None:
    log = tmp_path / "runtime.log"
    before = {log: b"existing log\n"}
    log.write_bytes(content)
    with pytest.raises(AssertionError, match=r"^TC-2: continuation restarted work"):
        assert_tc2_continuation_files(tmp_path, before)


@pytest.mark.parametrize("name", ["api.pid", "runtime.log"], ids=["pidfile", "log"])
def test_tc2_file_oracle_rejects_new_file(tmp_path: Path, name: str) -> None:
    directory = tmp_path / "unit"
    directory.mkdir()
    (directory / name).write_bytes(b"")
    with pytest.raises(AssertionError, match=r"^TC-2: continuation restarted work"):
        assert_tc2_continuation_files(tmp_path, {})


@pytest.mark.parametrize("name", ["api.pid", "runtime.log"], ids=["pidfile", "log"])
def test_tc2_file_oracle_rejects_missing_file(tmp_path: Path, name: str) -> None:
    with pytest.raises(AssertionError, match=r"^TC-2: continuation restarted work"):
        assert_tc2_continuation_files(tmp_path, {tmp_path / name: b"existing\n"})


def test_tc1_unit_database_name_survives_allocation_replacement(units: Environment) -> None:
    art, env, names, roots, _ = units
    first = up(env, names[0], roots[0])
    key = next(key for key, item in registry(art).items() if item.worktree == str(roots[0]))
    successful(env, "down", names[0])
    wait_dead(first.pids.values())
    same_unit(env, names, roots[0])
    second = up(env, names[1], roots[0])
    assert second.database_url == first.database_url, "TC-1: database name uses allocation id"
    assert list(registry(art)) == [key], "TC-1: unit key changed with the allocation"


@pytest.mark.parametrize("attach_peer", [False, True], ids=["registered-peer", "attached-peer"])
def test_tc3_down_detaches_one_allocation_and_last_down_stops_unit(
    units: Environment, attach_peer: bool
) -> None:
    art, env, names, roots, _ = units
    same_unit(env, names, roots[0])
    first = up(env, names[0], roots[0])
    if attach_peer:
        up(env, names[1], roots[0])
    successful(env, "down", names[0])
    assert all(alive(pid) for pid in first.pids.values()), "TC-3: down stopped a shared unit"
    assert sql(database(first), "SELECT 1") == "1"
    assert fetch(f"http://127.0.0.1:{first.ports['web']}/bench")[0] == 200
    saved = registry(art)
    assert len(saved) == 1
    item = next(iter(saved.values()))
    assert names[0] not in item.allocation_ids, "TC-3: down did not detach the caller"
    assert names[1] in item.allocation_ids
    successful(env, "down", names[1])
    wait_dead(first.pids.values())
    assert registry(art) == {}
    assert not list((art / "task-env").rglob("*.pid"))
    assert list((art / "task-env").glob("*.json")) == [art / "task-env/registry.json"]
    assert (
        sql("postgres", f"SELECT count(*) FROM pg_database WHERE datname = '{database(first)}'")
        == "0"
    )


@pytest.mark.parametrize(
    ("released", "active_run", "attach_peer"),
    [(False, None, True), (False, None, False), (True, "run-still-active", True)],
    ids=["unreleased", "unattached-reservation", "active-run"],
)
def test_tc3_reap_checks_every_same_worktree_reservation(
    units: Environment, released: bool, active_run: str | None, attach_peer: bool
) -> None:
    art, env, names, roots, _ = units
    same_unit(env, names, roots[0])
    first = up(env, names[0], roots[0])
    if attach_peer:
        up(env, names[1], roots[0])
    run = Path(env["PILOT_RUN"])
    state = json.loads(run.read_text())
    state["reservations"][names[0]]["released"] = True
    state["reservations"][names[1]].update(released=released, active_run=active_run)
    run.write_text(json.dumps(state))
    for pid in set(first.pids.values()):
        os.kill(pid, signal.SIGTERM)
    wait_dead(first.pids.values())
    before = registry(art)
    successful(env, "reap")
    assert registry(art) == before, "TC-3: reap removed a unit with a retained reservation"
    assert sql(database(first), "SELECT 1") == "1"
    state["reservations"][names[1]].update(released=True, active_run=None)
    run.write_text(json.dumps(state))
    successful(env, "reap")
    assert registry(art) == {}, "TC-3: reap retained an all-released dead unit"
    assert not list((art / "task-env").rglob("*.pid"))
    assert (
        sql("postgres", f"SELECT count(*) FROM pg_database WHERE datname = '{database(first)}'")
        == "0"
    )
    successful(env, "reap")
    assert registry(art) == {}


def test_tc3_reap_keeps_live_released_unit_and_other_worktree(units: Environment) -> None:
    art, env, names, roots, _ = units
    first = up(env, names[0], roots[0])
    second = up(env, names[1], roots[1])
    run = Path(env["PILOT_RUN"])
    state = json.loads(run.read_text())
    state["reservations"][names[0]]["released"] = True
    run.write_text(json.dumps(state))
    before = registry(art)
    successful(env, "reap")
    assert registry(art) == before, "TC-3: reap removed a live unit"
    for pid in set(first.pids.values()):
        os.kill(pid, signal.SIGTERM)
    wait_dead(first.pids.values())
    successful(env, "reap")
    saved = registry(art)
    assert len(saved) == 1 and next(iter(saved.values())) == second
    assert all(alive(pid) for pid in second.pids.values())
    assert sql(database(second), "SELECT 1") == "1"


def test_tc4_registered_hyphenated_long_allocation_is_accepted(units: Environment) -> None:
    _, env, names, roots, _ = units
    run = Path(env["PILOT_RUN"])
    state = json.loads(run.read_text())
    original = names[0]
    names[0] = original.replace("_", "-") + "-continuation" * 6
    state["reservations"][names[0]] = state["reservations"].pop(original)
    run.write_text(json.dumps(state))
    item = up(env, names[0], roots[0])
    assert names[0] in item.allocation_ids
    assert len(database(item).encode()) <= 63


@pytest.mark.parametrize("reservation", ["missing", "wrong-worktree"])
def test_tc4_up_refuses_unregistered_or_wrong_worktree_before_start(
    tmp_path: Path, reservation: str
) -> None:
    root = worktree(tmp_path / "unit", dependencies=False)
    art = tmp_path / "art"
    art.mkdir()
    run = art / "run.json"
    name = "tc_unregistered_allocation"
    entries = (
        {}
        if reservation == "missing"
        else {name: {"worktree": str(tmp_path / "other"), "released": False, "active_run": None}}
    )
    run.write_text(json.dumps({"reservations": entries}))
    env = {**os.environ, "PILOT_RUN": str(run), "ART": str(art), "PILOT_ART": str(art)}
    result = invoke(env, "up", name, str(root))
    assert result.returncode != 0, "TC-4: up accepted a missing or mismatched reservation"
    assert "reservation" in result.stderr.lower() or "registered" in result.stderr.lower(), (
        f"TC-4: reservation refusal must precede dependency and service startup: {result.stderr}"
    )
    assert name in result.stderr
    if reservation == "wrong-worktree":
        assert "worktree" in result.stderr.lower()
    assert not list(art.rglob("*.pid"))
    assert not list(art.rglob("runtime.log"))
    if (art / "task-env/registry.json").exists():
        assert not (art / "task-env/registry.json").read_text().strip() or registry(art) == {}


def test_tc5_missing_node_modules_reports_npm_ci_before_any_service(tmp_path: Path) -> None:
    root = worktree(tmp_path / "unit", dependencies=False)
    art = tmp_path / "art"
    art.mkdir()
    run = art / "run.json"
    name = "tc_missing_dependencies"
    run.write_text(
        json.dumps(
            {"reservations": {name: {"worktree": str(root), "released": False, "active_run": None}}}
        )
    )
    env = {**os.environ, "PILOT_RUN": str(run), "ART": str(art), "PILOT_ART": str(art)}
    result = invoke(env, "up", name, str(root))
    assert result.returncode != 0, "TC-5: missing node_modules was accepted"
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1, f"TC-5: expected one actionable stderr line, got {result.stderr}"
    assert str(root / "web/node_modules") in lines[0]
    assert "npm ci" in lines[0] and str(root / "web") in lines[0]
    assert result.stdout == ""
    assert not list(art.rglob("*.pid"))
    assert not list(art.rglob("runtime.log")), "TC-5: startup began before dependency validation"
    if (art / "task-env/registry.json").exists():
        assert not (art / "task-env/registry.json").read_text().strip() or registry(art) == {}


def test_cr2_stop_kills_child_after_group_leader_exits(tmp_path: Path) -> None:
    tool = runpy.run_path(str(base.TOOL))
    ready = tmp_path / "child.pid"
    child_code = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    leader_code = (
        f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child_code!r}])"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        leader.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "CR-2: child did not become ready"
        child = int(ready.read_text())
        assert alive(child) and not alive(leader.pid)
        tool["stop"]({"web": leader.pid}, [leader])
        deadline = time.monotonic() + 3
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(child), "CR-2: child survived after its group leader exited"
    finally:
        with suppress(ProcessLookupError):
            os.killpg(leader.pid, signal.SIGKILL)


def test_cr2_stop_accepts_exited_group_after_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        child.wait(timeout=10)
        denied = MagicMock(side_effect=[None, PermissionError(1, "Operation not permitted")])
        if sys.platform == "darwin":
            monkeypatch.setattr(os, "killpg", denied)
        task_env.stop({"web": child.pid}, [child])
        if sys.platform == "darwin":
            assert denied.call_args_list == [
                ((child.pid, signal.SIGTERM),),
                ((child.pid, 0),),
            ]
            denied.reset_mock()
            denied.side_effect = PermissionError(1, "Operation not permitted")
        task_env.stop({"web": child.pid}, [])
        if sys.platform == "darwin":
            denied.assert_called_once_with(child.pid, signal.SIGTERM)
    finally:
        child.wait(timeout=10)


def test_cr2_stop_rejects_live_group_after_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "child.pid"
    child_code = (
        "import os, time; from pathlib import Path; "
        f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child_code!r}])",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    kill_group = os.killpg
    try:
        leader.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        child = int(ready.read_text())
        assert alive(child) and not alive(leader.pid)
        denied = MagicMock(side_effect=PermissionError(1, "Operation not permitted"))
        monkeypatch.setattr(os, "killpg", denied)
        with pytest.raises(ValueError, match="Owned processes survived shutdown"):
            task_env.stop({"web": leader.pid}, [leader])
        assert alive(child)
        assert ((leader.pid, signal.SIGKILL),) in denied.call_args_list
    finally:
        with suppress(ProcessLookupError):
            kill_group(leader.pid, signal.SIGKILL)


def test_cr3_failed_start_cleanup_preserves_error_and_runs_every_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = runpy.run_path(str(base.TOOL))
    root = worktree(tmp_path / "unit", dependencies=False)
    (root / "web/node_modules").mkdir()
    directory = tmp_path / "task-env"
    directory.mkdir()
    name = "cleanup_unit"
    original = ValueError("CR-3 original startup failure")
    sockets = []
    for port in (41001, 41002, 41003):
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listener.getsockname.return_value = ("127.0.0.1", port)
        sockets.append(listener)
    monkeypatch.setattr(tool["socket"], "socket", MagicMock(side_effect=sockets))
    monkeypatch.setattr(tool["subprocess"], "run", MagicMock())
    child = MagicMock(pid=12345)
    monkeypatch.setattr(tool["subprocess"], "Popen", MagicMock(side_effect=[child, original]))
    stopped = MagicMock(side_effect=ValueError("CR-3 cleanup stop failure"))
    dropped = MagicMock()
    monkeypatch.setitem(tool["start"].__globals__, "stop", stopped)
    monkeypatch.setitem(tool["start"].__globals__, "drop", dropped)
    with pytest.raises(ValueError) as error:
        tool["start"](directory, name, root, {})
    assert error.value is original, "CR-3: cleanup replaced the original startup error"
    stopped.assert_called_once()
    dropped.assert_called_once_with(name)
    assert not list(directory.rglob("*.pid")), "CR-3: cleanup left pidfiles"
    assert not (directory / f"{name}.json").exists()


def test_tc3_down_stops_unit_with_only_released_inactive_peer_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "unit"
    key = "unit_" + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    run = tmp_path / "run.json"
    run.write_text(
        json.dumps(
            {
                "reservations": {
                    "caller": {"worktree": str(root), "released": False, "active_run": None},
                    "peer": {"worktree": str(root), "released": True, "active_run": None},
                }
            }
        )
    )
    directory = tmp_path / "task-env"
    (directory / key).mkdir(parents=True)
    item = task_env.Receipt(
        key,
        str(root.resolve()),
        dict(zip(task_env.SERVICES, (41001, 41002, 41003), strict=True)),
        dict(zip(task_env.SERVICES, (12001, 12002, 12003), strict=True)),
        task_env.database_url(key),
        {},
        ["caller"],
    )
    (directory / f"{key}.json").write_text(json.dumps(asdict(item)))
    (directory / "registry.json").write_text(json.dumps({key: asdict(item)}))
    for service, pid in item.pids.items():
        task_env.pidfile(directory, key, service).write_text(str(pid))
    stopped = MagicMock()
    dropped = MagicMock()
    monkeypatch.setattr(task_env, "stop", stopped)
    monkeypatch.setattr(task_env, "drop", dropped)
    monkeypatch.setenv("PILOT_RUN", str(run))
    monkeypatch.setattr(sys, "argv", [str(base.TOOL), "down", "caller"])
    task_env.main()
    stopped.assert_called_once_with(item.pids, [])
    dropped.assert_called_once_with(key)
    assert json.loads((directory / "registry.json").read_text()) == {}
    assert not (directory / f"{key}.json").exists()
    assert not list(directory.rglob("*.pid"))


def test_tc3_down_stops_unit_with_only_released_inactive_peer(units: Environment) -> None:
    art, env, names, roots, _ = units
    same_unit(env, names, roots[0])
    run = Path(env["PILOT_RUN"])
    state = json.loads(run.read_text())
    state["reservations"][names[1]].update(released=True, active_run=None)
    run.write_text(json.dumps(state))
    first = up(env, names[0], roots[0])
    successful(env, "down", names[0])
    wait_dead(first.pids.values())
    assert registry(art) == {}
    assert not list((art / "task-env").rglob("*.pid"))
    assert list((art / "task-env").glob("*.json")) == [art / "task-env/registry.json"]
    assert (
        sql("postgres", f"SELECT count(*) FROM pg_database WHERE datname = '{database(first)}'")
        == "0"
    )


@pytest.mark.parametrize("prefix", ["", "a" * 60], ids=["short", "long"])
def test_cr5_sanitized_database_names_do_not_collide(prefix: str) -> None:
    original = prefix + "a-b"
    sanitized = prefix + "a_b"
    original_url = task_env.database_url(original)
    sanitized_url = task_env.database_url(sanitized)
    assert original_url != sanitized_url
    digest = hashlib.sha256(original.encode()).hexdigest()[:12]
    assert urlparse(original_url).path.endswith("_" + digest)
    assert len(urlparse(original_url).path.removeprefix("/p1_")) <= 60
    assert len(urlparse(sanitized_url).path.removeprefix("/p1_")) <= 60
    assert task_env.database_url("a_b") == "postgresql://app_runtime@/p1_a_b?host=/tmp"
