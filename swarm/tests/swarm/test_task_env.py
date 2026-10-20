import ast
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts/swarm/task_env.py"
SERVICES = {"api", "jwks", "web"}


def invoke(env, *args):
    assert TOOL.is_file(), "TE: task_env.py CLI is missing"
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def successful(env, *args):
    result = invoke(env, *args)
    assert result.returncode == 0, f"TE: {args}: {result.stdout}\n{result.stderr}"
    return result


def sql(database, statement):
    result = subprocess.run(
        ["psql", "-X", "-h", "/tmp", "-d", database, "-At", "-c", statement],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, f"TE: PostgreSQL: {result.stdout}\n{result.stderr}"
    return result.stdout.strip()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_dead(pids):
    for _ in range(100):
        if not any(alive(pid) for pid in pids):
            return
        time.sleep(0.1)
    assert not any(alive(pid) for pid in pids), "TE: owned processes survived shutdown"


def fetch(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


@contextmanager
def allocation_env(tmp_path, count=2):
    assert TOOL.is_file(), "TE: task_env.py CLI is missing"
    art = tmp_path / "art"
    art.mkdir()
    run = art / "run.json"
    names = [f"te_{uuid4().hex[:12]}_{i}" for i in range(count)]
    state = {"reservations": {name: {"released": False} for name in names}}
    run.write_text(json.dumps(state))
    env = {
        **os.environ,
        "ART": str(art),
        "PILOT_ART": str(art),
        "PILOT_RUN": str(run),
    }
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        try:
            assert sql("postgres", "SELECT 1") == "1"
            yield art, env, names, occupied.getsockname()[1]
        finally:
            for name in names:
                if (art / "task-env" / f"{name}.json").exists():
                    successful(env, "down", name)


def up(art, env, name):
    result = successful(env, "up", name, str(ROOT))
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1, "TE: up must print exactly one JSON line"
    receipt = json.loads(lines[0])
    saved = json.loads((art / "task-env" / f"{name}.json").read_text())
    assert receipt == saved, "TE: stdout and persisted receipt differ"
    assert set(receipt["ports"]) == SERVICES
    assert set(receipt["pids"]) == SERVICES
    assert all(isinstance(port, int) and 0 < port < 65536 for port in receipt["ports"].values())
    assert all(isinstance(pid, int) and pid > 1 for pid in receipt["pids"].values())
    assert all(alive(pid) for pid in receipt["pids"].values())
    url = urlparse(receipt["database_url"])
    assert url.scheme in {"postgres", "postgresql", "postgresql+psycopg"}
    assert url.hostname in {None, "localhost", "127.0.0.1"}
    assert url.path == f"/p1_{name}"
    assert sql(f"p1_{name}", "SELECT current_database()") == f"p1_{name}"
    exports = receipt["exports"]
    assert exports["INTERIOR_DATABASE_URL"] == receipt["database_url"]
    assert exports["PILOT_WEB_PORT"] == str(receipt["ports"]["web"])
    jwks_url = urlparse(exports["INTERIOR_JWKS_URL"])
    assert jwks_url.hostname == "127.0.0.1"
    assert jwks_url.port == receipt["ports"]["jwks"]
    status, document = fetch(exports["INTERIOR_JWKS_URL"])
    assert status == 200 and json.loads(document)["keys"]
    request = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "5",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            f"http://127.0.0.1:{receipt['ports']['api']}/api/me",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert request.returncode == 0 and request.stdout == "401", request.stderr
    assert fetch(f"http://127.0.0.1:{receipt['ports']['web']}/bench")[0] == 200
    return receipt


def test_te1_concurrent_allocations_have_disjoint_ports_and_databases(tmp_path):
    with allocation_env(tmp_path) as (art, env, names, occupied_port):
        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(executor.map(lambda name: up(art, env, name), names))
        first, second = receipts
        first_ports = set(first["ports"].values())
        second_ports = set(second["ports"].values())
        assert first_ports.isdisjoint(second_ports), "TE-1: allocations share ports"
        assert occupied_port not in first_ports | second_ports
        assert first["database_url"] != second["database_url"]
        assert set(first["pids"].values()).isdisjoint(second["pids"].values())
        registry = json.loads((art / "task-env/registry.json").read_text())
        for name, receipt in zip(names, receipts, strict=True):
            assert registry[name]["ports"] == receipt["ports"]
            assert registry[name]["database_url"] == receipt["database_url"]


def test_te2_repeated_up_reuses_live_pids_ports_and_database(tmp_path):
    with allocation_env(tmp_path, 1) as (art, env, names, _):
        first = up(art, env, names[0])
        sql(
            f"p1_{names[0]}",
            "CREATE TABLE te_survives (value integer); INSERT INTO te_survives VALUES (37)",
        )
        second = up(art, env, names[0])
        for key in ("ports", "pids", "database_url", "exports"):
            assert second[key] == first[key], f"TE-2: repeated up changed {key}"
        assert sql(f"p1_{names[0]}", "SELECT value FROM te_survives") == "37"


def test_te3_pidfiles_and_source_do_not_invoke_ps(tmp_path):
    assert TOOL.is_file(), "TE: task_env.py CLI is missing"
    tree = ast.parse(TOOL.read_text())
    commands = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any(re.search(r"(?:^|[\s/;|&])ps(?:$|\s)", command) for command in commands), (
        "TE-3: task_env.py invokes ps"
    )
    with allocation_env(tmp_path, 1) as (art, env, names, _):
        receipt = up(art, env, names[0])
        pidfiles = list((art / "task-env").rglob("*.pid"))
        assert pidfiles, "TE-3: no pidfiles"
        stored = {int(path.read_text().strip()) for path in pidfiles}
        assert set(receipt["pids"].values()) <= stored
        assert all(alive(pid) for pid in stored)
        successful(env, "down", names[0])
        wait_dead(receipt["pids"].values())


@pytest.mark.parametrize("resource", ["pids", "database_url"])
def test_te4_down_refuses_foreign_resources_then_stops_only_its_own(tmp_path, resource):
    with allocation_env(tmp_path) as (art, env, names, _):
        first = up(art, env, names[0])
        second = up(art, env, names[1])
        path = art / "task-env" / f"{names[0]}.json"
        original = path.read_text()
        foreign = {**first, resource: second[resource]}
        path.write_text(json.dumps(foreign))
        try:
            refused = invoke(env, "down", names[0])
            assert refused.returncode != 0, "TE-4: down accepted another allocation's resources"
            for name, receipt in zip(names, (first, second), strict=True):
                assert all(alive(pid) for pid in receipt["pids"].values())
                assert sql(f"p1_{name}", "SELECT 1") == "1"
        finally:
            path.write_text(original)
        successful(env, "down", names[0])
        wait_dead(first["pids"].values())
        assert (
            sql("postgres", f"SELECT count(*) FROM pg_database WHERE datname = 'p1_{names[0]}'")
            == "0"
        )
        assert all(alive(pid) for pid in second["pids"].values())
        assert sql(f"p1_{names[1]}", "SELECT 1") == "1"
        assert fetch(f"http://127.0.0.1:{second['ports']['web']}/bench")[0] == 200


def test_te5_status_and_reap_require_both_release_and_dead_pids(tmp_path):
    with allocation_env(tmp_path, 4) as (art, env, names, _):
        receipts = [up(art, env, name) for name in names]
        status = successful(env, "status").stdout
        assert all(name in status for name in names), "TE-5: status omitted live allocations"
        run = Path(env["PILOT_RUN"])
        state = json.loads(run.read_text())
        for name in names[:2]:
            state["reservations"][name]["released"] = True
        run.write_text(json.dumps(state))
        for receipt in (receipts[0], receipts[2]):
            for pid in set(receipt["pids"].values()):
                os.kill(pid, signal.SIGTERM)
            wait_dead(receipt["pids"].values())
        before = [(art / "task-env" / f"{name}.json").read_bytes() for name in names[1:]]
        successful(env, "reap")
        assert (
            sql("postgres", f"SELECT count(*) FROM pg_database WHERE datname = 'p1_{names[0]}'")
            == "0"
        )
        registry = json.loads((art / "task-env/registry.json").read_text())
        assert names[0] not in registry, "TE-5: reap retained released dead allocation"
        assert not (art / "task-env" / f"{names[0]}.json").exists()
        for name, content in zip(names[1:], before, strict=True):
            assert name in registry
            assert (art / "task-env" / f"{name}.json").read_bytes() == content
            assert sql(f"p1_{name}", "SELECT 1") == "1"
        for receipt in (receipts[1], receipts[3]):
            assert all(alive(pid) for pid in receipt["pids"].values())
        after = successful(env, "status").stdout
        assert names[0] not in after and names[2] not in after
        assert names[1] in after and names[3] in after
        successful(env, "reap")
        assert json.loads((art / "task-env/registry.json").read_text()) == registry


def preview_config(env):
    code = """
const fs = require('fs');
const ts = require('typescript');
const Module = require('module');
const filename = process.argv[1];
const source = fs.readFileSync(filename, 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: {module: ts.ModuleKind.CommonJS}
}).outputText;
const loaded = new Module(filename);
loaded.filename = filename;
loaded.paths = Module._nodeModulePaths(require('path').dirname(filename));
loaded._compile(compiled, filename);
console.log(JSON.stringify(loaded.exports.default.webServer));
"""
    result = subprocess.run(
        ["node", "-e", code, str(ROOT / "web/playwright.config.ts")],
        cwd=ROOT / "web",
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_te_web_preview_reuse_requires_up_exports(tmp_path):
    source = (ROOT / "web/playwright.config.ts").read_text()
    assert not re.search(r"reuseExistingServer\s*:\s*false\b", source), (
        "TE-WEB: preview reuse is unconditionally disabled"
    )
    with allocation_env(tmp_path, 1) as (art, env, names, _):
        receipt = up(art, env, names[0])
        clean = {key: value for key, value in env.items() if key not in receipt["exports"]}
        clean["PILOT_WEB_PORT"] = str(receipt["ports"]["web"])
        default = preview_config(clean)
        configured = preview_config({**clean, **receipt["exports"]})
        assert default["reuseExistingServer"] is False
        assert configured["reuseExistingServer"] is True
        assert configured["url"] == f"http://127.0.0.1:{receipt['ports']['web']}/bench"
        assert default["command"] == configured["command"]
        assert default["url"] == configured["url"]
        assert alive(receipt["pids"]["web"])
