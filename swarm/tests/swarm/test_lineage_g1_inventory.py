import importlib
import json
from pathlib import Path

import pytest
from test_evidence_amendment_lineage import correction, digest, lineage
from test_evidence_write import Fixture, git

__all__ = ["lineage"]


def amended_gate(fixture: Fixture, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/swarm"))
    module = importlib.import_module("unit_gate")
    code, error = correction(fixture)
    assert code == 0, f"approved amendment rejected: {error}"
    (fixture.art / "u-acceptance.md").write_text("```json\n{}\n```\n")
    tests = json.loads((fixture.art / "u-s1-tests.json").read_text())["tests_commit"]
    assert tests == git(fixture.repo, "rev-parse", "HEAD")
    return module.Gate(fixture.repo, fixture.art, "u", fixture.predecessor, tests)


def test_g1_accepts_valid_amendment_lineage(
    lineage: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = amended_gate(lineage, monkeypatch)
    assert (lineage.art / "u-s1-tests.sha256").read_text() == (
        f"{digest(lineage.repo / 'tests/test_unit.py')}  tests/test_unit.py\n"
    )
    assert gate.frozen()["exit_code"] == 0


def test_g1_refuses_sha256_differing_from_lineage_inventory(
    lineage: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = amended_gate(lineage, monkeypatch)
    (lineage.art / "u-s1-tests.sha256").write_text(f"{'0' * 64}  tests/test_unit.py\n")
    error = ""
    try:
        gate.frozen()
    except ValueError as failure:
        error = str(failure)
    assert "final inventory differs" in error, (
        "G1 accepted an s1-tests.sha256 that differs from the lineage inventory: " + error
    )
