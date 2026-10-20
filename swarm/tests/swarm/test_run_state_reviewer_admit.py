import json
import os
from pathlib import Path

import pytest
from test_run_state import bootstrap, invoke

from scripts.swarm import run_state


@pytest.fixture
def reviewer_run(tmp_path: Path) -> Path:
    path = tmp_path / "run.json"
    state = bootstrap(path)
    state["active_lead"] = {
        **run_state.processes()[os.getpid()],
        "model_session_id": "parent",
    }
    path.write_text(json.dumps(state))
    path.with_name("candidate-candidate.json").write_text(
        json.dumps({"expected_review_modes": ["code", "integration"]})
    )
    result = invoke(
        path,
        "launcher",
        "admit",
        {
            "allocation_id": "first",
            "run_id": "first-run",
            "role": "reviewer",
            "allowance": 20,
            "tag": "candidate",
        },
    )
    assert result.returncode == 0, result.stderr
    reservation = json.loads(path.read_text())["reservations"]["first"]
    assert reservation["parent_session_id"] == "parent"
    assert reservation["output_paths"] == [
        str(path.with_name("candidate-code-review.json").resolve()),
        str(path.with_name("candidate-integration-review.json").resolve()),
    ]
    return path


def test_second_reviewer_is_refused_without_changing_run(reviewer_run: Path) -> None:
    before = reviewer_run.read_bytes()
    result = invoke(
        reviewer_run,
        "launcher",
        "admit",
        {
            "allocation_id": "second",
            "run_id": "second-run",
            "role": "reviewer",
            "allowance": 20,
            "tag": "candidate",
        },
    )
    assert result.returncode != 0
    assert "Reviewer needs one reserved allocation with exact output paths" in result.stderr
    assert reviewer_run.read_bytes() == before


def test_second_reviewer_after_release_is_admitted(reviewer_run: Path) -> None:
    result = invoke(
        reviewer_run,
        "launcher",
        "release",
        {"allocation_id": "first", "run_id": "first-run"},
    )
    assert result.returncode == 0, result.stderr
    result = invoke(
        reviewer_run,
        "launcher",
        "admit",
        {
            "allocation_id": "second",
            "run_id": "second-run",
            "role": "reviewer",
            "allowance": 20,
            "tag": "candidate",
        },
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(reviewer_run.read_text())
    assert state["reservations"]["first"]["released"] is True
    assert state["reservations"]["second"]["active_run"] == "second-run"


def test_builder_beside_unreleased_reviewer_is_admitted(reviewer_run: Path) -> None:
    result = invoke(
        reviewer_run,
        "launcher",
        "admit",
        {
            "allocation_id": "builder",
            "run_id": "builder-run",
            "role": "builder",
            "allowance": 20,
        },
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(reviewer_run.read_text())
    assert state["reservations"]["first"]["released"] is False
    assert state["reservations"]["builder"]["active_run"] == "builder-run"


def test_same_reviewer_admission_is_idempotent(reviewer_run: Path) -> None:
    result = invoke(
        reviewer_run,
        "launcher",
        "admit",
        {
            "allocation_id": "first",
            "run_id": "first-run",
            "role": "reviewer",
            "allowance": 20,
            "tag": "candidate",
        },
    )
    assert result.returncode == 0, result.stderr
    assert list(json.loads(reviewer_run.read_text())["reservations"]) == ["first"]


@pytest.mark.parametrize("changes", [{"parent_session_id": "other"}, {"active_run": None}])
def test_second_reviewer_outside_live_parent_reservations_is_admitted(
    reviewer_run: Path, changes: dict[str, object]
) -> None:
    state = json.loads(reviewer_run.read_text())
    state["reservations"]["first"].update(changes)
    reviewer_run.write_text(json.dumps(state))
    result = invoke(
        reviewer_run,
        "launcher",
        "admit",
        {
            "allocation_id": "second",
            "run_id": "second-run",
            "role": "reviewer",
            "allowance": 20,
            "tag": "candidate",
        },
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(reviewer_run.read_text())["reservations"]["second"]["active_run"] == (
        "second-run"
    )
