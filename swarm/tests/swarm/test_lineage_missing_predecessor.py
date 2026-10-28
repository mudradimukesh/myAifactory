import importlib
import json
import types
from pathlib import Path

import pytest
from test_evidence_write import Fixture


def red_proof_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, red_predecessor: str | None, drop: bool
) -> str:
    monkeypatch.delenv("REPO", raising=False)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/swarm"))
    module: types.ModuleType = importlib.import_module("unit_gate")
    fixture = Fixture(tmp_path)
    (fixture.art / "u-test-map.json").write_text("[]")
    fixture.red(fixture.tests)
    (fixture.art / "u-acceptance.md").write_text("```json\n{}\n```\n")
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode == 0, result.stderr
    red_path, frozen_path = fixture.art / "u-red.json", fixture.art / "u-s1-tests.json"
    red, frozen = json.loads(red_path.read_text()), json.loads(frozen_path.read_text())
    assert frozen["predecessor"] == fixture.predecessor
    if drop:
        del red["predecessor"], frozen["predecessor"]
    else:
        red["predecessor"] = red_predecessor or fixture.predecessor
    red_path.write_text(json.dumps(red))
    frozen_path.write_text(json.dumps(frozen))
    gate = module.Gate(fixture.repo, fixture.art, "u", fixture.predecessor, fixture.tests)

    def skip_execution(identifier: str, commands: list[list[str]]) -> dict[str, object]:
        return {"exit_code": 0, "measured": 0, "threshold": 0, "command": commands}

    monkeypatch.setattr(gate, "commands", skip_execution)
    monkeypatch.setattr(module, "python_rows", lambda path: [])
    try:
        gate.green()
    except ValueError as failure:
        return str(failure)
    return ""


def test_red_proof_rejects_missing_predecessor_on_both_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = red_proof_error(tmp_path, monkeypatch, None, drop=True)
    assert "Red proof differs from frozen tests" in error, (
        "red_proof accepted red and frozen records with no predecessor binding"
    )


def test_red_proof_accepts_matching_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert red_proof_error(tmp_path, monkeypatch, None, drop=False) == ""


def test_red_proof_rejects_explicit_predecessor_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = red_proof_error(tmp_path, monkeypatch, "0" * 40, drop=False)
    assert "Red proof differs from frozen tests" in error, error
