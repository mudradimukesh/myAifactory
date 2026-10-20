import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from gate_evidence import acceptance, digest, output, retire_worktree
from run_state import DEFAULT_RUN, budget, check_action

DEFAULT_REPO = Path(
    "/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer"
)
DEFAULT_WORKTREES = Path(str(DEFAULT_REPO) + "-wt") / "p1-pilot-integration"


def load(path):
    return json.loads(path.read_text())


def save(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temp, path)


@contextmanager
def integration_lock(art):
    with (art / "integration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def allowed(run_path, action):
    state = load(run_path)
    check_action(state, run_path, action, require_lead=True)
    budget(state)
    return state


def resolve_base(repo: Path) -> tuple[str, str]:
    base_ref = os.environ.get("PILOT_BASE_REF", "main")
    try:
        base = output(
            ["git", "rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}"], repo
        )
    except subprocess.CalledProcessError as error:
        raise ValueError("Invalid base ref: " + base_ref) from error
    return base_ref, base


def prepare(repo, art, run_path, tag, branch, check_only=False):
    allowed(run_path, "candidate")
    spec = acceptance(art / (tag + "-acceptance.md"))
    if not (art / (tag + "-ids.txt")).read_text().strip():
        raise ValueError("Acceptance ids missing")
    builder = output(["git", "rev-parse", "--verify", branch + "^{commit}"], repo)
    base_ref, base = resolve_base(repo)
    if check_only:
        return {"check": "PASS", "builder_sha": builder, "main_sha": base}
    with integration_lock(art):
        allowed(run_path, "candidate")
        base_ref, base = resolve_base(repo)
        current = art / (tag + "-candidate.json")
        attempt = 1
        kept = None
        if current.exists():
            previous = load(current)
            attempt = previous["attempt"] + 1
            archive = art / "attempts" / previous["feature"] / str(previous["attempt"])
            archive.mkdir(parents=True, exist_ok=True)
            for path in art.glob(tag + "-*"):
                if path.is_file():
                    destination = archive / path.name
                    if destination.exists() and destination.read_bytes() != path.read_bytes():
                        raise ValueError("Attempt archive already contains different evidence")
                    shutil.copy2(path, destination)
            kept = retire_worktree(repo, previous["worktree"], run_path)
        worktree_root = Path(os.environ.get("PILOT_WORKTREE_ROOT", str(DEFAULT_WORKTREES)))
        skipped = []
        while True:
            root = worktree_root / tag / str(attempt)
            integration_branch = f"int/{tag}/{attempt}"
            if not (
                root.exists()
                or subprocess.run(
                    ["git", "show-ref", "--verify", "--quiet", "refs/heads/" + integration_branch],
                    cwd=repo,
                ).returncode
                == 0
            ):
                break
            skipped.append(attempt)
            attempt += 1
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", integration_branch, str(root), base],
            cwd=repo,
            check=True,
        )
        subprocess.run(["git", "merge", "--no-ff", "--no-edit", builder], cwd=root, check=True)
        candidate = output(["git", "rev-parse", "HEAD"], root)
        receipt = {
            "feature": spec["feature"],
            "tag": tag,
            "attempt": attempt,
            "main_sha": base,
            "base_ref": base_ref,
            "builder_sha": builder,
            "candidate_sha": candidate,
            "worktree": str(root),
            "tree": output(["git", "rev-parse", "HEAD^{tree}"], root),
            "expected_review_modes": ["code", "product"] if spec.get("product_ids") else ["code"],
            "integration_branch": integration_branch,
            "status": "prepared",
        }
        if skipped:
            receipt["skipped_attempts"] = skipped
        if current.exists() and kept:
            receipt["previous_worktree_kept"] = {"path": previous["worktree"], "reason": kept}
        save(current, receipt)
        return receipt


def review_paths(art, tag, receipt):
    return [art / (tag + "-" + mode + "-review.json") for mode in receipt["expected_review_modes"]]


def validate_packet(repo, art, tag, receipt):
    root = Path(receipt["worktree"])
    command = [
        str(root / ".venv/bin/python"),
        str(Path(__file__).with_name("review_check.py")),
        "--gate",
        str(art / (tag + "-gate.json")),
        "--candidate",
        str(art / (tag + "-candidate.json")),
        "--ids",
        str(art / (tag + "-ids.txt")),
        "--open-findings",
        str(art / (tag + "-open-findings.json")),
    ]
    for path in review_paths(art, tag, receipt):
        command += ["--review", str(path)]
    subprocess.run(command, cwd=root, check=True)
    return {
        str(path): digest(path)
        for path in [art / (tag + "-gate.json"), *review_paths(art, tag, receipt)]
    }


def push(repo, remote, art, tag, receipt):
    commit = receipt["candidate_sha"]
    if output(["git", "rev-parse", "main"], repo) != commit:
        raise ValueError("Local main differs from accepted local merge")
    result = subprocess.run(
        ["git", "push", remote, commit + ":refs/heads/main"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    receipt["push"] = {
        "remote": remote,
        "commit": commit,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    receipt["status"] = "pushed" if result.returncode == 0 else "merged_local"
    save(art / (tag + "-candidate.json"), receipt)
    if result.returncode:
        raise ValueError("Local integration succeeded; authorized push failed: " + result.stderr)
    cleanup(repo, art, tag, receipt)
    return commit


def cleanup(repo, art, tag, receipt):
    # A kept or failed removal never changes the push outcome; merge retries it later.
    kept = retire_worktree(repo, receipt["worktree"])
    receipt["worktree_cleanup"] = "done" if kept is None else "pending: " + kept
    save(art / (tag + "-candidate.json"), receipt)


def merge(repo, remote, art, run_path, tag, check_only=False):
    state = allowed(run_path, "merge")
    if not state.get("approvals", {}).get("U6"):
        raise ValueError("U6 push approval missing")
    receipt_path = art / (tag + "-candidate.json")
    receipt = load(receipt_path)
    for path in [
        art / (tag + "-gate.json"),
        art / (tag + "-ids.txt"),
        art / (tag + "-open-findings.json"),
        *review_paths(art, tag, receipt),
    ]:
        if not path.is_file():
            raise ValueError("Required evidence missing: " + str(path))
    if output(["git", "branch", "--show-current"], repo) != "main":
        raise ValueError("Main checkout is on another branch")
    if check_only:
        return "PASS"
    with integration_lock(art):
        allowed(run_path, "merge")
        receipt = load(receipt_path)
        if receipt["status"] == "merged_local":
            for name, expected in receipt["accepted_evidence"].items():
                if digest(Path(name)) != expected:
                    raise ValueError("Accepted evidence changed after local merge")
            return push(repo, remote, art, tag, receipt)
        if receipt["status"] == "pushed":
            if output(["git", "rev-parse", "main"], repo) != receipt["candidate_sha"]:
                raise ValueError("Main moved after the recorded push")
            for name, expected in receipt["accepted_evidence"].items():
                if digest(Path(name)) != expected:
                    raise ValueError("Accepted evidence changed after push")
            remote_ref = output(["git", "ls-remote", remote, "refs/heads/main"], repo)
            if not remote_ref or remote_ref.split()[0] != receipt["candidate_sha"]:
                raise ValueError("Remote main differs from the recorded push")
            if receipt.get("worktree_cleanup") != "done":
                cleanup(repo, art, tag, receipt)
            return receipt["candidate_sha"]
        if receipt["status"] != "prepared":
            raise ValueError("Candidate status does not permit integration")
        if output(["git", "rev-parse", "main"], repo) != receipt["main_sha"]:
            receipt.update(
                status="invalidated", invalidation="main moved; rebuild and rerun gate and review"
            )
            save(receipt_path, receipt)
            raise ValueError(receipt["invalidation"])
        receipt["accepted_evidence"] = validate_packet(repo, art, tag, receipt)
        allowed(run_path, "merge")
        if (
            output(["git", "branch", "--show-current"], repo) != "main"
            or output(["git", "rev-parse", "main"], repo) != receipt["main_sha"]
        ):
            raise ValueError("Main changed immediately before integration")
        subprocess.run(
            ["git", "merge", "--ff-only", receipt["candidate_sha"]], cwd=repo, check=True
        )
        receipt["status"] = "merged_local"
        save(receipt_path, receipt)
        refs = output(
            ["git", "reflog", "show", "-2", "--format=%H", "refs/heads/main"], repo
        ).splitlines()
        if (
            output(["git", "rev-parse", "main"], repo) != receipt["candidate_sha"]
            or len(refs) < 2
            or refs[1] != receipt["main_sha"]
        ):
            receipt.update(
                status="invalidated_after_local_merge", invalidation="Concurrent main change"
            )
            save(receipt_path, receipt)
            raise ValueError("Concurrent main change invalidates acceptance; do not push")
        allowed(run_path, "merge")
        return push(repo, remote, art, tag, receipt)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in {"prepare", "merge"}:
        raise ValueError("Expected prepare or merge")
    mode, *args = sys.argv[1:]
    check_only = "--check" in args
    args = [a for a in args if a != "--check"]
    counts = {1, 2} if mode == "prepare" and check_only else {2 if mode == "prepare" else 1}
    if len(args) not in counts or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", args[0]):
        raise ValueError("Invalid tag or arguments")
    repo = Path(os.environ.get("REPO", str(DEFAULT_REPO)))
    art = Path(os.environ.get("ART", str(DEFAULT_RUN.parent)))
    run_path = Path(os.environ.get("PILOT_RUN", str(DEFAULT_RUN)))
    if mode == "prepare":
        branch = (
            args[1] if len(args) == 2 else load(art / (args[0] + "-candidate.json"))["builder_sha"]
        )
        result = prepare(repo, art, run_path, args[0], branch, check_only)
        print(json.dumps(result))
    else:
        print(merge(repo, os.environ.get("REMOTE", "interior"), art, run_path, args[0], check_only))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print("REFUSED:", error, file=sys.stderr)
        raise SystemExit(1) from error
