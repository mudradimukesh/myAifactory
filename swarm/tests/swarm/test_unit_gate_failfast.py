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
        "src/probe.py",
        "tests/test_probe.py",
        "web/src/probe.ts",
    ]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (tmp_path / "probe-acceptance.md").write_text(
        '```json\n{"python_test_paths": ["tests/test_probe.py"]}\n```\n'
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
        "regression": "G10",
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
    stop = next(i for i, row in enumerate(rows) if row["id"] == failure)
    assert [item[0] for item in visited] == [row["id"] for row in rows[: stop + 1]]
    assert rows[stop]["exit_code"] == 17
    assert all(
        row["disposition"] == "blocked" and row["exit_code"] == 1 for row in rows[stop + 1 :]
    )
    assert json.loads((tmp_path / "probe-survivors.json").read_text()) == {"survivors": []}


@pytest.mark.parametrize("failure", ["none", "G10", "G17", "G18"])
def test_final_gate_runs_once_and_stops_after_failure(tmp_path, recording_actions, failure):
    module, visited, codes = recording_actions
    codes[failure] = 17
    gate = module.Gate(tmp_path, tmp_path, "probe", "base", "candidate")
    gate.final = True
    rows = gate.execute()
    assert len(rows) == 18 and len({row["id"] for row in rows}) == 18
    assert len(visited) == len({item[0] for item in visited})
    stop = next((i for i, row in enumerate(rows) if row["id"] == failure), len(rows))
    assert {item[0] for item in visited} == {
        row["id"] for row in rows[: stop + 1] if row["id"] != "G11"
    }
    assert all(
        row["exit_code"] == 1 and row["disposition"] == "blocked" for row in rows[stop + 1 :]
    )
    if failure == "none":
        assert all(row["exit_code"] == 0 for row in rows)
        assert next(row for row in rows if row["id"] == "G11")["disposition"] == "deferred"
    else:
        assert rows[stop]["exit_code"] == 17
    assert not any(
        "--count=5" in command or "--repeat-each=5" in command
        for _, commands, _ in visited
        for command in commands
    )


@pytest.mark.parametrize("failure", ["none", "G1", "G4", "G8", "G15", "related"])
def test_fast_gate_defers_expensive_work_and_stops_after_failure(
    tmp_path, recording_actions, failure
):
    module, visited, codes = recording_actions
    codes[failure] = 17
    gate = module.Gate(tmp_path, tmp_path, "probe", "base", "candidate", fast=True)
    rows = gate.execute()
    assert len(rows) == 19
    assert not {item[0] for item in visited} & module.FULL_ONLY
    stop = next((i for i, row in enumerate(rows) if row["id"] == failure), len(rows))
    assert all(
        row["disposition"] == "blocked" and row["exit_code"] == 1 for row in rows[stop + 1 :]
    )
    for row in rows[:stop]:
        assert row["exit_code"] == 0
        if row["id"] in module.FULL_ONLY:
            assert row["disposition"] == "deferred"
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
    rows = packet["gates"]
    stop = next(i for i, row in enumerate(rows) if row["id"] == failure)
    assert len(rows) == 18
    assert all(
        row["exit_code"] == 1 and row["disposition"] == "blocked" for row in rows[stop + 1 :]
    )
    assert [item[0] for item in visited] == [row["id"] for row in rows[: stop + 1]]
    assert rows[stop]["exit_code"] == 17
