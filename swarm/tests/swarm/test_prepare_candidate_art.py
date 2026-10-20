import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.fixture
def candidate_art(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "file").write_text("base")
    git(repo, "add", "file")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "builder")
    (repo / "file").write_text("candidate")
    git(repo, "commit", "-am", "builder")
    git(repo, "checkout", "main")

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("prepare_candidate.sh", "integration.py", "gate_evidence.py"):
        shutil.copyfile(ROOT / "scripts/swarm" / name, scripts / name)
    # Replace only the copied default, so no invocation can reach the live run or ART.
    source = (ROOT / "scripts/swarm/run_state.py").read_text()
    original = 'DEFAULT_RUN = Path("/tmp/factory-fp-0925/swarm/interior-p1/run.json")'
    assert source.count(original) == 1
    default = tmp_path / "default"
    (scripts / "run_state.py").write_text(
        source.replace(original, f"DEFAULT_RUN = Path({str(default / 'run.json')!r})")
    )
    spec = importlib.util.spec_from_file_location(
        "candidate_art_run_state", scripts / "run_state.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = module.processes()[os.getpid()]
    state = {
        "lifecycle": "preparing",
        "active_lead": identity,
        "consumed": {"overall": 0, "preparation": 0},
        "ceilings": {"overall": 1000, "preparation": 1000},
    }
    arts = {}
    for name in ("default", "pilot", "explicit"):
        art = tmp_path / name
        art.mkdir()
        (art / "run.json").write_text(json.dumps(state))
        (art / "feature-acceptance.md").write_text(
            "```json\n" + json.dumps({"feature": name, "product_ids": []}) + "\n```\n"
        )
        (art / "feature-ids.txt").write_text(name + "\n")
        arts[name] = art
    env = dict(os.environ)
    for name in ("ART", "PILOT_RUN", "PYTHONPATH"):
        env.pop(name, None)
    env.update(
        REPO=str(repo),
        PILOT_WORKTREE_ROOT=str(tmp_path / "worktrees"),
        PATH=str(Path(sys.executable).parent) + os.pathsep + env["PATH"],
        PYTHONDONTWRITEBYTECODE="1",
    )
    command = ["/bin/zsh", str(scripts / "prepare_candidate.sh")]
    return repo, arts, env, command


@pytest.mark.parametrize("selection", ["pilot", "explicit", "default"])
def test_prepare_uses_selected_art_for_reads_and_writes(candidate_art, selection):
    repo, arts, env, command = candidate_art
    if selection != "default":
        env["PILOT_RUN"] = str(arts["pilot"] / "run.json")
    if selection == "explicit":
        env["ART"] = str(arts["explicit"])
    art = arts[selection]
    untouched = {
        name: {p.name: p.read_bytes() for p in other.iterdir()}
        for name, other in arts.items()
        if name != selection
    }
    for attempt in (1, 2):
        result = subprocess.run(
            [*command, "feature", "builder"], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        receipt_path = art / "feature-candidate.json"
        assert receipt_path.is_file(), f"Candidate JSON missing from selected ART: {art}"
        receipt = json.loads(receipt_path.read_text())
        assert receipt["feature"] == selection, "Acceptance was read from another ART"
        assert receipt["attempt"] == attempt
        assert Path(receipt["worktree"]).is_dir()
        assert receipt["builder_sha"] == git(repo, "rev-parse", "builder")
    assert (art / "integration.lock").is_file()
    archive = art / "attempts" / selection / "1"
    assert json.loads((archive / "feature-candidate.json").read_text())["attempt"] == 1
    for suffix in ("acceptance.md", "ids.txt"):
        assert (archive / ("feature-" + suffix)).read_bytes() == (
            art / ("feature-" + suffix)
        ).read_bytes()
    before = git(repo, "show-ref")
    result = subprocess.run(
        [*command, "--check", "feature"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["builder_sha"] == receipt["builder_sha"]
    assert git(repo, "show-ref") == before
    (art / "feature-ids.txt").write_text("")
    refused = subprocess.run(
        [*command, "--check", "feature"], env=env, capture_output=True, text=True
    )
    assert refused.returncode == 1
    assert refused.stderr == "REFUSED: Acceptance ids missing\n"
    for name, expected in untouched.items():
        assert {p.name: p.read_bytes() for p in arts[name].iterdir()} == expected


@pytest.mark.parametrize("args", [[], ["--check", "feature", "builder"]])
def test_both_unset_preserves_legacy_refusal(candidate_art, args):
    _, arts, env, command = candidate_art
    (arts["default"] / "feature-acceptance.md").unlink()
    result = subprocess.run([*command, *args], env=env, capture_output=True, text=True)
    legacy = subprocess.run(
        [sys.executable, str(Path(command[1]).with_name("integration.py")), "prepare", *args],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == legacy.returncode == 1
    assert (result.stdout, result.stderr) == (legacy.stdout, legacy.stderr)
    assert not (arts["default"] / "feature-candidate.json").exists()


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    ("refusal", "message"),
    [
        ("unknown_usage", "Usage is unknown for an inventoried run"),
        ("metadata", "Acceptance needs exactly one JSON metadata block"),
    ],
)
def test_refusals_are_unchanged(candidate_art, explicit, refusal, message):
    repo, arts, env, command = candidate_art
    env["PILOT_RUN"] = str(arts["pilot"] / "run.json")
    art = arts["explicit"] if explicit else arts["pilot"]
    if explicit:
        env["ART"] = str(art)
    if refusal == "unknown_usage":
        run_path = arts["pilot"] / "run.json"
        state = json.loads(run_path.read_text())
        state["runs"] = {"fixture": {"usage_status": "unknown"}}
        run_path.write_text(json.dumps(state))
    else:
        (art / "feature-acceptance.md").write_text("Missing metadata\n")
    before = git(repo, "show-ref")
    result = subprocess.run(
        [*command, "feature", "builder"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert result.stderr == f"REFUSED: {message}\n"
    assert git(repo, "show-ref") == before
    assert not Path(env["PILOT_WORKTREE_ROOT"]).exists()
    for other in arts.values():
        assert not (other / "feature-candidate.json").exists()
        assert not (other / "integration.lock").exists()
