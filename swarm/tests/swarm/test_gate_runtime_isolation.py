import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/swarm"))
import gate_evidence  # noqa: E402
import unit_gate  # noqa: E402

sys.path.pop(0)


def test_each_stateful_gate_gets_its_own_runtime(tmp_path, monkeypatch):
    opened, closed = [], []

    @contextmanager
    def runtime(root, art, tag, env):
        opened.append(tag)
        try:
            yield {"INTERIOR_DATABASE_URL": "db-" + tag}
        finally:
            closed.append(tag)

    monkeypatch.setattr(unit_gate, "runtime", runtime)
    gate = object.__new__(unit_gate.Gate)
    gate.root, gate.art, gate.tag, gate.fast = tmp_path, tmp_path, "unit", False
    gate.env = {"BASE": "1"}
    gate.runtime_env = {"BASE": "1"}
    seen = []

    def action():
        seen.append(dict(gate.env))
        gate.env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = "per-gate"
        return {"exit_code": 0}

    gate.isolated("G10", action)()
    gate.isolated("G17", action)()
    assert opened == closed == ["unit-G10", "unit-G17"]
    assert [s["INTERIOR_DATABASE_URL"] for s in seen] == ["db-unit-G10", "db-unit-G17"]
    assert gate.env == {"BASE": "1"}

    def broken():
        raise ValueError("gate crashed")

    with pytest.raises(ValueError, match="gate crashed"):
        gate.isolated("G2", broken)()
    assert closed[-1] == "unit-G2"
    assert gate.env == {"BASE": "1"}
    assert {"G2", "G10", "G17", "related"} <= unit_gate.STATEFUL
    assert not {"G4", "G14", "G15", "G16"} & unit_gate.STATEFUL


def test_runtime_drops_only_its_database_when_setup_fails(tmp_path, monkeypatch):
    calls = []

    def run(command, *args, **kwargs):
        calls.append(command)
        if "alembic" in command:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    monkeypatch.setattr(gate_evidence.subprocess, "run", run)
    with (
        pytest.raises(subprocess.CalledProcessError),
        gate_evidence.runtime(tmp_path, tmp_path, "unit-G10", {}),
    ):
        pass
    created = [c[-1] for c in calls if c[0] == "createdb"]
    dropped = [c[-1] for c in calls if c[0] == "dropdb"]
    assert len(created) == 1 and dropped == created
    assert '"database_dropped": true' in (tmp_path / "unit-G10-runtime.json").read_text()
