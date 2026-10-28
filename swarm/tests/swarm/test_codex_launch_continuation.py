import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/swarm"))
import codex_launch


@pytest.fixture
def chain(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
        },
    )
    art = tmp_path / "art"
    art.mkdir()
    run = art / "run.json"
    run.write_text(json.dumps({"assignments": {}}))
    message = art / "brief.md"
    message.write_text("Build the unit.\n")
    env = {
        "PILOT_RUN": str(run),
        "REPO": str(repo),
        "ROLE": "builder",
        "ASSIGNMENT_ID": "unit-1",
        "ALLOCATION_ID": "unit-1",
        "ALLOWANCE": "95000",
        "FEATURE": "unit",
    }
    return art, repo, message, env


def fake(art, repo, statuses, commit=True):
    calls = []

    def supervisor(directory, tag, target, message_file, env):
        index = len(calls)
        calls.append({"tag": tag, "target": target, "message": message_file.read_text(), **env})
        status = statuses[index]
        receipt = {
            "status": status,
            "usage_status": "known",
            "remaining_processes": [],
            "allocation_id": env["ALLOCATION_ID"],
        }
        if status == "HANDOFF":
            handoff = directory / (tag + "-handoff.md")
            handoff.write_text("Next: step " + str(index if commit else 0) + "\n")
            receipt["handoff_path"] = str(handoff)
            if commit:
                subprocess.run(
                    ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", tag],
                    check=True,
                    env={
                        "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": "/usr/bin:/bin",
                    },
                )
        (directory / (tag + "-launch.json")).write_text(json.dumps(receipt))
        return 0 if status in {"REPLY", "HANDOFF"} else 1

    return calls, supervisor


def test_handoff_continues_until_reply(chain):
    art, repo, message, env = chain
    calls, supervisor = fake(art, repo, ["HANDOFF", "HANDOFF", "REPLY"])
    code = codex_launch.continue_builder(
        art, "unit-1", "claude-sonnet-5-5", message, env, supervisor
    )
    assert code == 0
    assert [c["tag"] for c in calls] == ["unit-1", "unit-1-c1", "unit-1-c2"]
    assert [c["ALLOCATION_ID"] for c in calls] == ["unit-1", "unit-1-c1", "unit-1-c2"]
    assert [c["ASSIGNMENT_ID"] for c in calls] == ["unit-1", "unit-1-c1", "unit-1-c2"]
    assert all(c["target"] == "claude-sonnet-5-5" for c in calls)
    assert calls[1]["message"].startswith("Build the unit.\n")
    assert str(art / "unit-1-handoff.md") in calls[1]["message"]
    assert str(art / "unit-1-c1-handoff.md") in calls[2]["message"]
    first = json.loads((art / "unit-1-launch.json").read_text())
    assert first["chain"] == [
        str(art / t) for t in ("unit-1-c1-launch.json", "unit-1-c2-launch.json")
    ]
    assert json.loads((art / "unit-1-c1-launch.json").read_text())["continued_from"] == str(
        art / "unit-1-launch.json"
    )


def test_chain_stops_at_the_link_limit(chain):
    art, repo, message, env = chain
    calls, supervisor = fake(art, repo, ["HANDOFF"] * 5)
    codex_launch.continue_builder(art, "unit-1", "claude-sonnet-5-5", message, env, supervisor)
    assert len(calls) == 1 + codex_launch.CONTINUE_MAX


@pytest.mark.parametrize("status", ["FAIL", "CAPACITY", "REPLY"])
def test_non_handoff_status_ends_the_chain(chain, status):
    art, repo, message, env = chain
    calls, supervisor = fake(art, repo, ["HANDOFF", status, "HANDOFF"])
    code = codex_launch.continue_builder(
        art, "unit-1", "claude-sonnet-5-5", message, env, supervisor
    )
    assert len(calls) == 2
    assert code == (0 if status == "REPLY" else 1)


@pytest.mark.parametrize(
    "defect", ["unknown-usage", "survivor", "empty-handoff", "missing-handoff"]
)
def test_unfinished_receipt_does_not_continue(chain, defect):
    art, repo, message, env = chain
    calls, base = fake(art, repo, ["HANDOFF", "REPLY"])

    def supervisor(directory, tag, target, message_file, env):
        code = base(directory, tag, target, message_file, env)
        path = directory / (tag + "-launch.json")
        receipt = json.loads(path.read_text())
        if defect == "unknown-usage":
            receipt["usage_status"] = "unknown"
        elif defect == "survivor":
            receipt["remaining_processes"] = [123]
        elif defect == "empty-handoff":
            Path(receipt["handoff_path"]).write_text("  \n")
        else:
            receipt.pop("handoff_path")
        path.write_text(json.dumps(receipt))
        return code

    codex_launch.continue_builder(art, "unit-1", "claude-sonnet-5-5", message, env, supervisor)
    assert len(calls) == 1


def test_no_progress_stops_the_chain(chain):
    art, repo, message, env = chain
    calls, supervisor = fake(art, repo, ["HANDOFF"] * 4, commit=False)
    codex_launch.continue_builder(art, "unit-1", "claude-sonnet-5-5", message, env, supervisor)
    assert len(calls) == 2


@pytest.mark.parametrize("role", ["reviewer", "architect"])
def test_only_builders_continue(chain, role):
    art, repo, message, env = chain
    calls, supervisor = fake(art, repo, ["HANDOFF", "REPLY"])
    codex_launch.continue_builder(
        art, "unit-1", "claude-sonnet-5-5", message, {**env, "ROLE": role}, supervisor
    )
    assert len(calls) == 1


def test_artifact_directory_must_hold_the_run(chain, tmp_path):
    _, repo, message, env = chain
    other = tmp_path / "other"
    other.mkdir()
    calls, supervisor = fake(other, repo, ["HANDOFF", "REPLY"])
    codex_launch.continue_builder(other, "unit-1", "claude-sonnet-5-5", message, env, supervisor)
    assert len(calls) == 1


def test_refused_link_stops_and_keeps_the_handoff_result(chain, capsys):
    art, repo, message, env = chain
    _, base = fake(art, repo, ["HANDOFF", "REPLY"])

    def supervisor(directory, tag, target, message_file, env):
        if tag != "unit-1":
            raise ValueError("Dispatch frozen")
        return base(directory, tag, target, message_file, env)

    code = codex_launch.continue_builder(
        art, "unit-1", "claude-sonnet-5-5", message, env, supervisor
    )
    assert code == 0
    assert "CHAIN_STOP unit-1-c1 Dispatch frozen" in capsys.readouterr().out


def test_resumed_builder_continues_with_its_assigned_model(chain):
    art, repo, message, env = chain
    run = Path(env["PILOT_RUN"])
    run.write_text(json.dumps({"assignments": {"unit-1": {"model": "claude-sonnet-5-5"}}}))
    calls, supervisor = fake(art, repo, ["HANDOFF", "REPLY"])
    codex_launch.continue_builder(art, "unit-1", "resume:abc", message, env, supervisor)
    assert [c["target"] for c in calls] == ["resume:abc", "claude-sonnet-5-5"]


def test_admitted_link_failure_fails_the_chain_and_keeps_its_receipt(chain, capsys):
    art, repo, message, env = chain
    _, base = fake(art, repo, ["HANDOFF", "REPLY"])

    def supervisor(directory, tag, target, message_file, env):
        code = base(directory, tag, target, message_file, env)
        if tag != "unit-1":
            raise ValueError("record-run failed")
        return code

    code = codex_launch.continue_builder(
        art, "unit-1", "claude-sonnet-5-5", message, env, supervisor
    )
    assert code == 1
    assert "CHAIN_FAIL unit-1-c1 record-run failed" in capsys.readouterr().out
    first = json.loads((art / "unit-1-launch.json").read_text())
    assert first["chain"] == [str(art / "unit-1-c1-launch.json")]
