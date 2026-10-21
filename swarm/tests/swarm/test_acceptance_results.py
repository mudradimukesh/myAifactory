import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def checker():
    spec = importlib.util.spec_from_file_location(
        "acceptance_check", ROOT / "scripts/swarm/acceptance_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_browser_projects_and_repetitions_all_must_pass(checker):
    mapping = [
        {
            "id": "A1",
            "runner": "playwright",
            "test_node": "signin.spec.ts::signs in",
            "projects": ["light", "dark"],
            "oracle": "spec_literal",
            "evidence_kind": "observed",
        }
    ]
    rows = [
        {"node": "signin.spec.ts::signs in", "project": p, "status": "passed", "error": ""}
        for p in ["light", "dark"]
        for _ in range(5)
    ]
    checker.check(mapping, ["A1"], {"playwright": rows}, 5)
    with pytest.raises(ValueError):
        checker.check(mapping, ["A1"], {"playwright": rows[:-1]}, 5)
    rows[0]["status"] = "skipped"
    with pytest.raises(ValueError):
        checker.check(mapping, ["A1"], {"playwright": rows}, 5)


def test_red_requires_expected_assertion_not_setup_error(checker):
    row = {"status": "failed", "error": "AssertionError: expected Sign in"}
    assert checker.assertion_red(row, "expected Sign in")
    assert not checker.assertion_red(
        {"status": "failed", "error": "ImportError: missing"}, "missing"
    )
    assert not checker.assertion_red({"status": "skipped", "error": ""}, "")


def test_preservation_id_requires_every_editor_case(checker):
    nodes = [f"editor.spec.ts::case {i}" for i in range(11)]
    projects = ["light-keyboard", "dark-keyboard", "light-pointer", "dark-pointer"]
    mapping = [
        {
            "id": "preservation",
            "runner": "playwright",
            "test_nodes": nodes,
            "projects": projects,
            "oracle": "spec_literal",
            "preservation": True,
        }
    ]
    rows = [
        {"node": node, "project": project, "status": "passed"}
        for node in nodes
        for project in projects
    ]
    checker.check(mapping, ["preservation"], {"playwright": rows})
    with pytest.raises(ValueError):
        checker.check(mapping, ["preservation"], {"playwright": rows[:-1]})


def test_colored_playwright_assertions_keep_their_expected_message(checker):
    row = {
        "status": "failed",
        "error": "Error: \x1b[2mexpect(\x1b[22mreceived).toBe(expected)\nExpecte"
        "d: \x1b[32m1\x1b[39m",
    }
    assert checker.assertion_red(row, "Expected: 1")


def test_light_policy_preserves_frozen_map_and_requires_both_inputs(checker):
    mapping = [
        {
            "id": "A1",
            "runner": "playwright",
            "test_node": "a::works",
            "oracle": "spec_literal",
            "projects": ["light-keyboard", "dark-keyboard", "light-pointer", "dark-pointer"],
        }
    ]
    selected = checker.light_mapping(mapping)
    assert len(mapping[0]["projects"]) == 4
    rows = [
        {"node": "a::works", "project": p, "status": "passed"}
        for p in ("light-keyboard", "light-pointer")
    ]
    checker.check(selected, ["A1"], {"playwright": rows})
    with pytest.raises(ValueError):
        checker.check(selected, ["A1"], {"playwright": rows[:1]})
    with pytest.raises(ValueError, match="no light-theme"):
        checker.light_mapping([{**mapping[0], "projects": ["dark-keyboard"]}])


def test_retroactive_red_accepts_expected_failure_of_any_kind(checker):
    missing = {"status": "failed", "error": "AttributeError: no attribute 'retire_draining'"}
    assert checker.retroactive_red(missing, "AttributeError")
    assert checker.retroactive_red({"status": "failed", "error": "Failed: DID NOT RAISE"}, "Failed")
    setup = {"status": "error", "error": "FileNotFoundError: No such file or directory: '-p'"}
    assert checker.retroactive_red(setup, "FileNotFoundError")
    assert not checker.retroactive_red(missing, "ValueError")
    assert not checker.retroactive_red(missing, "")
    assert not checker.retroactive_red({"status": "passed", "error": ""}, "AttributeError")
