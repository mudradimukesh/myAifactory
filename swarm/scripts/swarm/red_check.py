import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from acceptance_check import (
    assertion_red,
    browser_rows,
    light_mapping,
    mapped_nodes,
    python_rows,
    retroactive_red,
)
from gate_evidence import digest, output, provision, retire_worktree, runtime


def run(root, command, log, env):
    with log.open("w") as stream:
        return subprocess.run(
            command, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT
        ).returncode


def prove(repo, art, tag, predecessor, tests_commit, retroactive=None):
    mapping = light_mapping(json.loads((art / (tag + "-test-map.json")).read_text()))
    root = art / "red" / (tag + "-" + str(time.time_ns()))
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(root), predecessor], cwd=repo, check=True
    )
    try:
        return collect(repo, art, tag, root, mapping, predecessor, tests_commit, retroactive)
    finally:
        # Never mask the red result; a kept worktree is reported, not raised.
        kept = retire_worktree(repo, root)
        if kept:
            print("red worktree kept at " + str(root) + ": " + kept, file=sys.stderr)


def collect(repo, art, tag, root, mapping, predecessor, tests_commit, retroactive):
    paths = output(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", tests_commit], repo
    ).splitlines()
    allowed = ("tests/", "web/tests/", "web/e2e/")
    if not paths or any(not p.startswith(allowed) for p in paths):
        raise ValueError("Tests commit contains production or configuration edits")
    subprocess.run(["git", "cherry-pick", "--no-commit", tests_commit], cwd=root, check=True)
    python = root / ".venv/bin/python"
    env = provision(root, art / (tag + "-red-install.log"))
    reports = {}
    with runtime(root, art, tag + "-red", env) as env:
        if any(m["runner"] == "pytest" for m in mapping):
            junit = art / (tag + "-red.xml")
            nodes = sorted(
                {node for m in mapping if m["runner"] == "pytest" for node in mapped_nodes(m)}
            )
            run(
                root,
                [
                    str(python),
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:randomly",
                    *nodes,
                    "--junitxml=" + str(junit),
                ],
                art / (tag + "-red-python.log"),
                env,
            )
            reports["pytest"] = python_rows(junit)
        if any(m["runner"] == "playwright" for m in mapping):
            report = art / (tag + "-red-browser.json")
            env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = str(report)
            specs = sorted(
                {
                    node.split("::")[0]
                    for m in mapping
                    if m["runner"] == "playwright"
                    for node in mapped_nodes(m)
                }
            )
            run(
                root,
                [
                    "npm",
                    "--prefix",
                    "web",
                    "run",
                    "e2e",
                    "--",
                    *specs,
                    "--project=light-keyboard",
                    "--project=light-pointer",
                    "--reporter=json",
                    "--retries=0",
                ],
                art / (tag + "-red-browser.log"),
                env,
            )
            reports["playwright"] = browser_rows(json.loads(report.read_text()))
    red = retroactive_red if retroactive else assertion_red
    findings = []
    for item in mapping:
        rows = [r for r in reports[item["runner"]] if r["node"] in mapped_nodes(item)]
        projects = item.get("projects", ["pytest"])
        if len(rows) != len(projects) * len(mapped_nodes(item)) or {
            (r["node"], r["project"]) for r in rows
        } != {(n, p) for n in mapped_nodes(item) for p in projects}:
            raise ValueError("Red collection differs for " + item["id"])
        preservation = item.get("preservation", False)
        valid = all(
            r["status"] == "passed" if preservation else red(r, item["expected_failure"])
            for r in rows
        )
        if not valid:
            raise ValueError("Not assertion-level red or preservation failed: " + item["id"])
        findings.append({"id": item["id"], "preservation": preservation, "rows": rows})
    receipt = {
        "predecessor": predecessor,
        "tests_commit": tests_commit,
        "test_map_sha256": digest(art / (tag + "-test-map.json")),
        "worktree": str(root),
        "passed": True,
        "retroactive_ruling": retroactive,
        "findings": findings,
    }
    (art / (tag + "-red.json")).write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("predecessor")
    parser.add_argument("tests_commit")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--retroactive", metavar="RULING")
    parser.add_argument("--art", type=Path, default=Path("/tmp/factory-fp-0925/swarm/interior-p1"))
    args = parser.parse_args()
    try:
        prove(args.repo, args.art, args.tag, args.predecessor, args.tests_commit, args.retroactive)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, "FAIL: " + str(error) + "\n")
    print("PASS")


if __name__ == "__main__":
    main()
