import contextlib
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def recording_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, list[tuple[str, list[list[str]], str]], dict[str, int]]:
    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    module = importlib.import_module("unit_gate")
    files = [
        "scripts/swarm/unit_gate.py",
        "tests/swarm/test_unit_gate_failfast.py",
        "web/src/probe.ts",
    ]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (tmp_path / "probe-acceptance.md").write_text(
        '```json\n{"python_test_paths": ["tests/swarm/test_unit_gate_failfast.py"]}\n```\n'
    )
    (tmp_path / "probe-test-map.json").write_text("[]\n")
    monkeypatch.setattr(module, "changed", lambda *args: files)
    visited: list[tuple[str, list[list[str]], str]] = []
    codes: dict[str, int] = {}

    def record(identifier: str, commands: list[list[str]], cwd: str = ".") -> dict[str, object]:
        visited.append((identifier, commands, cwd))
        code = codes.get(identifier, 0)
        return {
            "command": commands,
            "exit_code": code,
            "measured": int(code != 0),
            "threshold": 0,
            "evidence": "recorded: " + identifier,
        }

    for name, identifier in {
        "frozen": "G1",
        "green": "G2",
        "mapped": "G3",
        "properties": "G9",
        "coverage": "G12",
        "journey": "G17",
        "related": "related",
    }.items():
        monkeypatch.setattr(
            module.Gate,
            name,
            lambda self, identifier=identifier: record(identifier, [[identifier]]),
        )
    monkeypatch.setattr(
        module.Gate,
        "commands",
        lambda self, identifier, commands, cwd=".": record(identifier, commands, cwd),
    )
    monkeypatch.setattr(
        module.Gate, "diff", lambda self, identifier: record(identifier, [[identifier]])
    )
    monkeypatch.setattr(
        module.Gate, "unit", lambda self, identifier: record(identifier, [[identifier]])
    )
    monkeypatch.setattr(
        module.Gate,
        "mutation",
        lambda self, identifier, language, files: record(identifier, [[identifier]]),
    )
    return module, visited, codes


@pytest.mark.parametrize("failure", ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9"])
def test_full_gate_skips_after_cheap_failure(
    tmp_path: Path,
    recording_actions: tuple[ModuleType, list[tuple[str, list[list[str]], str]], dict[str, int]],
    failure: str,
) -> None:
    module, visited, codes = recording_actions
    codes[failure] = 17
    gate = module.Gate(tmp_path, tmp_path, "probe", "base", "candidate")
    rows = gate.execute()
    identifiers = [f"G{i}" for i in range(1, 19)]
    stop = identifiers.index(failure) + 1
    assert [item[0] for item in visited] == identifiers[:stop], (
        "Later gate actions ran after a cheap gate failure"
    )
    assert [row["id"] for row in rows] == identifiers
    for row in rows[:stop]:
        assert row["exit_code"] == (17 if row["id"] == failure else 0)
        assert row["evidence"] == "recorded: " + row["id"]
    for row in rows[stop:]:
        assert row["exit_code"] == 1
        assert row["evidence"] == "skipped: prior failure"
    assert json.loads((tmp_path / "probe-survivors.json").read_text()) == {"survivors": []}


@pytest.mark.parametrize("failure", ["none", "G10", "G17", "G18"])
def test_full_gate_runs_all_actions_with_original_commands_and_rows(
    tmp_path: Path,
    recording_actions: tuple[ModuleType, list[tuple[str, list[list[str]], str]], dict[str, int]],
    failure: str,
) -> None:
    module, visited, codes = recording_actions
    codes[failure] = 17
    gate = module.Gate(tmp_path, tmp_path, "probe", "base", "candidate")
    rows = gate.execute()
    ruff = str(tmp_path / ".venv/bin/ruff")
    python = str(tmp_path / ".venv/bin/python")
    commands = {
        "G1": [["G1"]],
        "G2": [["G2"]],
        "G3": [["G3"]],
        "G4": [
            [ruff, "check", "."],
            [
                ruff,
                "format",
                "--check",
                "scripts/swarm/unit_gate.py",
                "tests/swarm/test_unit_gate_failfast.py",
            ],
        ],
        "G5": [
            [
                ruff,
                "check",
                "--extend-select",
                "C90,PLR0911,PLR0912,PLR0913,PLR0915",
                "--config",
                "lint.mccabe.max-complexity = 10",
                "scripts/swarm/unit_gate.py",
            ]
        ],
        "G6": [
            [
                str(tmp_path / ".venv/bin/mypy"),
                "--strict",
                "--follow-imports=silent",
                "scripts/swarm/unit_gate.py",
            ]
        ],
        "G7": [["G7"]],
        "G8": [["G8"]],
        "G9": [["G9"]],
        "G10": [[python, "-m", "pytest", "-q", "-p", "randomly", "--ignore=tests/phase2"]],
        "G11": [
            [
                python,
                "-m",
                "pytest",
                "-q",
                "-p",
                "randomly",
                "--count=5",
                "--repeat-scope=session",
                "tests/swarm/test_unit_gate_failfast.py",
            ]
        ],
        "G12": [["G12"]],
        "G13": [["G13"]],
        "G14": [["npm", "--prefix", "web", "run", "lint"]],
        "G15": [["npm", "--prefix", "web", "run", "typecheck"]],
        "G16": [["G16"]],
        "G17": [["G17"]],
        "G18": [["G18"]],
    }
    assert visited == [(identifier, command, ".") for identifier, command in commands.items()]
    assert [row["id"] for row in rows] == list(commands)
    for row in rows:
        assert {key: value for key, value in row.items() if key != "duration_seconds"} == {
            "id": row["id"],
            "disposition": "retained",
            "comparison": "max",
            "command": commands[row["id"]],
            "exit_code": 17 if row["id"] == failure else 0,
            "measured": int(row["id"] == failure),
            "threshold": 0,
            "evidence": "recorded: " + row["id"],
        }
        assert row["duration_seconds"] >= 0


@pytest.mark.parametrize("failure", ["none", "G1", "G4", "G8", "G15", "related"])
def test_fast_gate_keeps_actions_and_deferred_rows_after_failure(
    tmp_path: Path,
    recording_actions: tuple[ModuleType, list[tuple[str, list[list[str]], str]], dict[str, int]],
    failure: str,
) -> None:
    module, visited, codes = recording_actions
    codes[failure] = 17
    gate = module.Gate(tmp_path, tmp_path, "probe", "base", "candidate", fast=True)
    rows = gate.execute()
    deferred = {"G2", "G3", "G9", "G10", "G11", "G12", "G13", "G16", "G17", "G18"}
    assert sorted(item[0] for item in visited) == [
        "G1",
        "G14",
        "G15",
        "G4",
        "G5",
        "G6",
        "G7",
        "G8",
        "related",
    ]
    assert [row["id"] for row in rows] == [*[f"G{i}" for i in range(1, 19)], "related"]
    for row in rows:
        if row["id"] in deferred:
            assert {key: value for key, value in row.items() if key != "duration_seconds"} == {
                "id": row["id"],
                "comparison": "max",
                "disposition": "not_applicable",
                "command": [],
                "exit_code": 0,
                "measured": 0,
                "threshold": 0,
                "evidence": "Fast gate: deferred to the full pre gate",
            }
        else:
            assert row["disposition"] == "retained"
            assert row["exit_code"] == (17 if row["id"] == failure else 0)
            assert row["evidence"] == "recorded: " + row["id"]
    captured = {identifier: (commands, cwd) for identifier, commands, cwd in visited}
    ruff = str(tmp_path / ".venv/bin/ruff")
    assert captured["G4"] == (
        [
            [
                ruff,
                "check",
                "--force-exclude",
                "scripts/swarm/unit_gate.py",
                "tests/swarm/test_unit_gate_failfast.py",
            ],
            [
                ruff,
                "format",
                "--check",
                "scripts/swarm/unit_gate.py",
                "tests/swarm/test_unit_gate_failfast.py",
            ],
        ],
        ".",
    )
    assert captured["G14"] == (
        [["npm", "exec", "--", "eslint", "--max-warnings=0", "src/probe.ts"]],
        "web",
    )
    assert json.loads((tmp_path / "probe-fast-survivors.json").read_text()) == {"survivors": []}


@pytest.mark.parametrize("failure", ["G2", "G9"])
def test_unit_gate_receipt_and_exit_fail_when_actions_are_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_actions: tuple[ModuleType, list[tuple[str, list[list[str]], str]], dict[str, int]],
    failure: str,
) -> None:
    module, visited, codes = recording_actions
    codes[failure] = 17
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
    monkeypatch.setattr(module, "artifact_hashes", lambda *args: {})
    monkeypatch.setattr(module, "runtime", lambda *args: contextlib.nullcontext({}))
    monkeypatch.setenv("ART", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["unit_gate", "pre", "probe", "b", "c"])
    assert module.main() == 1
    packet = json.loads((tmp_path / "probe-gate.json").read_text())
    stop = int(failure.removeprefix("G"))
    assert len(packet["gates"]) == 18
    assert all(
        row["exit_code"] == 1 and row["evidence"] == "skipped: prior failure"
        for row in packet["gates"][stop:]
    ), "Gate receipt did not record later gates as failed skips"
    assert [item[0] for item in visited] == [f"G{i}" for i in range(1, stop + 1)]
    assert packet["gates"][stop - 1]["exit_code"] == 17
