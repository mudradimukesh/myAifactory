import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from acceptance_check import browser_rows, check, mapped_nodes
from acceptance_check import python_rows as read_python_rows
from diff_lint import check as lint_diff
from gate_evidence import (
    NODE,
    acceptance,
    artifact_hashes,
    changed,
    digest,
    fingerprint,
    output,
    production,
    provision,
    retire_worktree,
    runtime,
)
from integration import resolve_base

python_rows = read_python_rows


def run_all(checks, workers=1):
    def run(item):
        identifier, action = item
        started = time.monotonic()
        try:
            result = action()
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            result = {
                "exit_code": 1,
                "measured": 1,
                "threshold": 0,
                "command": [],
                "error": str(error),
            }
        return {
            "id": identifier,
            "disposition": "retained",
            "comparison": "max",
            **result,
            "duration_seconds": time.monotonic() - started,
        }

    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(run, checks))


FULL_ONLY = {"G2", "G3", "G9", "G10", "G11", "G12", "G13", "G16", "G17", "G18"}
# The Phase 1 POC gates skip suites another phase owns (user ruling 2026-10-20).
# Phase 2's environment tests read Phase 2 receipts that Phase 1 cannot provide.
PYTEST_IGNORE = ["--ignore=tests/phase2"]
WEB_LINTED = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


class Gate:
    art: Path
    tag: str
    fast = False

    def __init__(self, root, art, tag, base, candidate, fast=False):
        self.root, self.art, self.tag, self.base, self.candidate = root, art, tag, base, candidate
        self.fast = fast
        self.spec = acceptance(art / (tag + "-acceptance.md"))
        self.files = changed(root, base, candidate)
        self.py = production(self.files, "python")
        self.ts = production(self.files, "web")
        self.python = str(root / ".venv/bin/python")
        self.env = {
            **os.environ,
            "PATH": str(NODE.parent) + os.pathsep + os.environ["PATH"],
            "CI": "1",
        }
        self.mapping = json.loads((art / (tag + "-test-map.json")).read_text())
        self.reports = {}
        self.survivors = []

    def input(self, suffix: str) -> Path:
        return self.art / (self.tag + "-" + suffix)

    def file(self, suffix: str) -> Path:
        return self.input(("fast-" if self.fast else "") + suffix)

    def commands(self, gate, commands, cwd="."):
        report = self.file("G10-tests.xml")
        if gate == "G10":
            commands = [[*command, "--junitxml=" + str(report)] for command in commands]
        codes = []
        with self.file(gate + ".log").open("w") as log:
            for command in commands:
                completed = subprocess.run(
                    command, cwd=self.root / cwd, env=self.env, stdout=log, stderr=subprocess.STDOUT
                )
                codes.append(completed.returncode)
        result = {
            "command": commands,
            "exit_code": max([abs(c) for c in codes], default=0),
            "measured": sum(c != 0 for c in codes),
            "threshold": 0,
            "evidence": str(self.file(gate + ".log")),
        }
        if gate == "G10":
            if not report.exists():
                raise ValueError("G10 test report missing")
            tests = python_rows(report)
            if not tests or any(row["status"] == "error" for row in tests):
                raise ValueError("G10 runner report is empty or has infrastructure errors")
            result["failed_test_ids"] = sorted(
                {row["node"] for row in tests if row["status"] == "failed"}
            )
            result["paths"] = ["src/", "tests/", "scripts/swarm/"]
        return result

    def frozen(self):
        record = json.loads(self.input("s1-tests.json").read_text())
        hashes = self.input("s1-tests.sha256").read_text().splitlines()
        if not hashes:
            raise ValueError("Frozen hashes missing")
        for row in hashes:
            expected, file = row.split(maxsplit=1)
            path = self.root / file.lstrip(" *")
            if not path.resolve().is_relative_to(self.root.resolve()) or digest(path) != expected:
                raise ValueError("Frozen test hash differs")
        scope = ["--", "tests", "web/tests", "web/e2e"]
        since_tests, since_base = (
            set(
                output(
                    ["git", "diff", "--name-only", start, self.candidate, *scope], self.root
                ).split()
            )
            for start in (record["tests_commit"], self.base)
        )
        # Test changes merged in from the gate base are not candidate edits.
        modified = "\n".join(sorted(since_tests & since_base))
        if modified:
            raise ValueError("Frozen tests changed after tests commit: " + modified)
        return {
            "command": ["sha256", str(self.input("s1-tests.sha256"))],
            "exit_code": 0,
            "measured": 0,
            "threshold": 0,
        }

    def green(self):
        red = json.loads(self.input("red.json").read_text())
        frozen = json.loads(self.input("s1-tests.json").read_text())
        if (
            not red["passed"]
            or red["tests_commit"] != frozen["tests_commit"]
            or red["test_map_sha256"] != digest(self.input("test-map.json"))
        ):
            raise ValueError("Red proof differs from frozen tests")
        commands = []
        nodes = sorted(
            {node for m in self.mapping if m["runner"] == "pytest" for node in mapped_nodes(m)}
        )
        if nodes:
            commands.append(
                [
                    self.python,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:randomly",
                    *nodes,
                    "--junitxml=" + str(self.file("green.xml")),
                ]
            )
        specs = sorted(
            {
                node.split("::")[0]
                for m in self.mapping
                if m["runner"] == "playwright"
                for node in mapped_nodes(m)
            }
        )
        if specs:
            self.env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = str(self.file("green-browser.json"))
            commands.append(
                [
                    "npm",
                    "--prefix",
                    "web",
                    "run",
                    "e2e",
                    "--",
                    *specs,
                    "--retries=0",
                    "--reporter=json",
                ]
            )
        result = self.commands("G2", commands)
        if nodes:
            self.reports["pytest"] = python_rows(self.file("green.xml"))
        if specs:
            self.reports["playwright"] = browser_rows(
                json.loads(self.file("green-browser.json").read_text())
            )
        return result

    def related(self):
        present = [f for f in self.files if (self.root / f).exists()]
        stems = [Path(f).stem for f in present if f.endswith(".py") and not f.startswith("tests/")]
        importing = [
            str(path.relative_to(self.root))
            for path in sorted((self.root / "tests").rglob("test_*.py"))
            if any(
                "import" in line and any(re.search(rf"\b{re.escape(s)}\b", line) for s in stems)
                for line in path.read_text().splitlines()
            )
        ]
        nodes = sorted(
            {
                *[f for f in present if re.fullmatch(r"tests/(.+/)?test_[^/]+\.py", f)],
                *importing,
                *[n for m in self.mapping if m["runner"] == "pytest" for n in mapped_nodes(m)],
            }
        )
        web = [f.removeprefix("web/") for f in present if f.startswith("web/src/")]
        commands = []
        if nodes:
            commands.append([self.python, "-m", "pytest", "-q", "-p", "no:randomly", *nodes])
        if web:
            commands.append(
                [
                    "npm",
                    "--prefix",
                    "web",
                    "run",
                    "test",
                    "--",
                    "related",
                    "--run",
                    "--passWithNoTests",
                    *web,
                ]
            )
        return self.commands("related", commands)

    def mapped(self):
        check(self.mapping, self.input("ids.txt").read_text().splitlines(), self.reports)
        return {"command": ["acceptance_check.py"], "exit_code": 0, "measured": 0, "threshold": 0}

    def diff(self, identifier):
        errors = lint_diff(self.root, self.base, self.candidate, 1000)
        selected = [
            e for e in errors if (e["reason"] == "file exceeds line limit") == (identifier == "G8")
        ]
        return {
            "command": ["diff_lint.py", self.base, self.candidate],
            "exit_code": int(bool(selected)),
            "measured": len(selected),
            "threshold": 0,
            "errors": selected,
        }

    def unit(self, identifier, repeat=True):
        commands = []
        include = [
            option
            for file in self.ts
            for option in ["--coverage.include", file.removeprefix("web/")]
        ]
        for seed in range(1, 6) if repeat else [1]:
            commands.append(
                [
                    "npm",
                    "--prefix",
                    "web",
                    "test",
                    "--",
                    "--run",
                    "--retry=0",
                    "--sequence.shuffle.files",
                    "--sequence.shuffle.tests",
                    "--sequence.seed=" + str(seed),
                    "--coverage.enabled",
                    *include,
                ]
            )
        return self.commands(identifier, commands)

    def coverage(self):
        commands = []
        if self.py:
            commands += [
                [
                    self.python,
                    "-m",
                    "pytest",
                    "-q",
                    *PYTEST_IGNORE,
                    *["--cov=" + path for path in sorted({str(Path(f).parent) for f in self.py})],
                    "--cov-branch",
                    "--cov-report=xml:" + str(self.file("cov.xml")),
                ],
                [
                    str(self.root / ".venv/bin/diff-cover"),
                    str(self.file("cov.xml")),
                    "--compare-branch=" + self.base,
                    "--fail-under=90",
                ],
            ]
        if self.ts:
            unit = self.unit("G12-unit", False)
            if unit["exit_code"]:
                return unit
            report = self.root / "web/coverage/lcov.info"
            text = report.read_text()
            covered = [
                line.removeprefix("SF:") for line in text.splitlines() if line.startswith("SF:")
            ]
            for file in self.ts:
                if not any(
                    name.endswith(file) or name == file.removeprefix("web/") for name in covered
                ):
                    raise ValueError("Changed production file absent from coverage: " + file)
            normalized = self.file("coverage.lcov")
            normalized.write_text(
                "\n".join(
                    "SF:" + str((self.root / "web" / line[3:]).resolve().relative_to(self.root))
                    if line.startswith("SF:")
                    else line
                    for line in text.splitlines()
                )
                + "\n"
            )
            commands.append(
                [
                    str(self.root / ".venv/bin/diff-cover"),
                    str(normalized),
                    "--compare-branch=" + self.base,
                    "--fail-under=90",
                ]
            )
        return self.commands("G12", commands)

    def mutation(self, identifier, language, files):
        if not files:
            return {
                "disposition": "not_applicable",
                "command": [],
                "exit_code": 0,
                "measured": 0,
                "threshold": 0,
                "evidence": "Recorded diff has no changed production " + language,
            }
        packet = self.file(identifier + "-mutation.json")
        result = self.commands(
            identifier,
            [
                [
                    self.python,
                    str(Path(__file__).with_name("mutation_gate.py")),
                    "--language",
                    language,
                    "--changed",
                    *files,
                    "--base",
                    self.base,
                    "--candidate",
                    self.candidate,
                    "--threshold",
                    "70",
                    "--out",
                    str(packet),
                    "--repo",
                    str(self.root),
                ]
            ],
        )
        if packet.exists():
            content = json.loads(packet.read_text())
            self.survivors.extend(content["survivors"])
            result.update(measured=content["score"], threshold=70, comparison="min")
        return result

    def properties(self):
        paths = self.spec.get("property_test_paths", [])
        if not paths:
            return {
                "disposition": "not_applicable",
                "command": [],
                "exit_code": 0,
                "measured": 0,
                "threshold": 0,
                "evidence": "Acceptance specifies no property-tested invariants",
            }
        if any("@given" not in (self.root / p).read_text() for p in paths):
            raise ValueError("Required property tests missing")
        return self.commands("G9", [[self.python, "-m", "pytest", "-q", *paths]])

    @property
    def repeats(self):
        return 1 if self.fast else 5

    def journey(self):
        report = self.file("repeat-browser.json")
        self.env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = str(report)
        result = self.commands(
            "G17",
            [
                [
                    "npm",
                    "--prefix",
                    "web",
                    "run",
                    "e2e",
                    "--",
                    *self.spec["journey_specs"],
                    "--retries=0",
                    "--repeat-each=" + str(self.repeats),
                    "--workers=1",
                    "--reporter=json",
                ]
            ],
        )
        tests = browser_rows(json.loads(report.read_text()))
        if not tests:
            raise ValueError("G17 runner report is empty")
        result["failed_test_ids"] = sorted(
            {row["node"] for row in tests if row["status"] not in {"passed", "skipped"}}
        )
        result["paths"] = ["web/", "src/", "scripts/swarm/", "tests/e2e/"]
        mapping = [m for m in self.mapping if m["runner"] == "playwright"]
        if result["exit_code"]:
            return result
        check(
            mapping,
            [m["id"] for m in mapping],
            {"playwright": tests},
            repeats=self.repeats,
        )
        return result

    def execute(self):
        ruff = str(self.root / ".venv/bin/ruff")
        pytests = self.spec.get("python_test_paths", [])
        changed_python = [file for file in self.files if file.endswith(".py")]
        present_python = [f for f in changed_python if (self.root / f).exists()]
        web_lint = [
            f.removeprefix("web/")
            for f in self.files
            if f.startswith("web/") and f.endswith(WEB_LINTED) and (self.root / f).exists()
        ]
        if self.fast:
            ruff_check = (
                [[ruff, "check", "--force-exclude", *present_python]] if present_python else []
            )
            eslint = (
                [["npm", "exec", "--", "eslint", "--max-warnings=0", *web_lint]] if web_lint else []
            )
        else:
            ruff_check = [[ruff, "check", "."]]
            eslint = [["npm", "--prefix", "web", "run", "lint"]]
        checks = [
            ("G1", self.frozen),
            ("G2", self.green),
            ("G3", self.mapped),
            (
                "G4",
                lambda: self.commands(
                    "G4",
                    ruff_check
                    + ([[ruff, "format", "--check", *changed_python]] if changed_python else []),
                ),
            ),
            (
                "G5",
                lambda: self.commands(
                    "G5",
                    [
                        [
                            ruff,
                            "check",
                            "--extend-select",
                            "C90,PLR0911,PLR0912,PLR0913,PLR0915",
                            "--config",
                            "lint.mccabe.max-complexity = 10",
                            *self.py,
                        ]
                    ]
                    if self.py
                    else [],
                ),
            ),
            (
                "G6",
                lambda: self.commands(
                    "G6",
                    [
                        [
                            str(self.root / ".venv/bin/mypy"),
                            "--strict",
                            "--follow-imports=silent",
                            *self.py,
                        ]
                    ]
                    if self.py
                    else [],
                ),
            ),
            ("G7", lambda: self.diff("G7")),
            ("G8", lambda: self.diff("G8")),
            ("G9", self.properties),
            (
                "G10",
                lambda: self.commands(
                    "G10", [[self.python, "-m", "pytest", "-q", "-p", "randomly", *PYTEST_IGNORE]]
                ),
            ),
            (
                "G11",
                lambda: self.commands(
                    "G11",
                    [
                        [
                            self.python,
                            "-m",
                            "pytest",
                            "-q",
                            "-p",
                            "randomly",
                            "--count=5",
                            "--repeat-scope=session",
                            *pytests,
                        ]
                    ]
                    if pytests
                    else [],
                ),
            ),
            ("G12", self.coverage),
            ("G13", lambda: self.mutation("G13", "python", self.py)),
            ("G14", lambda: self.commands("G14", eslint, "web" if self.fast else ".")),
            ("G15", lambda: self.commands("G15", [["npm", "--prefix", "web", "run", "typecheck"]])),
            ("G16", lambda: self.unit("G16")),
            ("G17", self.journey),
            ("G18", lambda: self.mutation("G18", "web", self.ts)),
        ]
        if self.fast:
            deferred = {
                "disposition": "not_applicable",
                "command": [],
                "exit_code": 0,
                "measured": 0,
                "threshold": 0,
                "evidence": "Fast gate: deferred to the full pre gate",
            }
            checks = [(i, (lambda: deferred) if i in FULL_ONLY else a) for i, a in checks]
            checks.append(("related", self.related))
        else:
            from collections.abc import Callable
            from functools import partial

            failed = False
            cheap = {i for i, _ in checks[:9]}

            def run(identifier: str, action: Callable[[], dict[str, object]]) -> dict[str, object]:
                nonlocal failed
                if failed:
                    return {
                        "command": [],
                        "exit_code": 1,
                        "measured": 1,
                        "threshold": 0,
                        "evidence": "skipped: prior failure",
                    }
                try:
                    result = action()
                except (ValueError, KeyError, OSError, subprocess.SubprocessError):
                    if identifier in cheap:
                        failed = True
                    raise
                if identifier in cheap and result.get("exit_code") != 0:
                    failed = True
                return result

            checks = [(i, partial(run, i, a)) for i, a in checks]
        rows = run_all(checks, len(checks) if self.fast else 1)
        self.file("survivors.json").write_text(
            json.dumps({"survivors": self.survivors}, indent=2) + "\n"
        )
        return rows


def format_diagnostics(text):
    paths = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("error:"):
            return []
        if line.startswith("Would reformat: "):
            paths.append(line.removeprefix("Would reformat: "))
        elif line == "unformatted: File would be reformatted":
            location = re.fullmatch(
                r"\s*--> (.+):\d+:\d+", lines[index + 1] if index + 1 < len(lines) else ""
            )
            if not location:
                return []
            paths.append(location[1])
    return paths


def known_diagnostics(current, previous, kind):
    if kind == "format":
        actual = Counter(format_diagnostics(current))
        recorded = Counter(format_diagnostics(previous))
    else:
        actual = Counter(line for line in current.splitlines() if ": error:" in line)
        recorded = Counter(line for line in previous.splitlines() if ": error:" in line)
    return bool(actual) and not (actual - recorded)


def baseline(root, art, tag, env):
    python = str(root / ".venv/bin/python")
    commands = [
        [python, "-m", "pytest", "-q", "-p", "randomly", *PYTEST_IGNORE],
        [str(root / ".venv/bin/ruff"), "check", "."],
        [str(root / ".venv/bin/ruff"), "format", "--check", "."],
        [str(root / ".venv/bin/mypy"), "src"],
        ["npm", "--prefix", "web", "run", "typecheck"],
        ["npm", "--prefix", "web", "run", "e2e", "--", "editor.phase1-flows"],
    ]
    results = []
    for index, command in enumerate(commands):
        start = time.monotonic()
        log = art / (tag + f"-baseline-{index}.log")
        with log.open("w") as stream:
            result = subprocess.run(
                command, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT
            )
        disposition = "passed" if result.returncode == 0 else "failed"
        known = art / f"stage1-baseline-{index}.log"
        if result.returncode == 1 and index in {2, 3} and known.exists():
            kind = "format" if index == 2 else "mypy"
            old = known.read_text()
            affected = sorted(
                {line.split(":", 1)[0] for line in old.splitlines() if ": error:" in line}
            )
            if kind == "format":
                affected = format_diagnostics(old)
            baseline_sha = json.loads((art / "run.json").read_text())["baseline_sha"]
            unchanged = bool(affected) and not output(
                ["git", "diff", "--name-only", baseline_sha, "HEAD", "--", *affected], root
            )
            if unchanged and known_diagnostics(log.read_text(), old, kind):
                disposition = "known_failure"
        results.append(
            {
                "command": command,
                "exit_code": result.returncode,
                "disposition": disposition,
                "known_evidence": str(known) if disposition == "known_failure" else None,
                "duration_seconds": time.monotonic() - start,
                "evidence": str(log),
            }
        )
    packet = {
        "sha": output(["git", "rev-parse", "HEAD"], root),
        "worktree": str(root),
        "fingerprint": fingerprint(root),
        "results": results,
    }
    (art / (tag + "-baseline.json")).write_text(json.dumps(packet, indent=2) + "\n")
    return any(r["disposition"] == "failed" for r in results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["baseline", "fast", "pre"])
    parser.add_argument("tag")
    parser.add_argument("base", nargs="?")
    parser.add_argument("candidate", nargs="?")
    args = parser.parse_args()
    art = Path(os.environ.get("ART", "/tmp/factory-fp-0925/swarm/interior-p1")).resolve()
    os.environ["PATH"] = str(NODE.parent) + os.pathsep + os.environ["PATH"]
    try:
        if args.mode == "baseline":
            repo = Path.cwd()
            _, sha = resolve_base(repo)
            root = art / "baseline" / (args.tag + "-" + str(time.time_ns()))
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(root), sha], cwd=repo, check=True
            )
            try:
                env = provision(root, art / (args.tag + "-baseline-install.log"))
                with runtime(root, art, args.tag + "-baseline", env) as env:
                    return int(baseline(root, art, args.tag, env))
            finally:
                kept = retire_worktree(repo, root)
                if kept:
                    print("baseline worktree kept at " + str(root) + ": " + kept)
        receipt = json.loads((art / (args.tag + "-candidate.json")).read_text())
        root = Path(receipt["worktree"])
        if receipt["candidate_sha"] != args.candidate or receipt["main_sha"] != args.base:
            raise ValueError("Candidate arguments differ from receipt")
        if output(["git", "rev-parse", "HEAD"], root) != args.candidate:
            raise ValueError("Wrong candidate checkout")
        if output(["git", "status", "--porcelain", "--untracked-files=no"], root):
            raise ValueError("Candidate has unrelated tracked changes")
        marker = root / ".venv/provision.sha256"
        current = hashlib.sha256(
            b"".join((root / f).read_bytes() for f in ("pyproject.toml", "web/package-lock.json"))
        ).hexdigest()
        if args.mode == "fast" and marker.exists() and marker.read_text() == current:
            env = {**os.environ, "CI": "1"}
        else:
            env = provision(root, art / (args.tag + "-install.log"))
            marker.write_text(current)
        gate = Gate(root, art, args.tag, args.base, args.candidate, args.mode == "fast")
        isolation = args.tag + ("-fast" if args.mode == "fast" else "")
        with runtime(root, art, isolation, env) as isolated:
            gate.env.update(isolated)
            rows = gate.execute()
        try:
            hashes = artifact_hashes(art, args.tag, gate.spec, args.mode == "fast")
        except (ValueError, OSError) as error:
            hashes = {}
            rows.append(
                {
                    "id": "packet",
                    "disposition": "failed",
                    "command": ["artifact_hashes"],
                    "exit_code": 1,
                    "measured": 1,
                    "threshold": 0,
                    "error": str(error),
                }
            )
        packet = {
            "candidate_sha": args.candidate,
            "base_sha": args.base,
            "worktree": str(root),
            "tree": output(["git", "rev-parse", "HEAD^{tree}"], root),
            "fingerprint": fingerprint(root),
            "diff_files": gate.files,
            "gates": rows,
            "artifact_hashes": hashes,
        }
        gate.file("gate.json").write_text(json.dumps(packet, indent=2) + "\n")
        failures = [r for r in rows if r["exit_code"] != 0]
        for line in json.dumps(failures, indent=2).splitlines()[:100]:
            if failures:
                print(line)
        return int(bool(failures))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, "FAIL: " + str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
