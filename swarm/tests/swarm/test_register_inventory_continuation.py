import importlib
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("continued_session_id", "expected_exit"),
    [("previous-lead", 0), ("", 2), ("   ", 2), (None, 2), (17, 2), ("continued-lead", 2)],
    ids=["registered", "empty", "blank", "null", "non-string", "self"],
)
def test_register_inventory_continued_session(
    tmp_path: Path, continued_session_id: object, expected_exit: int
) -> None:
    path = tmp_path / "run.json"
    run_state = importlib.import_module("test_run_state")
    run_state.bootstrap(path)
    registration = {
        "session_id": "continued-lead",
        "continued_session_id": continued_session_id,
        "role": "lead",
        "pid": 86416,
        "pane_id": 132,
        "transcript_path": str(tmp_path / "continued-lead.jsonl"),
        "started_at": "2026-10-13T08:03:00Z",
    }
    before = path.read_bytes()
    result = run_state.invoke(path, "orchestrator", "register-inventory", registration)
    assert result.returncode == expected_exit, (
        "Invalid continuation binding accepted: " + result.stderr
    )
    if expected_exit:
        assert result.stderr.strip() == (
            "REFUSED: Inventory continued_session_id must name a different session"
        )
        assert path.read_bytes() == before
    else:
        assert json.loads(path.read_text())["inventory"]["sessions"] == [registration]
        repeated = run_state.invoke(path, "orchestrator", "register-inventory", registration)
        assert repeated.returncode == 0, repeated.stderr
