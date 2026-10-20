import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILDER_PARAGRAPH = (
    "User rule (October 13, RULING573j, highest priority): never stop mid-turn to ask a "
    "question. If a frozen test, allowed path, acceptance line or brief instruction conflicts "
    'with another, or a scope question arises, record it in your handoff under "Open questions". '
    "Give the exact evidence (file:line, node id, failing message) and your proposed answer. "
    "Skip only the blocked item and continue every other in-scope item until all are done or "
    "you reach the handoff cutoff. Never edit a frozen test or write outside the allowed paths "
    "to get around a conflict. The lead answers open questions in the next brief. Stop early "
    "only on a preflight mismatch checked before any edit (wrong HEAD or unexpected dirty "
    "files), at the handoff cutoff, or on a guard denial after two retries."
)
CAPACITY_ERROR = {
    "message": "Selected model is at capacity. Please try a different model.",
    "codex_error_info": "server_overloaded",
}
USAGE = {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 3}


@pytest.fixture
def launch(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    source = tmp_path / "source"
    launcher = source / "scripts/swarm/codex_launch.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text((ROOT / "scripts/swarm/codex_launch.py").read_text())
    builder_brief = source / "prompts/phase-1/builder-brief.md"
    builder_brief.parent.mkdir(parents=True)
    original_brief = (ROOT / "prompts/phase-1/builder-brief.md").read_text()
    builder_brief.write_text(original_brief + "\n" + BUILDER_PARAGRAPH + "\n")
    spec = importlib.util.spec_from_file_location("capacity_codex_launch", launcher)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    import run_state

    transactions = []
    transact = run_state.transact

    def recorded_transact(path, role, operation, data):
        result = transact(path, role, operation, data)
        transactions.append((operation, dict(data)))
        return result

    monkeypatch.setattr(run_state, "transact", recorded_transact)

    def run(tag, error=None, role="tester", brief="Implement the assigned task."):
        work = tmp_path / tag
        work.mkdir()
        checkout_brief = work / "prompts/phase-1/builder-brief.md"
        checkout_brief.parent.mkdir(parents=True)
        checkout_brief.write_text(builder_brief.read_text())
        stub = work / "codex"
        stub.write_text(
            "#!"
            + sys.executable
            + "\n"
            + """import json
import os
import sys
from pathlib import Path

error = json.loads(os.environ['FAKE_ERROR'])
Path('captured-prompt.txt').write_text(sys.argv[-1])
reply = Path(sys.argv[sys.argv.index('-o') + 1])
rollout = Path(os.environ['CODEX_HOME']) / 'sessions/rollout-test-thread.jsonl'
rollout.parent.mkdir(parents=True, exist_ok=True)
completion = {'type': 'task_complete', 'last_agent_message': None if error else 'done'}
if error:
    completion['error'] = error
rows = [
    {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra', 'effort': 'low'}},
    {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'total_token_usage': {'input_tokens': 20, 'cached_input_tokens': 5, 'output_tokens': 3}
    }}},
    {'type': 'event_msg', 'payload': completion},
]
rollout.write_text(''.join(json.dumps(row) + '\\n' for row in rows))
print(json.dumps({'type': 'thread.started', 'thread_id': 'test-thread'}), flush=True)
if not error:
    reply.write_text('done')
sys.exit(1 if error else 0)
"""
        )
        stub.chmod(0o755)
        run_path = work / "run.json"
        run_path.write_text(
            json.dumps(
                {
                    "lifecycle": "preparing",
                    "blockers": [],
                    "deadlines": {},
                    "ceilings": {"overall": 1000, "preparation": 1000},
                    "consumed": {"overall": 0, "preparation": 0},
                    "active_lead": None,
                    "dispatch_frozen": False,
                    "reservations": {},
                    "inventory": {"threads": [], "sessions": []},
                    "assignments": {
                        "a": {"model": "gpt-6-astra", "effort": "low", "reason": "test"}
                    },
                }
            )
        )
        message = work / "brief.txt"
        message.write_text(brief)
        env = {
            "PATH": str(work) + os.pathsep + os.environ["PATH"],
            "PILOT_RUN": str(run_path),
            "ASSIGNMENT_ID": "a",
            "ALLOCATION_ID": "allocation",
            "ALLOWANCE": "100",
            "ROLE": role,
            "FEATURE": "fixture",
            "REPO": str(work),
            "CODEX_HOME": str(work / "codex-home"),
            "FAKE_ERROR": json.dumps(error),
            "START_S": "2",
            "IDLE_S": "2",
            "PILOT_TICK": "0.02",
            "PILOT_STOP_INT": "0.1",
            "PILOT_STOP_TERM": "0.1",
        }
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(
            sys, "argv", [str(launcher), str(work), tag, "gpt-6-astra", str(message)]
        )
        transactions.clear()
        result = worker.main()
        output = capsys.readouterr()
        receipt_path = work / (tag + "-launch.json")
        return {
            "code": result,
            "stdout": output.out,
            "stderr": output.err,
            "receipt": json.loads(receipt_path.read_text()) if receipt_path.exists() else None,
            "state": json.loads(run_path.read_text()),
            "transactions": list(transactions),
            "prompt_path": work / "captured-prompt.txt",
        }

    run.builder_brief = builder_brief
    run.original_brief = original_brief
    return run


def test_cs_reason_copies_capacity_error_from_task_complete(launch):
    result = launch("capacity-reason", CAPACITY_ERROR)
    assert result["receipt"]["stop_reason"] == "server_overloaded", (
        "CS-REASON: task_complete codex_error_info must become receipt stop_reason"
    )


def test_cs_status_capacity_preserves_accounting_and_release(launch):
    result = launch("capacity-status", CAPACITY_ERROR)
    receipt = result["receipt"]
    assert result["code"] != 0
    assert receipt["usage_delta"] == {"new_tokens": 18, "cache_reads": 5}
    assert receipt["final_cumulative_usage"] == USAGE
    assert receipt["remaining_processes"] == []
    operations = result["transactions"]
    assert [operation for operation, data in operations][-2:] == ["record-run", "release"]
    assert operations[-2][1]["complete"] is True
    assert operations[-2][1]["run_id"] == "capacity-status"
    assert operations[-1][1] == {"allocation_id": "allocation", "run_id": "capacity-status"}
    state = result["state"]
    assert state["runs"]["capacity-status"]["complete"] is True
    assert state["runs"]["capacity-status"]["new_tokens"] == 18
    assert state["runs"]["capacity-status"]["cache_reads"] == 5
    assert state["reservations"]["allocation"]["observed"] == 18
    assert state["reservations"]["allocation"]["released"] is True
    assert state["reservations"]["allocation"]["active_run"] is None
    assert state["consumed"] == {"overall": 18, "preparation": 18}
    assert (receipt["status"], result["stdout"].splitlines()[-1].split()[0]) == (
        "CAPACITY",
        "CAPACITY",
    ), "CS-STATUS: receipt and final output must name CAPACITY"


def test_cs_other_error_reason_and_success_receipt(launch):
    success = launch("success")
    receipt = success["receipt"]
    assert success["code"] == 0
    assert receipt["status"] == "REPLY"
    assert receipt["stop_reason"] is None
    assert receipt["signals"] == []
    assert receipt["child_exit"] == 0
    assert receipt["usage_status"] == "known"
    assert receipt["usage_delta"] == {"new_tokens": 18, "cache_reads": 5}
    assert receipt["final_cumulative_usage"] == USAGE
    assert receipt["remaining_processes"] == []
    assert success["stdout"].splitlines()[-1] == "REPLY success exit=0"
    other = launch("other-error", {"message": "Fixture error", "codex_error_info": "other_error"})
    assert other["code"] != 0
    assert other["receipt"]["status"] == "FAIL"
    assert other["stdout"].splitlines()[-1].split()[0] == "FAIL"
    assert other["receipt"]["stop_reason"] == "other_error", (
        "CS-OTHER: non-capacity task_complete errors must retain codex_error_info"
    )


def test_cs_brief_builder_paragraph_and_missing_source(launch):
    issues = []
    brief = "Implement the assigned task."
    appended = launch("builder-append", role="builder", brief=brief)
    assert appended["code"] == 0, appended["stdout"] + appended["stderr"]
    prompt = appended["prompt_path"].read_text()
    if not (prompt.startswith(brief) and prompt.count(BUILDER_PARAGRAPH) == 1):
        issues.append("builder prompt must append the source paragraph once")
    supplied = brief + "\n\n" + BUILDER_PARAGRAPH
    duplicate = launch("builder-existing", role="builder", brief=supplied)
    if duplicate["prompt_path"].read_text() != supplied:
        issues.append("builder prompt with the paragraph must stay unchanged")
    for role in ("tester", "reviewer", "lead"):
        other = launch("unchanged-" + role, role=role, brief=brief)
        if other["prompt_path"].read_text() != brief:
            issues.append(role + " prompt must stay unchanged")
    launch.builder_brief.write_text(
        "\n\n".join(
            paragraph
            for paragraph in launch.original_brief.split("\n\n")
            if not any(
                line.startswith("User rule (October 13, RULING573j")
                for line in paragraph.splitlines()
            )
        )
    )
    missing = launch("builder-missing", role="builder", brief=brief)
    diagnostic = missing["stdout"] + missing["stderr"]
    if missing["code"] == 0:
        issues.append("missing source paragraph must return nonzero")
    if missing["prompt_path"].exists():
        issues.append("missing source paragraph must stop before the child starts")
    if not ("RULING573j" in diagnostic and "builder-brief.md" in diagnostic):
        issues.append("missing source paragraph must name RULING573j and builder-brief.md")
    assert issues == [], "CS-BRIEF: " + "; ".join(issues)
