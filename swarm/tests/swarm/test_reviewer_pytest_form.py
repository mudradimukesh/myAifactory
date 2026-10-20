import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from scripts.swarm.run_state import RunState

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def guard() -> Iterator[ModuleType]:
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "pilot_guard", ROOT / "scripts/swarm/pilot_guard.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.pop(0)


@pytest.fixture
def reviewer_state() -> RunState:
    return {
        "lifecycle": "running",
        "blockers": [],
        "deadlines": {},
        "active_lead": {"pid": 42, "model_session_id": "lead-session"},
        "dispatch_frozen": False,
        "inventory": {
            "sessions": [
                {
                    "session_id": "review",
                    "role": "reviewer",
                    "parent_session_id": "lead-session",
                    "output_paths": [],
                }
            ]
        },
    }


def documented_commands(section: str) -> list[str]:
    document = (ROOT / ".claude/agents/p1-pilot-reviewer.md").read_text()
    block = re.search(rf"{re.escape(section)}:\n\n```sh\n(.*?)\n```", document, re.DOTALL)
    assert block is not None, f"Missing {section.lower()} fenced command block"
    commands = block.group(1).splitlines()
    assert commands, f"Empty {section.lower()} fenced command block"
    return commands


def test_documented_examples_are_allowed(
    guard: ModuleType, reviewer_state: RunState, tmp_path: Path
) -> None:
    for command in documented_commands("Allowed examples"):
        assert guard.check(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "session_id": "review",
            },
            reviewer_state,
            tmp_path / "run.json",
            100,
            83000,
        ), command


def test_documented_forms_are_refused(
    guard: ModuleType, reviewer_state: RunState, tmp_path: Path
) -> None:
    refusals = {
        "pytest -q path/test_x.py": "Reviewer pytest requires explicit test node ids",
        "python -m pytest -q path/test_x.py::test_name": (
            "Reviewer may only check the packet and write registered outputs"
        ),
        "pytest --rootdir /path/to/candidate path/test_x.py::test_name": (
            "Reviewer pytest requires explicit test node ids"
        ),
        "PYTHONPATH=src pytest -q path/test_x.py::test_name": (
            "Reviewer may only check the packet and write registered outputs"
        ),
    }
    commands = documented_commands("Refused forms")
    assert set(commands) == set(refusals)
    for command in commands:
        with pytest.raises(ValueError, match=f"^{re.escape(refusals[command])}$"):
            guard.check(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                    "session_id": "review",
                },
                reviewer_state,
                tmp_path / "run.json",
                100,
                83000,
            )
