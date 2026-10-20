import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from test_pilot_guard import ROOT, event, state
from test_pilot_guard import guard as guard


@pytest.fixture
def candidate_pytest(tmp_path):
    s = state()
    output = str(tmp_path / "tag-code-review.json")
    s["inventory"]["sessions"] = [
        {
            "session_id": "lead-session/reviewer-id",
            "agent_id": "reviewer-id",
            "role": "reviewer",
            "parent_session_id": "lead-session",
            "output_paths": [output],
        }
    ]
    s["reservations"] = {
        "review": {
            "role": "reviewer",
            "parent_session_id": "lead-session",
            "active_run": "review-run",
            "output_paths": [output],
            "released": False,
        }
    }
    s["runs"] = {
        "review-run": {
            "allocation_id": "review",
            "session_id": "lead-session/reviewer-id",
            "agent_id": "reviewer-id",
            "complete": False,
        }
    }
    (tmp_path / "tag-candidate.json").write_text(json.dumps({"worktree": str(ROOT)}))
    command = shlex.join(
        [
            str(Path(sys.executable).parent / "pytest"),
            "-c",
            str(ROOT / "pyproject.toml"),
            "-o",
            f"pythonpath={ROOT} {ROOT / 'src'}",
            "-q",
            str(ROOT / "tests/swarm/test_reviewer_pytest_form.py")
            + "::test_documented_examples_are_allowed",
        ]
    )
    e = event("Bash", command=command)
    e["agent_id"] = "reviewer-id"
    return s, e, tmp_path


def test_reviewer_candidate_pytest_is_allowed(guard, tmp_path, candidate_pytest):
    s, e, _ = candidate_pytest
    try:
        allowed = guard.check(e, s, tmp_path / "run.json", 100, 83000)
    except ValueError:
        allowed = False
    assert allowed, e["tool_input"]["command"]


@pytest.mark.parametrize(
    "change",
    [
        "worktree",
        "config",
        "pythonpath",
        "node",
        "missing-packet",
        "malformed-packet",
        "missing-worktree",
        "relative-worktree",
        "reservation",
        "released",
        "complete",
        "other-session",
        "other-agent",
        "cd",
        ";",
        "|",
        "&",
        ">",
        "<",
        "`",
        "$",
        "\n",
        "extra-option",
    ],
)
def test_reviewer_candidate_pytest_preserves_refusals(guard, tmp_path, candidate_pytest, change):
    s, e, _ = candidate_pytest
    try:
        allowed = guard.check(e, s, tmp_path / "run.json", 100, 83000)
    except ValueError:
        allowed = False
    assert allowed, e["tool_input"]["command"]
    words = shlex.split(e["tool_input"]["command"])
    packet = tmp_path / "tag-candidate.json"
    if change == "worktree":
        packet.write_text(json.dumps({"worktree": str(tmp_path)}))
    elif change == "config":
        words[2] = str(tmp_path / "pyproject.toml")
    elif change == "pythonpath":
        words[4] = f"pythonpath={tmp_path} {tmp_path / 'src'}"
    elif change == "node":
        words[-1] = str(tmp_path / "test_x.py") + "::test_name"
    elif change == "missing-packet":
        packet.unlink()
    elif change == "malformed-packet":
        packet.write_text("[]")
    elif change == "missing-worktree":
        packet.write_text("{}")
    elif change == "relative-worktree":
        packet.write_text(json.dumps({"worktree": "."}))
    elif change == "reservation":
        s["reservations"] = {}
    elif change == "released":
        s["reservations"]["review"]["released"] = True
    elif change == "complete":
        s["runs"]["review-run"]["complete"] = True
    elif change == "other-session":
        s["runs"]["review-run"]["session_id"] = "other-session"
    elif change == "other-agent":
        s["runs"]["review-run"]["agent_id"] = "other-agent"
    elif change == "cd":
        words = ["cd", str(ROOT), *words]
    elif change == "extra-option":
        words.extend(["-o", "pythonpath=/tmp"])
    else:
        words[-1] += change
    e["tool_input"]["command"] = shlex.join(words)
    with pytest.raises(ValueError):
        guard.check(e, s, tmp_path / "run.json", 100, 83000)


@pytest.mark.parametrize("extra_options", ["-q", "--collect-only"])
def test_reviewer_candidate_pytest_collects_from_main(
    guard, tmp_path, candidate_pytest, monkeypatch, extra_options
):
    monkeypatch.setenv("PYTEST_ADDOPTS", extra_options)
    s, e, outside_candidate = candidate_pytest
    words = shlex.split(e["tool_input"]["command"])
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTEST_ADDOPTS"}
    }
    old = subprocess.run(
        [words[0], "-q", words[-1]], cwd=outside_candidate, env=env, capture_output=True, text=True
    )
    assert old.returncode != 0
    assert "ModuleNotFoundError: No module named 'scripts'" in old.stdout + old.stderr
    try:
        allowed = guard.check(e, s, tmp_path / "run.json", 100, 83000)
    except ValueError:
        allowed = False
    assert allowed, e["tool_input"]["command"]
    result = subprocess.run(words, cwd=outside_candidate, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "ModuleNotFoundError" not in result.stdout + result.stderr
