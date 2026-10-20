import contextlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.fixture
def base_fixture(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    monkeypatch.delenv("PILOT_BASE_REF", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "file").write_text("base")
    git(repo, "add", "file")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "pre-main")
    (repo / "pre-main-file").write_text("pre-main")
    git(repo, "add", "pre-main-file")
    git(repo, "commit", "-m", "pre-main")
    git(repo, "checkout", "-b", "builder", "main")
    (repo / "file").write_text("candidate")
    git(repo, "commit", "-am", "builder")
    git(repo, "checkout", "main")
    git(repo, "tag", "blob-base", git(repo, "rev-parse", "main:file"))
    art = tmp_path / "art"
    art.mkdir()
    (art / "feature-acceptance.md").write_text(
        '```json\n{"feature":"feature","product_ids":[]}\n```\n'
    )
    (art / "feature-ids.txt").write_text("A1\n")
    identity = importlib.import_module("run_state").processes()[os.getpid()]
    (art / "run.json").write_text(
        json.dumps(
            {
                "lifecycle": "preparing",
                "blockers": [],
                "deadlines": {},
                "active_lead": identity,
                "dispatch_frozen": False,
                "consumed": {"overall": 0, "preparation": 0},
                "ceilings": {"overall": 1000, "preparation": 1000},
                "reservations": {},
            }
        )
    )
    for name, value in {
        "REPO": repo,
        "ART": art,
        "PILOT_RUN": art / "run.json",
        "PILOT_WORKTREE_ROOT": tmp_path / "worktrees",
    }.items():
        monkeypatch.setenv(name, str(value))
    return repo, art


@pytest.mark.parametrize("base_ref", [None, "main", "pre-main"])
def test_prepare_base_ref_and_readonly_check(base_fixture, monkeypatch, tmp_path, base_ref):
    repo, art = base_fixture
    if base_ref is not None:
        monkeypatch.setenv("PILOT_BASE_REF", base_ref)
    expected_ref = base_ref if base_ref is not None else "main"
    expected_sha = git(repo, "rev-parse", expected_ref)
    assert git(repo, "rev-parse", "main") != git(repo, "rev-parse", "pre-main")
    command = ["/bin/zsh", str(ROOT / "scripts/swarm/prepare_candidate.sh")]
    refs = git(repo, "show-ref")
    check = subprocess.run(
        [*command, "--check", "feature", "builder"], capture_output=True, text=True
    )
    assert check.returncode == 0, check.stderr
    assert json.loads(check.stdout)["main_sha"] == expected_sha
    assert git(repo, "show-ref") == refs
    assert not (tmp_path / "worktrees").exists()
    assert not (art / "feature-candidate.json").exists()
    result = subprocess.run([*command, "feature", "builder"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((art / "feature-candidate.json").read_text())
    assert receipt["main_sha"] == expected_sha
    assert receipt.get("base_ref") == expected_ref
    worktree = Path(receipt["worktree"])
    assert git(worktree, "rev-parse", "HEAD^1") == expected_sha
    assert git(worktree, "rev-parse", "HEAD^2") == git(repo, "rev-parse", "builder")
    assert git(worktree, "rev-parse", "HEAD") == receipt["candidate_sha"]
    if expected_ref == "pre-main":
        assert (worktree / "pre-main-file").read_text() == "pre-main"
    check = subprocess.run([*command, "--check", "feature"], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    assert json.loads(check.stdout)["main_sha"] == expected_sha


@pytest.mark.parametrize("base_ref", ["missing-base", "blob-base", ""])
@pytest.mark.parametrize("check_only", [False, True])
def test_prepare_invalid_base_ref_has_no_side_effects(
    base_fixture, monkeypatch, tmp_path, base_ref, check_only
):
    repo, art = base_fixture
    integration = importlib.import_module("integration")
    receipt = art / "feature-candidate.json"
    receipt.write_text(json.dumps({"feature": "feature", "attempt": 1}))
    before = {path.name: path.read_bytes() for path in art.iterdir()}
    refs = git(repo, "show-ref")
    worktrees = git(repo, "worktree", "list", "--porcelain")
    monkeypatch.setenv("PILOT_BASE_REF", base_ref)
    with pytest.raises(ValueError, match="base ref"):
        integration.prepare(repo, art, art / "run.json", "feature", "builder", check_only)
    assert git(repo, "show-ref") == refs
    assert git(repo, "worktree", "list", "--porcelain") == worktrees
    assert {path.name: path.read_bytes() for path in art.iterdir()} == before
    assert not (tmp_path / "worktrees").exists()
    assert not (art / "attempts").exists()


def test_prepare_resolves_base_again_under_lock(base_fixture, monkeypatch):
    repo, art = base_fixture
    integration = importlib.import_module("integration")
    monkeypatch.setenv("PILOT_BASE_REF", "pre-main")
    old_sha = git(repo, "rev-parse", "pre-main")
    git(repo, "checkout", "pre-main")
    (repo / "later-file").write_text("later")
    git(repo, "add", "later-file")
    git(repo, "commit", "-m", "later base")
    new_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    git(repo, "branch", "-f", "pre-main", old_sha)

    @contextlib.contextmanager
    def lock(art):
        git(repo, "branch", "-f", "pre-main", new_sha)
        yield

    monkeypatch.setattr(integration, "integration_lock", lock)
    receipt = integration.prepare(repo, art, art / "run.json", "feature", "builder")
    assert receipt["main_sha"] == new_sha
    assert git(Path(receipt["worktree"]), "rev-parse", "HEAD^1") == new_sha


@pytest.mark.parametrize("base_ref", [None, "main", "pre-main"])
def test_baseline_uses_base_ref(base_fixture, monkeypatch, base_ref):
    repo, art = base_fixture
    gate = importlib.import_module("unit_gate")
    if base_ref is not None:
        monkeypatch.setenv("PILOT_BASE_REF", base_ref)
    expected_sha = git(repo, "rev-parse", base_ref if base_ref is not None else "main")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["unit_gate", "baseline", "feature"])
    provisioned = []
    inspected = []

    def provision(root, log):
        provisioned.append(root)
        assert git(root, "rev-parse", "HEAD") == expected_sha
        assert git(root, "branch", "--show-current") == ""
        return {"fixture": "baseline"}

    def baseline(root, actual_art, tag, env):
        inspected.append(root)
        assert actual_art == art
        assert tag == "feature"
        assert env == {"fixture": "baseline"}
        assert git(root, "rev-parse", "HEAD") == expected_sha
        return False

    monkeypatch.setattr(gate, "provision", provision)
    monkeypatch.setattr(gate, "runtime", lambda root, art, tag, env: contextlib.nullcontext(env))
    monkeypatch.setattr(gate, "baseline", baseline)
    assert gate.main() == 0
    assert len(provisioned) == 1
    assert inspected == provisioned


def test_pre_still_compares_receipt_base_argument(base_fixture, monkeypatch, capsys):
    repo, art = base_fixture
    gate = importlib.import_module("unit_gate")
    main_sha = git(repo, "rev-parse", "main")
    base_sha = git(repo, "rev-parse", "pre-main")
    candidate_sha = git(repo, "rev-parse", "builder")
    (art / "feature-candidate.json").write_text(
        json.dumps({"worktree": str(repo), "main_sha": base_sha, "candidate_sha": candidate_sha})
    )
    monkeypatch.setenv("PILOT_BASE_REF", "main")
    monkeypatch.setattr(sys, "argv", ["unit_gate", "pre", "feature", main_sha, candidate_sha])
    with pytest.raises(SystemExit) as refused:
        gate.main()
    assert refused.value.code == 1
    assert "Candidate arguments differ from receipt" in capsys.readouterr().err
