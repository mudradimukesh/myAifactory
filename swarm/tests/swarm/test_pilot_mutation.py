import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_uncovered_mutants_reduce_score_and_errors_are_not_kills():
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "mutation_gate", ROOT / "scripts/swarm/mutation_gate.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        mutants = [
            {"id": str(i), "status": status, "location": {"start": {"line": i + 1}}}
            for i, status in enumerate(["Killed", "Timeout", "Survived", "NoCoverage"])
        ]
        result = module.score_web(
            {"files": {"src/auth/session.ts": {"mutants": mutants}}},
            {"web/src/auth/session.ts": {1, 3}},
        )
        assert result["score"] == 50
        assert len(result["survivors"]) == 2
        assert result["survivors"][0]["contract_relevant"] is True
        mutants[0]["status"] = "RuntimeError"
        with pytest.raises(ValueError):
            module.score_web({"files": {"src/auth/session.ts": {"mutants": mutants}}}, {})
    finally:
        sys.path.pop(0)
