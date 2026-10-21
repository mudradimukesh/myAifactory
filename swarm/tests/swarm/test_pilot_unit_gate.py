import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def poc_gate(tmp_path, monkeypatch):
    import importlib
    import json

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        module = importlib.import_module("unit_gate")
        files = ["src/plan.py", "tests/api/test_plan.py", "tests/swarm/test_plan.py"]
        for file in files:
            (tmp_path / file).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / file).write_text("import plan\n")
        mapping = [
            {
                "id": "A1",
                "runner": "pytest",
                "test_node": "tests/api/test_plan.py::test_save",
                "oracle": "spec_literal",
            }
        ]
        (tmp_path / "u-test-map.json").write_text(json.dumps(mapping))
        (tmp_path / "u-ids.txt").write_text("A1\n")
        (tmp_path / "u-s1-tests.json").write_text('{"tests_commit": "t"}')
        (tmp_path / "u-red.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "tests_commit": "t",
                    "test_map_sha256": module.digest(tmp_path / "u-test-map.json"),
                }
            )
        )
        monkeypatch.setattr(
            module, "acceptance", lambda path: {"journey_specs": ["save.phase1-flows.spec.ts"]}
        )
        monkeypatch.setattr(module, "changed", lambda *args: files)
        gate = module.Gate(tmp_path, tmp_path, "u", "base", "candidate")
        captured = []

        def commands(identifier, commands, cwd="."):
            captured.extend(commands)
            gate.file("green-browser.json").write_text(
                json.dumps(
                    {
                        "suites": [
                            {
                                "specs": [
                                    {
                                        "file": "save.phase1-flows.spec.ts",
                                        "title": "saves",
                                        "tests": [
                                            {"projectName": p, "results": [{"status": "passed"}]}
                                            for p in ("light-keyboard", "light-pointer")
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                )
            )
            return {"exit_code": 0, "measured": 0, "threshold": 0, "command": commands}

        monkeypatch.setattr(gate, "commands", commands)
        monkeypatch.setattr(module, "python_rows", lambda path: [])
        yield module, gate, captured
    finally:
        sys.path.pop(0)


def test_development_runs_affected_tests_once_without_swarm_or_benchmarks(poc_gate):
    _, gate, commands = poc_gate
    gate.green()
    gate.unit("G16")
    python = [c for c in commands if "pytest" in c]
    assert len(python) == 1
    assert "tests/api/test_plan.py" in python[0]
    assert "tests/api/test_plan.py::test_save" not in python[0]
    assert not any(
        "swarm" in arg or "--cov" in arg or "count=" in arg for c in commands for arg in c
    )
    browser = next(c for c in commands if "e2e" in c)
    assert "--project=light-keyboard" in browser and "--project=light-pointer" in browser
    assert "--repeat-each=1" in browser
    assert not any("dark" in arg or "bench" in arg for arg in browser)


def test_final_reuses_one_python_run_for_regression_and_coverage(poc_gate):
    _, gate, commands = poc_gate
    gate.final = True
    gate.green()
    gate.coverage()
    python = [c for c in commands if "pytest" in c]
    assert len(python) == 1
    assert "--ignore=tests/swarm" in python[0]
    assert "--ignore=tests/phase2" in python[0]
    assert "--cov=src" in python[0] and "--cov-branch" in python[0]
    assert any("diff-cover" in c[0] for c in commands)
    assert gate.file("gate.json").name == "u-final-gate.json"


def test_gate_stops_before_expensive_work_after_lint_failure(poc_gate, monkeypatch):
    _, gate, _ = poc_gate
    monkeypatch.setattr(gate, "frozen", lambda: {"exit_code": 0})
    monkeypatch.setattr(gate, "commands", lambda *args: {"exit_code": 1})
    monkeypatch.setattr(gate, "green", lambda: pytest.fail("tests ran after lint failed"))
    rows = {r["id"]: r for r in gate.execute()}
    assert rows["G4"]["exit_code"] == 1
    assert rows["G2"]["disposition"] == "blocked"
    assert rows["G18"]["exit_code"] == 1


def test_development_results_pass_review_without_running_final_work(poc_gate, monkeypatch):
    import importlib

    module, gate, commands = poc_gate
    checks = importlib.import_module("review_check")
    monkeypatch.setattr(gate, "frozen", lambda: {"exit_code": 0, "measured": 0, "threshold": 0})
    monkeypatch.setattr(gate, "diff", lambda *args: {"exit_code": 0, "measured": 0, "threshold": 0})
    monkeypatch.setattr(
        module,
        "python_rows",
        lambda path: [
            {
                "node": "tests/api/test_plan.py::test_save",
                "project": "pytest",
                "status": "passed",
            }
        ],
    )
    monkeypatch.setattr(gate, "mutation", lambda *args: pytest.fail("development ran mutation"))
    rows = gate.execute()
    packet = {
        "candidate_sha": "candidate",
        "fingerprint": "env",
        "gates": rows,
        "test_policy": module.POC_TEST_POLICY,
        "stage": "pre",
        "test_suite": "poc",
    }
    checks.gates_valid(packet, {"candidate_sha": "candidate"}, gate.spec, "env")
    assert len([c for c in commands if "pytest" in c]) == 1
    assert len([c for c in commands if "e2e" in c]) == 1
    assert len([c for c in commands if "test" in c and "npm" in c]) == 1
    assert {r["id"] for r in rows if r["disposition"] == "deferred"} == {
        "G10",
        "G11",
        "G12",
        "G13",
        "G18",
    }


def test_swarm_selection_stays_separate_from_product(poc_gate):
    _, gate, commands = poc_gate
    gate.suite = "swarm"
    gate.mapping = [{"runner": "pytest", "test_node": "tests/swarm/test_plan.py::test_lifecycle"}]
    gate.green()
    assert len(commands) == 1
    assert "tests/swarm" in commands[0]
    assert "--ignore=tests/swarm" not in commands[0]
    assert not any("e2e" in command for command in commands)


def test_default_collection_excludes_swarm_but_explicit_swarm_still_runs(tmp_path):
    import os
    import subprocess

    (tmp_path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text())
    for folder in ("api", "swarm", "phase2"):
        path = tmp_path / "tests" / folder / ("test_" + folder + ".py")
        path.parent.mkdir(parents=True)
        path.write_text("def test_example():\n    assert True\n")
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}
    product = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert product.returncode == 0, product.stdout + product.stderr
    assert "tests/api/test_api.py" in product.stdout
    assert "tests/swarm/" not in product.stdout and "tests/phase2/" not in product.stdout
    swarm = subprocess.run(
        [*command, "tests/swarm"], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert swarm.returncode == 0, swarm.stdout + swarm.stderr
    assert "tests/swarm/test_swarm.py" in swarm.stdout
    assert "tests/api/" not in swarm.stdout


def test_gate_runs_later_checks_after_failure_and_records_all(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "unit_gate", ROOT / "scripts/swarm/unit_gate.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        visited = []

        def check(index):
            visited.append(index)
            if index == 2:
                raise ValueError("deliberate failure")
            return {"exit_code": 0, "measured": 0, "threshold": 0, "command": ["fixture"]}

        rows = module.run_all([(f"G{i}", lambda i=i: check(i)) for i in range(1, 19)])
        assert visited == list(range(1, 19))
        assert len(rows) == 18
        assert rows[1]["exit_code"] == 1
        assert rows[-1]["exit_code"] == 0
    finally:
        sys.path.pop(0)


def test_known_baseline_diagnostics_reject_new_errors():
    import importlib

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        gate = importlib.import_module("unit_gate")
        old = "src/a.py:7: error: Existing problem  [arg-type]\nFound 1 error"
        assert gate.known_diagnostics(old, old, "mypy")
        assert not gate.known_diagnostics(old + "\nsrc/b.py:9: error: New problem", old, "mypy")
        assert not gate.known_diagnostics("mypy crashed", old, "mypy")
        assert not gate.known_diagnostics(old.replace(":7:", ":8:"), old, "mypy")
        formatting = "Would reformat: tests/phase2/test_environment.py\n1 file would be reformatted"
        assert gate.known_diagnostics(formatting, formatting, "format")
        modern = (
            "unformatted: File would be reformatted\n"
            "   --> tests/phase2/test_environment.py:23:5\n"
            "    |\n23  | old code\n    - old\n23  + new\n"
            "1 file would be reformatted, 147 files already formatted\n"
        )
        assert gate.known_diagnostics(modern, modern, "format")
        assert gate.known_diagnostics(modern, formatting, "format")
        assert not gate.known_diagnostics(
            modern.replace("test_environment.py", "test_new.py"), modern, "format"
        )
        assert not gate.known_diagnostics(
            modern + "error: Failed to read tests/missing.py\n", modern, "format"
        )
        assert not gate.known_diagnostics(
            "unformatted: File would be reformatted\n", modern, "format"
        )
        assert not gate.known_diagnostics(
            formatting + "\nWould reformat: tests/new.py", formatting, "format"
        )
    finally:
        sys.path.pop(0)


def test_gate_formats_changed_python_tests(tmp_path, monkeypatch):
    import importlib

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        module = importlib.import_module("unit_gate")
        gate = object.__new__(module.Gate)
        gate.root, gate.art, gate.tag = tmp_path, tmp_path, "probe"
        gate.files = ["tests/api/test_session.py", "web/src/auth.ts"]
        (tmp_path / "tests/api").mkdir(parents=True)
        (tmp_path / "tests/api/test_session.py").write_text("")
        gate.py, gate.ts, gate.spec, gate.survivors = [], [], {}, []
        captured = []
        monkeypatch.setattr(
            gate,
            "commands",
            lambda name, commands, cwd=".": captured.extend(commands) or {"exit_code": 1},
        )
        monkeypatch.setattr(gate, "frozen", lambda: {"exit_code": 0})
        gate.execute()
        assert [
            str(tmp_path / ".venv/bin/ruff"),
            "format",
            "--check",
            "tests/api/test_session.py",
        ] in captured
    finally:
        sys.path.pop(0)


def test_fast_gate_runs_tests_once_and_defers_repeats_and_mutation(tmp_path, monkeypatch):
    import importlib

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        module = importlib.import_module("unit_gate")
        for name, text in {
            "src/plan.py": "",
            "tests/api/test_plan.py": "from plan import x\n",
            "tests/test_other.py": "import json\n",
            "web/src/a.ts": "",
        }.items():
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_text(text)
        gate = object.__new__(module.Gate)
        gate.root, gate.art, gate.tag, gate.fast = tmp_path, tmp_path, "probe", True
        gate.python = "python"
        gate.files = ["src/plan.py", "web/src/a.ts", "web/src/gone.ts"]
        gate.py, gate.ts, gate.spec, gate.survivors = ["src/plan.py"], ["web/src/a.ts"], {}, []
        gate.mapping = [{"runner": "pytest", "test_node": "tests/test_map.py::test_x"}]
        assert gate.file("G2.log") == tmp_path / "probe-fast-G2.log"
        assert gate.input("red.json") == tmp_path / "probe-red.json"
        assert gate.repeats == 1
        captured = {}
        monkeypatch.setattr(
            gate,
            "commands",
            lambda name, commands, cwd=".": (
                captured.update({name: (cwd, commands)}) or {"exit_code": 0}
            ),
        )
        monkeypatch.setattr(gate, "mutation", lambda *args: pytest.fail("mutation ran"))
        monkeypatch.setattr(gate, "frozen", lambda: {"exit_code": 0})
        monkeypatch.setattr(gate, "diff", lambda *args: {"exit_code": 0})
        rows = {row["id"]: row for row in gate.execute()}
        for identifier in ("G2", "G3", "G9", "G10", "G11", "G12", "G13", "G16", "G17", "G18"):
            assert rows[identifier]["disposition"] == "deferred"
        assert captured["G4"][1][0] == [
            str(tmp_path / ".venv/bin/ruff"),
            "check",
            "--force-exclude",
            "src/plan.py",
        ]
        assert captured["G14"] == (
            "web",
            [["npm", "exec", "--", "eslint", "--max-warnings=0", "src/a.ts"]],
        )
        assert captured["related"][1] == [
            [
                "python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:randomly",
                "tests/api/test_plan.py",
                "tests/test_map.py::test_x",
            ],
            [
                *["npm", "--prefix", "web", "run", "test", "--", "related", "--run"],
                *["--passWithNoTests", "src/a.ts"],
            ],
        ]
    finally:
        sys.path.pop(0)


def test_fast_gate_skips_provision_only_when_marker_matches(tmp_path, monkeypatch):
    import contextlib
    import hashlib
    import importlib
    import json

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        module = importlib.import_module("unit_gate")
        (tmp_path / "web").mkdir()
        (tmp_path / "pyproject.toml").write_text("p")
        (tmp_path / "web/package-lock.json").write_text("l")
        (tmp_path / ".venv").mkdir()
        (tmp_path / "probe-candidate.json").write_text(
            json.dumps({"worktree": str(tmp_path), "candidate_sha": "c", "main_sha": "b"})
        )
        outputs = {"HEAD": "c", "--porcelain": "", "HEAD^{tree}": "t"}
        monkeypatch.setattr(module, "output", lambda command, root: outputs[command[2]])
        provisioned = []
        monkeypatch.setattr(module, "provision", lambda root, log: provisioned.append(1) or {})
        monkeypatch.setattr(module, "fingerprint", lambda root: "f")
        monkeypatch.setattr(module, "artifact_hashes", lambda *args: {})
        isolations, runtime_envs = [], []
        monkeypatch.setattr(
            module,
            "runtime",
            lambda root, art, tag, env: isolations.append(tag) or contextlib.nullcontext({}),
        )

        class Probe(module.Gate):
            def __init__(self, root, art, tag, base, candidate, fast=False):
                self.art, self.tag, self.fast = art, tag, fast
                self.files, self.spec, self.env = [], {}, {}

            def execute(self):
                runtime_envs.append(self.runtime_env is not None)
                return [{"id": "G1", "exit_code": 0}]

        monkeypatch.setattr(module, "Gate", Probe)
        monkeypatch.setenv("ART", str(tmp_path))
        for mode in ("fast", "fast", "pre"):
            monkeypatch.setattr(sys, "argv", ["unit_gate", mode, "probe", "b", "c"])
            assert module.main() == 0
        assert len(provisioned) == 1
        assert isolations == []
        assert runtime_envs == [True, True, True]
        marker = (tmp_path / ".venv/provision.sha256").read_text()
        assert marker == hashlib.sha256(b"pl").hexdigest()
        (tmp_path / "web/package-lock.json").write_text("changed")
        monkeypatch.setattr(sys, "argv", ["unit_gate", "fast", "probe", "b", "c"])
        assert module.main() == 0
        assert len(provisioned) == 2
    finally:
        sys.path.pop(0)


def test_baseline_requires_recorded_format_file_to_be_unchanged(tmp_path, monkeypatch):
    import importlib
    import json
    from types import SimpleNamespace

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        gate = importlib.import_module("unit_gate")
        diagnostic = (
            "unformatted: File would be reformatted\n   --> tests/phase2/test_environment.py:23:5\n"
        )
        (tmp_path / "stage1-baseline-2.log").write_text(diagnostic)
        (tmp_path / "run.json").write_text(json.dumps({"baseline_sha": "original"}))
        monkeypatch.setattr(gate, "fingerprint", lambda root: {})

        def run(command, **kwargs):
            failed = "format" in command
            kwargs["stdout"].write(diagnostic if failed else "passed")
            return SimpleNamespace(returncode=int(failed))

        monkeypatch.setattr(gate.subprocess, "run", run)
        for modified in (False, True):

            def output(command, root, modified=modified):
                if command[1] == "diff":
                    assert command[-1] == "tests/phase2/test_environment.py"
                    return command[-1] if modified else ""
                return "current"

            monkeypatch.setattr(gate, "output", output)
            assert gate.baseline(tmp_path, tmp_path, "probe", {}) is modified
            packet = json.loads((tmp_path / "probe-baseline.json").read_text())
            assert next(r for r in packet["results"] if "format" in r["command"])[
                "disposition"
            ] == ("failed" if modified else "known_failure")
    finally:
        sys.path.pop(0)


def test_web_changed_line_coverage_uses_repository_paths(tmp_path, monkeypatch):
    import importlib
    import os
    import subprocess

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    gate_module = importlib.import_module("unit_gate")
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "web/src/auth.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export const value = 1;\n")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (
        ["init"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base"],
    ):
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    source.write_text("export const value = 2;\n")
    (root / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    report = root / "web/coverage/lcov.info"
    report.parent.mkdir()
    report.write_text("TN:\nSF:src/auth.ts\nDA:1,0\nLF:1\nLH:0\nend_of_record\n")
    gate = object.__new__(gate_module.Gate)
    gate.root, gate.art, gate.tag, gate.base = root, tmp_path, "coverage", base
    gate.qualification_base = base
    gate.py, gate.ts, gate.env = [], ["web/src/auth.ts"], env
    monkeypatch.setattr(gate, "unit", lambda *args: {"exit_code": 0})
    assert gate.coverage()["exit_code"] != 0
    report.write_text(report.read_text().replace("DA:1,0", "DA:1,1").replace("LH:0", "LH:1"))
    assert gate.coverage()["exit_code"] == 0
    sys.path.pop(0)


def test_runtime_expired_token_is_rejected_immediately(tmp_path):
    import importlib
    import os
    import urllib.error
    import urllib.request

    import pytest

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    evidence = importlib.import_module("gate_evidence")
    root = tmp_path / "runtime"
    root.mkdir()
    (root / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    for name in ("alembic.ini", "src"):
        (root / name).symlink_to(ROOT / name, target_is_directory=(ROOT / name).is_dir())
    with evidence.runtime(root, tmp_path, "expired", dict(os.environ)) as env:
        assert env["INTERIOR_STORE_DIR"] == str(root / ".pilot-store/expired")
        token = Path(env["PILOT_EXPIRED_TOKEN_FILE"]).read_text()
        request = urllib.request.Request(
            env["PILOT_API_URL"] + "/api/me", headers={"Authorization": "Bearer " + token}
        )
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(request)
        assert failure.value.code == 401
    sys.path.pop(0)


def test_gate_writes_packet_and_fails_when_artifact_is_missing(tmp_path, monkeypatch):
    import contextlib
    import importlib
    import json

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        module = importlib.import_module("unit_gate")
        (tmp_path / "web").mkdir()
        (tmp_path / ".venv").mkdir()
        (tmp_path / "pyproject.toml").write_text("p")
        (tmp_path / "web/package-lock.json").write_text("l")
        (tmp_path / "probe-candidate.json").write_text(
            json.dumps({"worktree": str(tmp_path), "candidate_sha": "c", "main_sha": "b"})
        )
        outputs = {"HEAD": "c", "--porcelain": "", "HEAD^{tree}": "t"}
        monkeypatch.setattr(module, "output", lambda command, root: outputs[command[2]])
        monkeypatch.setattr(module, "provision", lambda root, log: {})
        monkeypatch.setattr(module, "fingerprint", lambda root: "f")
        monkeypatch.setattr(module, "runtime", lambda *args: contextlib.nullcontext({}))

        class Probe(module.Gate):
            def __init__(self, root, art, tag, base, candidate, fast=False):
                self.art, self.tag, self.fast = art, tag, fast
                self.files, self.spec, self.env = [], {}, {}

            def execute(self):
                return [{"id": "G1", "exit_code": 0}]

        monkeypatch.setattr(module, "Gate", Probe)
        monkeypatch.setenv("ART", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["unit_gate", "pre", "probe", "b", "c"])
        assert module.main() == 1
        packet = json.loads((tmp_path / "probe-gate.json").read_text())
        assert packet["artifact_hashes"] == {}
        assert packet["gates"][-1]["id"] == "packet"
        assert "probe-acceptance.md" in packet["gates"][-1]["error"]
    finally:
        sys.path.pop(0)


def test_frozen_ignores_test_changes_merged_from_base(tmp_path):
    import hashlib
    import importlib
    import json
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(path, text):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
        git("add", path)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", path)
        return git("rev-parse", "HEAD")

    git("init", "-q", "-b", "main")
    commit("tests/swarm/test_tool.py", "old\n")
    git("checkout", "-qb", "unit")
    tests_commit = commit("tests/api/test_unit.py", "frozen\n")
    git("checkout", "-q", "main")
    base = commit("tests/swarm/test_tool.py", "new\n")
    git("checkout", "-q", "unit")
    git("-c", "user.name=t", "-c", "user.email=t@t", "merge", "-qm", "merge", "main")
    art = tmp_path / "art"
    art.mkdir()
    (art / "u-s1-tests.json").write_text(json.dumps({"tests_commit": tests_commit}))
    digest = hashlib.sha256(b"frozen\n").hexdigest()
    (art / "u-s1-tests.sha256").write_text(digest + "  tests/api/test_unit.py\n")
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        gate = object.__new__(importlib.import_module("unit_gate").Gate)
        gate.root, gate.art, gate.tag, gate.base = tmp_path, art, "u", base
        gate.candidate = git("rev-parse", "HEAD")
        assert gate.frozen()["exit_code"] == 0
        gate.candidate = commit("tests/swarm/test_tool.py", "edited\n")
        with pytest.raises(ValueError, match=r"test_tool\.py"):
            gate.frozen()
    finally:
        sys.path.pop(0)


def test_gate_uses_acceptance_check_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    module = importlib.import_module("unit_gate")
    readers = importlib.import_module("acceptance_check")
    for name in ("browser_rows", "check", "mapped_nodes", "python_rows"):
        assert getattr(module, name) is getattr(readers, name)
    report = tmp_path / "tests.xml"
    report.write_text(
        '<testsuite><testcase file="tests/test_probe.py" name="test_pass"/>'
        '<testcase file="tests/test_probe.py" name="test_fail">'
        '<failure message="assertion">details</failure></testcase></testsuite>'
    )
    assert module.python_rows(report) == [
        {
            "node": "tests/test_probe.py::test_pass",
            "project": "pytest",
            "status": "passed",
            "error": "",
        },
        {
            "node": "tests/test_probe.py::test_fail",
            "project": "pytest",
            "status": "failed",
            "error": "assertion\ndetails",
        },
    ]


def test_gate_commands_stops_after_failure_and_records_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib
    import os

    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    module = importlib.import_module("unit_gate")
    gate = object.__new__(module.Gate)
    gate.root, gate.art, gate.tag = tmp_path, tmp_path, "probe"
    gate.env = {**os.environ, "GATE_PROBE": "environment retained"}
    (tmp_path / "work").mkdir()
    commands = [
        [sys.executable, "-c", "import sys; print('first', flush=True); sys.exit(3)"],
        [
            sys.executable,
            "-c",
            "import os, sys; print(os.getcwd()); print(os.environ['GATE_PROBE']); "
            "print('stderr retained', file=sys.stderr)",
        ],
    ]
    result = gate.commands("G4", commands, cwd="work")
    assert result == {
        "command": commands,
        "exit_code": 3,
        "measured": 1,
        "threshold": 0,
        "evidence": str(tmp_path / "probe-G4.log"),
    }
    log = (tmp_path / "probe-G4.log").read_text()
    assert "first" in log
    assert str(tmp_path / "work") not in log
    assert "environment retained" not in log
    assert "stderr retained" not in log


@pytest.mark.parametrize("report_case", ["passed", "missing_project", "duplicate_run", "skipped"])
def test_gate_journey_checks_acceptance_rows(tmp_path, monkeypatch, report_case):
    import importlib

    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    module = importlib.import_module("unit_gate")
    gate = object.__new__(module.Gate)
    gate.root, gate.art, gate.tag = tmp_path, tmp_path, "probe"
    gate.env = {}
    gate.spec = {"journey_specs": ["tests/probe.spec.ts"]}
    gate.mapping = [
        {
            "id": "acceptance-1",
            "runner": "playwright",
            "oracle": "fixture",
            "test_node": "tests/probe.spec.ts::acceptance passes",
            "projects": ["light-keyboard", "light-pointer"],
        }
    ]
    projects = (
        ["light-keyboard"]
        if report_case == "missing_project"
        else ["light-keyboard", "light-pointer"]
    )
    repeats = 2 if report_case == "duplicate_run" else 1
    gate.reports = {
        "playwright": [
            {
                "node": "tests/probe.spec.ts::acceptance passes",
                "project": project,
                "status": "skipped" if report_case == "skipped" else "passed",
            }
            for project in projects
            for _ in range(repeats)
        ]
    }
    captured = []

    def commands(identifier, actions):
        captured.extend(actions)
        return {"exit_code": 0, "measured": 0, "threshold": 0, "command": actions}

    monkeypatch.setattr(gate, "commands", commands)
    if report_case == "passed":
        result = gate.journey()
        assert result["exit_code"] == 0
        assert result["failed_test_ids"] == []
        assert result["paths"] == ["web/", "src/", "scripts/swarm/", "tests/e2e/"]
    else:
        with pytest.raises(ValueError, match="acceptance-1"):
            gate.journey()
    assert captured == []


def test_full_suite_gates_skip_swarm_and_phase2_tests():
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        import unit_gate
    finally:
        sys.path.pop(0)
    source = (ROOT / "scripts/swarm/unit_gate.py").read_text()
    assert unit_gate.PYTEST_IGNORE == ["--ignore=tests/phase2", "--ignore=tests/swarm"]
    assert source.count("*PYTEST_IGNORE") == 2
