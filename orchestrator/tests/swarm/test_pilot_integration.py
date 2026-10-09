import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.fixture
def integration_fixture(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "web").mkdir()
    (repo / "web/package-lock.json").write_text("{}")
    (repo / "file").write_text("base")
    git(repo, "add", "file", "web/package-lock.json")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "builder")
    (repo / "file").write_text("candidate")
    git(repo, "commit", "-am", "builder")
    git(repo, "checkout", "main")
    (repo / "user-untracked").write_text("preserve")
    art = tmp_path / "art"
    art.mkdir()
    (art / "feature-acceptance.md").write_text(
        '```json\n{"feature":"feature","product_ids":[]}\n```\n'
    )
    (art / "feature-ids.txt").write_text("A1\n")
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        from run_state import processes

        identity = processes()[os.getpid()]
    finally:
        sys.path.pop(0)
    (art / "run.json").write_text(
        json.dumps(
            {
                "approvals": {"U6": True},
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
    env = dict(
        os.environ,
        REPO=str(repo),
        ART=str(art),
        PILOT_RUN=str(art / "run.json"),
        PILOT_WORKTREE_ROOT=str(tmp_path / "worktrees"),
    )
    command = ["/bin/zsh", str(ROOT / "scripts/swarm/prepare_candidate.sh")]
    return repo, art, env, command


def test_prepare_preserves_attempts_and_check_is_readonly(
    integration_fixture, tmp_path
):
    repo, art, env, command = integration_fixture
    before = git(repo, "show-ref")
    result = subprocess.run(
        [*command, "--check", "feature", "builder"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert git(repo, "show-ref") == before
    assert not (tmp_path / "worktrees").exists()
    receipts = []
    for _ in range(2):
        result = subprocess.run(
            [*command, "feature", "builder"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        receipts.append(json.loads((art / "feature-candidate.json").read_text()))
    result = subprocess.run(
        [*command, "--check", "feature"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert receipts[0]["attempt"] == 1
    assert receipts[1]["attempt"] == 2
    assert Path(receipts[0]["worktree"]).exists()
    assert (art / "attempts/feature/1/feature-candidate.json").exists()
    assert (repo / "user-untracked").read_text() == "preserve"
    assert git(repo, "rev-parse", "main") == receipts[0]["main_sha"]


@pytest.mark.parametrize(
    "scenario",
    [
        "accepted",
        "push_failure",
        "stale",
        "pause",
        "unknown_status",
        "pushed_drift",
        "corrupt_diff",
    ],
)
def test_merge_exact_reviewed_commit(integration_fixture, tmp_path, scenario):
    from test_pilot_gate_validation import packet

    repo, art, env, command = integration_fixture
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    subprocess.run(
        [*command, "feature", "builder"], env=env, check=True, capture_output=True
    )
    receipt = json.loads((art / "feature-candidate.json").read_text())
    candidate_root = Path(receipt["worktree"])
    (candidate_root / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        from gate_evidence import acceptance, artifact_hashes, changed, fingerprint

        _, gate, review = packet()
        gate.update(
            candidate_sha=receipt["candidate_sha"],
            base_sha=receipt["main_sha"],
            worktree=receipt["worktree"],
            tree=receipt["tree"],
            fingerprint=fingerprint(candidate_root),
            diff_files=changed(repo, receipt["main_sha"], receipt["candidate_sha"]),
        )
    finally:
        sys.path.pop(0)
    review.update(
        candidate_sha=receipt["candidate_sha"],
        base_sha=receipt["main_sha"],
        unit="feature",
    )
    for suffix, value in [
        ("gate", gate),
        ("code-review", review),
        ("open-findings", []),
        ("survivors", {"survivors": []}),
    ]:
        (art / ("feature-" + suffix + ".json")).write_text(json.dumps(value))
    for suffix in ["test-map.json", "s1-tests.json", "s1-tests.sha256", "red.json"]:
        (art / ("feature-" + suffix)).write_text("fixture")
    gate["artifact_hashes"] = artifact_hashes(
        art, "feature", acceptance(art / "feature-acceptance.md")
    )
    (art / "feature-gate.json").write_text(json.dumps(gate))
    (art / "feature-diff.patch").write_text(
        subprocess.check_output(
            ["git", "diff", receipt["main_sha"], receipt["candidate_sha"]],
            cwd=repo,
            text=True,
        )
    )
    merge = ["/bin/zsh", str(ROOT / "scripts/swarm/merge_unit.sh")]
    checked = subprocess.run(
        [*merge, "--check", "feature"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    assert git(repo, "rev-parse", "main") == receipt["main_sha"]
    if scenario == "corrupt_diff":
        (art / "feature-diff.patch").write_text("wrong candidate diff")
    if scenario == "unknown_status":
        receipt["status"] = "unexpected"
        (art / "feature-candidate.json").write_text(json.dumps(receipt))
    if scenario == "stale":
        (repo / "later").write_text("main changed")
        git(repo, "add", "later")
        git(repo, "commit", "-m", "later")
    elif scenario == "pause":
        (art / "PAUSE").write_text("hold")
    elif scenario == "push_failure":
        git(repo, "remote", "set-url", "origin", str(tmp_path / "unavailable.git"))
    before = git(repo, "rev-parse", "main")
    result = subprocess.run(
        [*merge, "feature"], env=env, capture_output=True, text=True, check=False
    )
    saved = json.loads((art / "feature-candidate.json").read_text())
    if scenario in {"stale", "pause", "unknown_status", "corrupt_diff"}:
        assert result.returncode != 0
        assert git(repo, "rev-parse", "main") == before
        assert git(remote, "rev-parse", "main") == receipt["main_sha"]
        if scenario == "stale":
            assert saved["status"] == "invalidated"
        return
    if scenario == "push_failure":
        assert result.returncode != 0
        assert saved["status"] == "merged_local"
        assert git(repo, "rev-parse", "main") == receipt["candidate_sha"]
        before_retry = git(repo, "reflog", "show", "main")
        git(repo, "remote", "set-url", "origin", str(remote))
        result = subprocess.run(
            [*merge, "feature"], env=env, capture_output=True, text=True, check=False
        )
        assert git(repo, "reflog", "show", "main") == before_retry
    assert result.returncode == 0, result.stderr
    assert git(repo, "rev-parse", "main") == receipt["candidate_sha"]
    assert git(remote, "rev-parse", "main") == receipt["candidate_sha"]
    assert (repo / "user-untracked").read_text() == "preserve"
    assert (
        json.loads((art / "feature-candidate.json").read_text())["status"] == "pushed"
    )
    if scenario == "pushed_drift":
        (repo / "later").write_text("main moved after push")
        git(repo, "add", "later")
        git(repo, "commit", "-m", "later")
        repeated = subprocess.run(
            [*merge, "feature"], env=env, capture_output=True, text=True, check=False
        )
        assert repeated.returncode != 0
    else:
        repeated = subprocess.run(
            [*merge, "feature"], env=env, capture_output=True, text=True, check=False
        )
        assert repeated.returncode == 0, repeated.stderr
