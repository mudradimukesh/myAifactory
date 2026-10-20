import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from test_pilot_guard import guard as guard


@pytest.fixture
def starting_reviewer(
    guard: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, dict[str, str]]]:
    snapshot = {
        "lifecycle": "running",
        "blockers": [],
        "deadlines": {},
        "active_lead": {"pid": 42, "model_session_id": "lead-session"},
        "dispatch_frozen": False,
        "consumed": {"overall": 0, "pilot": 0},
        "ceilings": {"overall": 300, "pilot": 300},
        "pilot_started_at": "now",
        "runs": {
            "review-run": {
                "allocation_id": "review",
                "stage": "pilot",
                "new_tokens": 0,
                "cache_reads": 0,
            }
        },
        "reservations": {
            "review": {
                "role": "reviewer",
                "active_run": "review-run",
                "allowance": 60,
                "observed": 0,
                "released": False,
                "parent_session_id": "lead-session",
                "output_paths": [str(tmp_path / "feature-code-review.json")],
            }
        },
        "inventory": {"sessions": [], "threads": []},
    }
    run = tmp_path / "run.json"
    run.write_text(json.dumps(snapshot))
    monkeypatch.setattr(guard, "originating_lead", lambda state: state["active_lead"])
    monkeypatch.setattr(
        sys.modules["run_state"], "originating_lead", lambda state: state["active_lead"]
    )
    parent = tmp_path / "lead-session.jsonl"
    parent.write_text("")
    event = {
        "session_id": "lead-session",
        "agent_id": "reviewer",
        "agent_type": "p1-pilot-reviewer",
        "transcript_path": str(parent),
        "hook_event_name": "SubagentStart",
    }
    yield run, event


def test_start_records_known_zero_usage(
    guard: ModuleType, starting_reviewer: tuple[Path, dict[str, str]]
) -> None:
    run, event = starting_reviewer
    transcript = run.parent / "lead-session/subagents/agent-reviewer.jsonl"
    assert not transcript.exists()
    guard.reviewer_event(event, run)
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "ALLOCATION: review"}})
        + "\n"
    )
    guard.reviewer_event({**event, "hook_event_name": "PreToolUse"}, run)
    saved = json.loads(run.read_text())
    recorded = saved["runs"]["review-run"]
    assert recorded["new_tokens"] == 0
    assert recorded["cache_reads"] == 0
    assert recorded["usage_status"] == "known", "Starting reviewer usage must be known zero"
    assert recorded["complete"] is False
    assert recorded["transcript_path"] == str(transcript)
    assert saved["consumed"] == {"overall": 0, "pilot": 0}
    assert saved["inventory"]["sessions"][0]["complete"] is False
    assert saved["reservations"]["review"]["active_run"] == "review-run"
    assert saved["reservations"]["review"]["released"] is False


def test_start_preserves_reviewer_serialization_and_builder_admission(
    guard: ModuleType, starting_reviewer: tuple[Path, dict[str, str]]
) -> None:
    run, event = starting_reviewer
    guard.reviewer_event(event, run)
    transcript = run.parent / "lead-session/subagents/agent-reviewer.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "ALLOCATION: review"}})
        + "\n"
    )
    guard.reviewer_event({**event, "hook_event_name": "PreToolUse"}, run)
    run.with_name("second-candidate.json").write_text(
        json.dumps({"expected_review_modes": ["code"]})
    )
    # Known usage allows independent builders, but does not release the reviewer slot.
    with pytest.raises(ValueError, match="one reserved allocation"):
        guard.transact(
            run,
            "launcher",
            "admit",
            {
                "role": "reviewer",
                "allocation_id": "second-reviewer",
                "run_id": "second-reviewer-run",
                "allowance": 60,
                "tag": "second",
            },
        )
    guard.transact(
        run,
        "launcher",
        "admit",
        {
            "role": "builder",
            "allocation_id": "second-builder",
            "run_id": "second-builder-run",
            "allowance": 60,
        },
    )
    saved = json.loads(run.read_text())
    assert "second-reviewer" not in saved["reservations"]
    assert saved["reservations"]["second-builder"]["active_run"] == "second-builder-run"
    assert saved["reservations"]["review"]["output_paths"] == [
        str(run.with_name("feature-code-review.json"))
    ]
    assert saved["runs"]["review-run"]["complete"] is False
    assert saved["consumed"] == {"overall": 0, "pilot": 0}
    with pytest.raises(ValueError, match="Allocation requires orchestrator reassessment"):
        guard.transact(
            run,
            "launcher",
            "admit",
            {"role": "builder", "allocation_id": "over", "run_id": "over", "allowance": 181},
        )
    for index in range(7):
        guard.transact(
            run,
            "launcher",
            "admit",
            {
                "role": "builder",
                "allocation_id": "slot-" + str(index),
                "run_id": "slot-" + str(index),
                "allowance": 1,
            },
        )
    with pytest.raises(ValueError, match="Eight builders are already active"):
        guard.transact(
            run,
            "launcher",
            "admit",
            {"role": "builder", "allocation_id": "ninth", "run_id": "ninth", "allowance": 1},
        )


def test_stop_records_deduplicated_final_usage(
    guard: ModuleType, starting_reviewer: tuple[Path, dict[str, str]]
) -> None:
    run, event = starting_reviewer
    guard.reviewer_event(event, run)
    bound = run.parent / "lead-session/subagents/agent-reviewer.jsonl"
    bound.parent.mkdir(parents=True)
    bound.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "ALLOCATION: review"}})
        + "\n"
    )
    guard.reviewer_event({**event, "hook_event_name": "PreToolUse"}, run)
    transcript = run.parent / "reviewer.jsonl"
    first = {
        "message": {
            "id": "m1",
            "usage": {
                "input_tokens": 7,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 20,
                "output_tokens": 2,
            },
        }
    }
    second = {"message": {"id": "m2", "usage": {"input_tokens": 1, "output_tokens": 1}}}
    transcript.write_text("\n".join(json.dumps(row) for row in (first, first, second)) + "\n")
    event.update(hook_event_name="SubagentStop", agent_transcript_path=str(transcript))
    guard.reviewer_event(event, run)
    guard.reviewer_event(event, run)
    saved = json.loads(run.read_text())
    recorded = saved["runs"]["review-run"]
    assert recorded["new_tokens"] == 14
    assert recorded["cache_reads"] == 20
    assert recorded["usage_status"] == "known"
    assert recorded["complete"] is True
    assert saved["consumed"] == {"overall": 14, "pilot": 14}
    assert saved["reservations"]["review"]["observed"] == 14
    assert saved["reservations"]["review"]["released"] is True
    assert saved["reservations"]["review"]["active_run"] is None
    assert saved["inventory"]["sessions"][0]["complete"] is True


@pytest.mark.parametrize("transcript_kind", ["missing", "empty", "unmeasurable"])
def test_stop_unknown_usage_blocks_admission(
    guard: ModuleType,
    starting_reviewer: tuple[Path, dict[str, str]],
    transcript_kind: str,
) -> None:
    run, event = starting_reviewer
    guard.reviewer_event(event, run)
    bound = run.parent / "lead-session/subagents/agent-reviewer.jsonl"
    bound.parent.mkdir(parents=True)
    bound.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "ALLOCATION: review"}})
        + "\n"
    )
    guard.reviewer_event({**event, "hook_event_name": "PreToolUse"}, run)
    transcript = run.parent / "reviewer.jsonl"
    if transcript_kind == "empty":
        transcript.write_text("")
    elif transcript_kind == "unmeasurable":
        transcript.write_text('invalid json\n{"message": {"id": "m1"}}\n')
    event.update(hook_event_name="SubagentStop", agent_transcript_path=str(transcript))
    guard.reviewer_event(event, run)
    saved = json.loads(run.read_text())
    recorded = saved["runs"]["review-run"]
    assert recorded["usage_status"] == "unknown"
    assert recorded["complete"] is True
    assert recorded["new_tokens"] == 0
    assert recorded["cache_reads"] == 0
    assert saved["reservations"]["review"]["released"] is True
    assert saved["inventory"]["sessions"][0]["complete"] is True
    for role in ("reviewer", "builder"):
        with pytest.raises(ValueError, match="Usage is unknown for an inventoried run"):
            guard.transact(
                run,
                "launcher",
                "admit",
                {
                    "role": role,
                    "allocation_id": "second-" + role,
                    "run_id": "second-" + role + "-run",
                    "allowance": 60,
                },
            )
