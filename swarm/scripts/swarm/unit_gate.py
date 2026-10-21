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

from acceptance_check import browser_rows, check, light_mapping, mapped_nodes
from acceptance_check import python_rows as read_python_rows
from diff_lint import check as lint_diff
from gate_evidence import (
    FINAL_ONLY,
    NODE,
    POC_PROJECTS,
    POC_TEST_POLICY,
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
PYTEST_IGNORE = ["--ignore=tests/phase2", "--ignore=tests/swarm"]
WEB_LINTED = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


class Gate:
    # Provisioned environment for per-gate runtimes; None runs gates without a runtime.
    runtime_env = None

    art: Path
    tag: str
    fast = False
    final = False
    suite = "poc"

    def __init__(self, root, art, tag, base, candidate, fast=False):
        self.root, self.art, self.tag, self.base, self.candidate = root, art, tag, base, candidate
        self.fast = fast
        self.qualification_base = base
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
        self.mapping = light_mapping(json.loads((art / (tag + "-test-map.json")).read_text()))
        nodes = [n for m in self.mapping if m["runner"] == "pytest" for n in mapped_nodes(m)]
        if nodes and all(n.startswith("tests/swarm/") for n in nodes):
            if any(m["runner"] != "pytest" for m in self.mapping):
                raise ValueError("Separate swarm acceptance from POC acceptance")
            self.suite = "swarm"
        elif any(n.startswith("tests/swarm/") for n in nodes):
            raise ValueError("Separate swarm acceptance from POC acceptance")
        self.py = [f for f in self.py if f.startswith("scripts/swarm/") == (self.suite == "swarm")]
        if self.suite == "swarm":
            self.ts = []
        self.reports = {}
        self.survivors = []

    def isolated(self, identifier, action):
        def run():
            name = (
                self.tag
                + ("-fast" if self.fast else "-final" if self.final else "")
                + "-"
                + identifier
            )
            with runtime(self.root, self.art, name, self.runtime_env) as configured:
                shared = self.env
                self.env = {**shared, **configured}
                try:
                    return action()
                finally:
                    self.env = shared

        return run

    def input(self, suffix: str) -> Path:
        return self.art / (self.tag + "-" + suffix)

    def file(self, suffix: str) -> Path:
        prefix = "fast-" if self.fast else "final-" if self.final else ""
        return self.input(prefix + suffix)

    def commands(self, gate, commands, cwd="."):
        report = self.file("G10-tests.xml")
        if gate == "G10":
            commands = [[*command, "--junitxml=" + str(report)] for command in commands]
        codes = []
        with self.file(gate + ".log").open("w") as log:
            for index, command in enumerate(commands):

                def launch(command=command):
                    return subprocess.run(
                        command,
                        cwd=self.root / cwd,
                        env=self.env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )

                completed = (
                    self.isolated(f"G2-{index}", launch)()
                    if (gate == "G2" and self.runtime_env is not None)
                    else launch()
                )
                codes.append(completed.returncode)
                if completed.returncode:
                    break
        result = {
            "command": commands,
            "exit_code": max([abs(c) for c in codes], default=0),
            "measured": sum(c != 0 for c in codes),
            "threshold": 0,
            "evidence": str(self.file(gate + ".log")),
        }
        if gate == "G10" and commands:
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
        nodes = self.python_nodes()
        if self.final or self.suite == "swarm":
            nodes = ["tests/swarm"] if self.suite == "swarm" else ["tests", *PYTEST_IGNORE]
        coverage = []
        if self.final and self.py:
            coverage = [
                *["--cov=" + path for path in sorted({str(Path(f).parent) for f in self.py})],
                "--cov-branch",
                "--cov-report=xml:" + str(self.file("cov.xml")),
            ]
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
                    *coverage,
                    "--junitxml=" + str(self.file("green.xml")),
                ]
            )
        specs = sorted(
            {
                *self.spec.get("journey_specs", []),
                *[
                    node.split("::")[0]
                    for m in self.mapping
                    if m["runner"] == "playwright"
                    for node in mapped_nodes(m)
                ],
            }
        )
        if self.final and self.suite == "poc":
            specs = [".*\\.phase1-flows\\.spec\\.ts"]
        if self.suite == "swarm":
            specs = []
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
                    *["--project=" + project for project in POC_PROJECTS],
                    "--repeat-each=1",
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

    def python_nodes(self):
        present = [f for f in self.files if (self.root / f).exists()]
        stems = [Path(f).stem for f in present if f.endswith(".py") and not f.startswith("tests/")]
        importing = [
            str(path.relative_to(self.root))
            for path in sorted((self.root / "tests").rglob("test_*.py"))
            if ("swarm" in path.relative_to(self.root).parts) == (self.suite == "swarm")
            and "phase2" not in path.relative_to(self.root).parts
            and any(
                "import" in line and any(re.search(rf"\b{re.escape(s)}\b", line) for s in stems)
                for line in path.read_text().splitlines()
            )
        ]
        nodes = sorted(
            {
                *[
                    f
                    for f in present
                    if re.fullmatch(r"tests/(.+/)?test_[^/]+\.py", f)
                    and f.startswith("tests/swarm/") == (self.suite == "swarm")
                    and not f.startswith("tests/phase2/")
                ],
                *importing,
                *self.spec.get("python_test_paths", []),
                *self.spec.get("property_test_paths", []),
                *[n for m in self.mapping if m["runner"] == "pytest" for n in mapped_nodes(m)],
            }
        )
        for node in nodes:
            if node.startswith("tests/phase2/") or (
                node.startswith("tests/swarm/") != (self.suite == "swarm")
            ):
                raise ValueError("Test path belongs to a different suite: " + node)
        files = {n for n in nodes if "::" not in n}
        return [n for n in nodes if "::" not in n or n.split("::")[0] not in files]

    def related(self):
        present = [f for f in self.files if (self.root / f).exists()]
        nodes = self.python_nodes()
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

    def unit(self, identifier):
        if self.suite == "swarm":
            return self.commands(identifier, [])
        coverage = []
        if self.final and self.ts:
            coverage = [
                "--coverage.enabled",
                *[
                    option
                    for file in self.ts
                    for option in ["--coverage.include", file.removeprefix("web/")]
                ],
            ]
        return self.commands(
            identifier,
            [
                [
                    "npm",
                    "--prefix",
                    "web",
                    "test",
                    "--",
                    "--run",
                    "--retry=0",
                    *coverage,
                ]
            ],
        )

    def coverage(self):
        commands = []
        if self.py:
            commands += [
                [
                    str(self.root / ".venv/bin/diff-cover"),
                    str(self.file("cov.xml")),
                    "--compare-branch=" + self.qualification_base,
                    "--fail-under=90",
                ],
            ]
        if self.ts:
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
                    "--compare-branch=" + self.qualification_base,
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
                    self.qualification_base,
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
        return {**self.commands("G9", []), "evidence": "Property tests included in G2"}

    @property
    def repeats(self):
        return 1

    def regression(self):
        tests = self.reports.get("pytest", [])
        if not tests or any(row["status"] == "error" for row in tests):
            raise ValueError("G10 runner report is empty or has infrastructure errors")
        failed = sorted({row["node"] for row in tests if row["status"] != "passed"})
        return {
            "command": [],
            "exit_code": int(bool(failed)),
            "measured": len(failed),
            "threshold": 0,
            "evidence": str(self.file("green.xml")),
            "failed_test_ids": failed,
            "paths": ["scripts/swarm/", "tests/swarm/"]
            if self.suite == "swarm"
            else ["src/", "tests/"],
        }

    def journey(self):
        mapping = [m for m in self.mapping if m["runner"] == "playwright"]
        check(mapping, [m["id"] for m in mapping], self.reports)
        tests = self.reports.get("playwright", [])
        if (mapping or self.spec.get("journey_specs")) and not tests:
            raise ValueError("G17 runner report is empty")
        commands = []
        if self.final and self.suite == "poc":
            self.env["PILOT_BENCHMARKS"] = "1"
            self.env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = str(self.file("benchmark-browser.json"))
            commands = [
                [
                    "npm",
                    "--prefix",
                    "web",
                    "run",
                    "e2e",
                    "--",
                    "--project=bench",
                    "--repeat-each=1",
                    "--retries=0",
                    "--workers=1",
                    "--reporter=json",
                ]
            ]
        result = self.commands("G17", commands)
        result["functional_evidence"] = str(self.file("green-browser.json"))
        if commands:
            tests = browser_rows(json.loads(self.file("benchmark-browser.json").read_text()))
            if not tests:
                raise ValueError("G17 benchmark report is empty")
        result["failed_test_ids"] = sorted({r["node"] for r in tests if r["status"] != "passed"})
        result["paths"] = ["web/", "src/", "scripts/swarm/", "tests/e2e/"]
        if result["failed_test_ids"]:
            result.update(exit_code=1, measured=len(result["failed_test_ids"]))
        return result

    def deferred(self, identifier):
        return {
            "disposition": "deferred",
            "command": [],
            "exit_code": 0,
            "measured": 0,
            "threshold": 0,
            "approval": POC_TEST_POLICY,
            "evidence": "Flake repetitions postponed"
            if identifier == "G11"
            else "Required at final qualification; not run during development",
        }

    def execute(self):
        ruff = str(self.root / ".venv/bin/ruff")
        changed_python = [
            file
            for file in self.files
            if file.endswith(".py")
            and file.startswith(("scripts/swarm/", "tests/swarm/")) == (self.suite == "swarm")
            and not file.startswith("tests/phase2/")
        ]
        present_python = [f for f in changed_python if (self.root / f).exists()]
        web_lint = [
            f.removeprefix("web/")
            for f in self.files
            if f.startswith("web/") and f.endswith(WEB_LINTED) and (self.root / f).exists()
        ]
        if not self.final:
            ruff_check = (
                [[ruff, "check", "--force-exclude", *present_python]] if present_python else []
            )
            eslint = (
                [["npm", "exec", "--", "eslint", "--max-warnings=0", *web_lint]] if web_lint else []
            )
        else:
            paths = (
                ["scripts/swarm", "tests/swarm"]
                if self.suite == "swarm"
                else [
                    ".",
                    "--extend-exclude=tests/swarm,tests/phase2,scripts/swarm",
                ]
            )
            ruff_check = [[ruff, "check", *paths]]
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
                    + ([[ruff, "format", "--check", *present_python]] if present_python else []),
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
            ("G10", self.regression),
            ("G11", lambda: self.deferred("G11")),
            ("G12", self.coverage),
            ("G13", lambda: self.mutation("G13", "python", self.py)),
            (
                "G14",
                lambda: self.commands(
                    "G14", eslint if self.suite == "poc" else [], "web" if not self.final else "."
                ),
            ),
            (
                "G15",
                lambda: self.commands(
                    "G15",
                    [["npm", "--prefix", "web", "run", "typecheck"]] if self.suite == "poc" else [],
                ),
            ),
            ("G16", lambda: self.unit("G16")),
            ("G17", self.journey),
            ("G18", lambda: self.mutation("G18", "web", self.ts)),
        ]
        if not self.final:
            checks = [
                (i, (lambda i=i: self.deferred(i)) if i in FINAL_ONLY else a) for i, a in checks
            ]
        order = [
            "G1",
            "G4",
            "G5",
            "G6",
            "G7",
            "G8",
            "G14",
            "G15",
            "G2",
            "G3",
            "G9",
            "G10",
            "G11",
            "G16",
            "G12",
            "G17",
            "G13",
            "G18",
        ]
        actions = dict(checks)
        if self.fast:
            for identifier in FULL_ONLY:
                actions[identifier] = lambda identifier=identifier: self.deferred(identifier)
            order.append("related")
            actions["related"] = self.related
        rows = []
        failed = False
        for identifier in order:
            if failed:
                rows.append(
                    {
                        "id": identifier,
                        "disposition": "blocked",
                        "command": [],
                        "exit_code": 1,
                        "measured": 1,
                        "threshold": 0,
                        "evidence": "Not run because a prerequisite failed",
                        "duration_seconds": 0,
                    }
                )
                continue
            action = actions[identifier]
            if self.runtime_env is not None and (
                identifier == "related" or (self.final and identifier in {"G13", "G17"})
            ):
                action = self.isolated(identifier, action)
            row = run_all([(identifier, action)])[0]
            rows.append(row)
            failed = row["exit_code"] != 0
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
        [python, "-m", "pytest", "-q", "-p", "no:randomly", *PYTEST_IGNORE],
        [
            str(root / ".venv/bin/ruff"),
            "check",
            ".",
            "--extend-exclude=tests/swarm,tests/phase2,scripts/swarm",
        ],
        [
            str(root / ".venv/bin/ruff"),
            "format",
            "--check",
            ".",
            "--extend-exclude=tests/swarm,tests/phase2,scripts/swarm",
        ],
        [str(root / ".venv/bin/mypy"), "src"],
        ["npm", "--prefix", "web", "run", "typecheck"],
        [
            "npm",
            "--prefix",
            "web",
            "run",
            "e2e",
            "--",
            "editor.phase1-flows",
            *["--project=" + project for project in POC_PROJECTS],
            "--repeat-each=1",
        ],
    ]
    results = []
    for index in (1, 2, 3, 4, 0, 5):
        command = commands[index]
        start = time.monotonic()
        log = art / (tag + f"-baseline-{index}.log")
        if any(row["disposition"] == "failed" for row in results):
            results.append(
                {
                    "command": command,
                    "exit_code": 1,
                    "disposition": "blocked",
                    "evidence": "Not run because a prerequisite failed",
                    "duration_seconds": 0,
                }
            )
            continue
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
    parser.add_argument("mode", choices=["baseline", "fast", "pre", "final"])
    parser.add_argument("tag")
    parser.add_argument("base", nargs="?")
    parser.add_argument("candidate", nargs="?")
    parser.add_argument("--qualification-base")
    args = parser.parse_args()
    if args.mode == "final" and not args.qualification_base:
        parser.error("Final qualification requires --qualification-base at POC start")
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
        if marker.exists() and marker.read_text() == current:
            env = {**os.environ, "CI": "1"}
        else:
            env = provision(root, art / (args.tag + "-install.log"))
            marker.write_text(current)
        gate = Gate(root, art, args.tag, args.base, args.candidate, args.mode == "fast")
        gate.final = args.mode == "final"
        qualification_files = []
        if gate.final:
            gate.qualification_base = output(["git", "rev-parse", args.qualification_base], root)
            if gate.spec.get("qualification_base_sha") != gate.qualification_base:
                raise ValueError("Qualification base must match frozen acceptance metadata")
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", gate.qualification_base, args.candidate],
                cwd=root,
                check=True,
            )
            qualification_files = changed(root, gate.qualification_base, args.candidate)
            gate.py = [
                f
                for f in production(qualification_files, "python")
                if f.startswith("scripts/swarm/") == (gate.suite == "swarm")
            ]
            gate.ts = production(qualification_files, "web") if gate.suite == "poc" else []
        gate.runtime_env = env
        rows = gate.execute()
        try:
            hashes = artifact_hashes(
                art, args.tag, gate.spec, args.mode == "fast", args.mode == "final"
            )
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
            "test_policy": POC_TEST_POLICY,
            "stage": args.mode,
            "test_suite": gate.suite,
            "qualification_base_sha": gate.qualification_base if gate.final else None,
            "qualification_diff_files": qualification_files,
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
