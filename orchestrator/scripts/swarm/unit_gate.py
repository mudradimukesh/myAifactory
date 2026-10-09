import argparse
import json
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from acceptance_check import browser_rows, check, mapped_nodes, python_rows
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
    project_adapter,
    provision,
    runtime,
)
from swarm_config import ART


def run_all(checks):
    results = []
    for identifier, action in checks:
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
        results.append(
            {
                "id": identifier,
                "disposition": "retained",
                "comparison": "max",
                **result,
                "duration_seconds": time.monotonic() - started,
            }
        )
    return results


class Gate:
    def __init__(self, root, art, tag, base, candidate):
        self.root, self.art, self.tag, self.base, self.candidate = (
            root,
            art,
            tag,
            base,
            candidate,
        )
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

    def file(self, suffix):
        return self.art / (self.tag + "-" + suffix)

    def commands(self, gate, commands):
        codes = []
        with self.file(gate + ".log").open("w") as log:
            for command in commands:
                result = subprocess.run(
                    command,
                    cwd=self.root,
                    env=self.env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                codes.append(result.returncode)
        return {
            "command": commands,
            "exit_code": max([abs(c) for c in codes], default=0),
            "measured": sum(c != 0 for c in codes),
            "threshold": 0,
            "evidence": str(self.file(gate + ".log")),
        }

    def frozen(self):
        record = json.loads(self.file("s1-tests.json").read_text())
        hashes = self.file("s1-tests.sha256").read_text().splitlines()
        if not hashes:
            raise ValueError("Frozen hashes missing")
        for row in hashes:
            expected, file = row.split(maxsplit=1)
            path = self.root / file.lstrip(" *")
            if (
                not path.resolve().is_relative_to(self.root.resolve())
                or digest(path) != expected
            ):
                raise ValueError("Frozen test hash differs")
        modified = output(
            [
                "git",
                "diff",
                "--name-only",
                record["tests_commit"],
                self.candidate,
                "--",
                "tests",
                "web/tests",
                "web/e2e",
            ],
            self.root,
        )
        if modified:
            raise ValueError("Frozen tests changed after tests commit: " + modified)
        return {
            "command": ["sha256", str(self.file("s1-tests.sha256"))],
            "exit_code": 0,
            "measured": 0,
            "threshold": 0,
        }

    def green(self):
        red = json.loads(self.file("red.json").read_text())
        frozen = json.loads(self.file("s1-tests.json").read_text())
        if (
            not red["passed"]
            or red["tests_commit"] != frozen["tests_commit"]
            or red["test_map_sha256"] != digest(self.file("test-map.json"))
        ):
            raise ValueError("Red proof differs from frozen tests")
        commands = []
        nodes = sorted(
            {
                node
                for m in self.mapping
                if m["runner"] == "pytest"
                for node in mapped_nodes(m)
            }
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
            self.env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = str(
                self.file("green-browser.json")
            )
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

    def mapped(self):
        check(self.mapping, self.file("ids.txt").read_text().splitlines(), self.reports)
        return {
            "command": ["acceptance_check.py"],
            "exit_code": 0,
            "measured": 0,
            "threshold": 0,
        }

    def diff(self, identifier):
        errors = lint_diff(self.root, self.base, self.candidate, 1000)
        selected = [
            e
            for e in errors
            if (e["reason"] == "file exceeds line limit") == (identifier == "G8")
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
                    "--cov=src",
                    "--cov-branch",
                    "--cov-report=xml:" + str(self.file("cov.xml")),
                ],
                [
                    str(self.root / ".venv/bin/diff-cover"),
                    str(self.file("cov.xml")),
                    "--compare-branch=" + self.base,
                    "--fail-under=80",
                ],
            ]
        if self.ts:
            unit = self.unit("G12-unit", False)
            if unit["exit_code"]:
                return unit
            report = self.root / "web/coverage/lcov.info"
            text = report.read_text()
            covered = [
                line.removeprefix("SF:")
                for line in text.splitlines()
                if line.startswith("SF:")
            ]
            for file in self.ts:
                if not any(
                    name.endswith(file) or name == file.removeprefix("web/")
                    for name in covered
                ):
                    raise ValueError(
                        "Changed production file absent from coverage: " + file
                    )
            normalized = self.file("coverage.lcov")
            normalized.write_text(
                "\n".join(
                    "SF:"
                    + str(
                        (self.root / "web" / line[3:]).resolve().relative_to(self.root)
                    )
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
                    "--fail-under=80",
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
                    "--repeat-each=5",
                    "--workers=1",
                    "--reporter=json",
                ]
            ],
        )
        mapping = [m for m in self.mapping if m["runner"] == "playwright"]
        check(
            mapping,
            [m["id"] for m in mapping],
            {"playwright": browser_rows(json.loads(report.read_text()))},
            repeats=5,
        )
        return result

    def execute(self):
        ruff = str(self.root / ".venv/bin/ruff")
        pytests = self.spec.get("python_test_paths", [])
        changed_python = [file for file in self.files if file.endswith(".py")]
        checks = [
            ("G1", self.frozen),
            ("G2", self.green),
            ("G3", self.mapped),
            (
                "G4",
                lambda: self.commands(
                    "G4",
                    [[ruff, "check", "."]]
                    + (
                        [[ruff, "format", "--check", *changed_python]]
                        if changed_python
                        else []
                    ),
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
                    "G10", [[self.python, "-m", "pytest", "-q", "-p", "randomly"]]
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
            (
                "G14",
                lambda: self.commands(
                    "G14", [["npm", "--prefix", "web", "run", "lint"]]
                ),
            ),
            (
                "G15",
                lambda: self.commands(
                    "G15", [["npm", "--prefix", "web", "run", "typecheck"]]
                ),
            ),
            ("G16", lambda: self.unit("G16")),
            ("G17", self.journey),
            ("G18", lambda: self.mutation("G18", "web", self.ts)),
        ]
        rows = run_all(checks)
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
                r"\s*--> (.+):\d+:\d+",
                lines[index + 1] if index + 1 < len(lines) else "",
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
    commands = project_adapter().baseline_commands(root)
    results = []
    for index, command in enumerate(commands):
        start = time.monotonic()
        log = art / (tag + f"-baseline-{index}.log")
        with log.open("w") as stream:
            result = subprocess.run(
                command,
                cwd=root,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        disposition = "passed" if result.returncode == 0 else "failed"
        known = art / f"stage1-baseline-{index}.log"
        if result.returncode == 1 and index in {2, 3} and known.exists():
            kind = "format" if index == 2 else "mypy"
            old = known.read_text()
            affected = sorted(
                {
                    line.split(":", 1)[0]
                    for line in old.splitlines()
                    if ": error:" in line
                }
            )
            if kind == "format":
                affected = format_diagnostics(old)
            baseline_sha = json.loads((art / "run.json").read_text())["baseline_sha"]
            unchanged = bool(affected) and not output(
                ["git", "diff", "--name-only", baseline_sha, "HEAD", "--", *affected],
                root,
            )
            if unchanged and known_diagnostics(log.read_text(), old, kind):
                disposition = "known_failure"
        results.append(
            {
                "command": command,
                "exit_code": result.returncode,
                "disposition": disposition,
                "known_evidence": str(known)
                if disposition == "known_failure"
                else None,
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
    parser.add_argument("mode", choices=["baseline", "pre"])
    parser.add_argument("tag")
    parser.add_argument("base", nargs="?")
    parser.add_argument("candidate", nargs="?")
    args = parser.parse_args()
    art = Path(os.environ.get("ART", str(ART))).resolve()
    os.environ["PATH"] = str(NODE.parent) + os.pathsep + os.environ["PATH"]
    try:
        if args.mode == "baseline":
            repo = Path.cwd()
            sha = output(["git", "rev-parse", "main"], repo)
            root = art / "baseline" / (args.tag + "-" + str(time.time_ns()))
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(root), sha],
                cwd=repo,
                check=True,
            )
            env = provision(root, art / (args.tag + "-baseline-install.log"))
            with runtime(root, art, args.tag + "-baseline", env) as env:
                return int(baseline(root, art, args.tag, env))
        receipt = json.loads((art / (args.tag + "-candidate.json")).read_text())
        root = Path(receipt["worktree"])
        if (
            receipt["candidate_sha"] != args.candidate
            or receipt["main_sha"] != args.base
        ):
            raise ValueError("Candidate arguments differ from receipt")
        if output(["git", "rev-parse", "HEAD"], root) != args.candidate:
            raise ValueError("Wrong candidate checkout")
        if output(["git", "status", "--porcelain", "--untracked-files=no"], root):
            raise ValueError("Candidate has unrelated tracked changes")
        env = provision(root, art / (args.tag + "-install.log"))
        gate = Gate(root, art, args.tag, args.base, args.candidate)
        with runtime(root, art, args.tag, env) as isolated:
            gate.env.update(isolated)
            rows = gate.execute()
        packet = {
            "candidate_sha": args.candidate,
            "base_sha": args.base,
            "worktree": str(root),
            "tree": output(["git", "rev-parse", "HEAD^{tree}"], root),
            "fingerprint": fingerprint(root),
            "diff_files": gate.files,
            "gates": rows,
            "artifact_hashes": artifact_hashes(art, args.tag, gate.spec),
        }
        (art / (args.tag + "-gate.json")).write_text(
            json.dumps(packet, indent=2) + "\n"
        )
        failures = [r for r in rows if r["exit_code"] != 0]
        for line in json.dumps(failures, indent=2).splitlines()[:100]:
            if failures:
                print(line)
        return int(bool(failures))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, "FAIL: " + str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
