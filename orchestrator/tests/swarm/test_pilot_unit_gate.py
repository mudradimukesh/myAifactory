import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
            return {
                "exit_code": 0,
                "measured": 0,
                "threshold": 0,
                "command": ["fixture"],
            }

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
        assert not gate.known_diagnostics(
            old + "\nsrc/b.py:9: error: New problem", old, "mypy"
        )
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
        gate.py, gate.ts, gate.spec, gate.survivors = [], [], {}, []
        captured = []
        monkeypatch.setattr(
            gate, "commands", lambda name, commands: captured.extend(commands) or {}
        )
        monkeypatch.setattr(module, "run_all", lambda checks: dict(checks)["G4"]())
        gate.execute()
        assert [
            str(tmp_path / ".venv/bin/ruff"),
            "format",
            "--check",
            "tests/api/test_session.py",
        ] in captured
    finally:
        sys.path.pop(0)


def test_baseline_requires_recorded_format_file_to_be_unchanged(tmp_path, monkeypatch):
    import importlib
    import json
    from types import SimpleNamespace

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        gate = importlib.import_module("unit_gate")
        diagnostic = "unformatted: File would be reformatted\n   --> tests/phase2/test_environment.py:23:5\n"
        (tmp_path / "stage1-baseline-2.log").write_text(diagnostic)
        (tmp_path / "run.json").write_text(json.dumps({"baseline_sha": "original"}))
        monkeypatch.setattr(gate, "fingerprint", lambda root: {})
        monkeypatch.setattr(
            gate,
            "project_adapter",
            lambda: SimpleNamespace(
                baseline_commands=lambda root: [
                    ["pytest"],
                    ["ruff", "check"],
                    ["ruff", "format"],
                ]
            ),
        )

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
            assert packet["results"][2]["disposition"] == (
                "failed" if modified else "known_failure"
            )
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
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "base",
        ],
    ):
        subprocess.run(
            ["git", *args], cwd=root, env=env, check=True, capture_output=True
        )
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    source.write_text("export const value = 2;\n")
    (root / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    report = root / "web/coverage/lcov.info"
    report.parent.mkdir()
    report.write_text("TN:\nSF:src/auth.ts\nDA:1,0\nLF:1\nLH:0\nend_of_record\n")
    gate = object.__new__(gate_module.Gate)
    gate.root, gate.art, gate.tag, gate.base = root, tmp_path, "coverage", base
    gate.py, gate.ts, gate.env = [], ["web/src/auth.ts"], env
    monkeypatch.setattr(gate, "unit", lambda *args: {"exit_code": 0})
    assert gate.coverage()["exit_code"] != 0
    report.write_text(
        report.read_text().replace("DA:1,0", "DA:1,1").replace("LH:0", "LH:1")
    )
    assert gate.coverage()["exit_code"] == 0
    sys.path.pop(0)


def test_project_runtime_requires_explicit_adapter(tmp_path, monkeypatch):
    import importlib

    import pytest

    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        evidence = importlib.import_module("gate_evidence")
        monkeypatch.delenv("SWARM_PROJECT_ADAPTER", raising=False)
        with (
            pytest.raises(ValueError, match="SWARM_PROJECT_ADAPTER"),
            evidence.runtime(tmp_path, tmp_path, "fixture", {}),
        ):
            pytest.fail("Unconfigured project runtime was accepted")
        adapter = tmp_path / "adapter.py"
        adapter.write_text(
            "from contextlib import contextmanager\n"
            "@contextmanager\ndef runtime(root, art, tag, env):\n"
            "    marker = art / 'owned-service'\n    marker.write_text(tag)\n"
            "    try:\n        yield {**env, 'READY': 'yes'}\n"
            "    finally:\n        marker.unlink()\n"
        )
        monkeypatch.setenv("SWARM_PROJECT_ADAPTER", str(adapter))
        with (
            pytest.raises(RuntimeError, match="fixture failure"),
            evidence.runtime(tmp_path, tmp_path, "fixture", {}) as env,
        ):
            assert env["READY"] == "yes"
            assert (tmp_path / "owned-service").read_text() == "fixture"
            raise RuntimeError("fixture failure")
        assert not (tmp_path / "owned-service").exists()
    finally:
        sys.path.pop(0)
