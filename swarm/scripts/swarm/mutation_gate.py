import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import NotRequired, TypedDict

from diff_lint import added_lines
from gate_evidence import output


class Position(TypedDict):
    line: int


class Mutant(TypedDict):
    id: str
    status: str
    location: dict[str, Position]
    replacement: NotRequired[str]
    mutatorName: NotRequired[str]


class MutatedFile(TypedDict):
    mutants: list[Mutant]


class MutationDocument(TypedDict):
    files: dict[str, MutatedFile]


class Survivor(TypedDict):
    id: str
    file: str
    line: int
    status: str
    contract_relevant: bool
    replacement: NotRequired[str | None]
    mutator: NotRequired[str | None]
    diff: NotRequired[str]


class MutationPacket(TypedDict):
    score: float
    killed_or_timeout: int
    total: int
    survivors: list[Survivor]
    command: NotRequired[list[str]]
    runner_exit: NotRequired[int]
    report: NotRequired[str]


def score_web(document: MutationDocument, changed_lines: dict[str, set[int]]) -> MutationPacket:
    killed = 0
    total = 0
    survivors: list[Survivor] = []
    for file, data in document["files"].items():
        file = file if file.startswith("web/") else "web/" + file
        for mutant in data["mutants"]:
            status = mutant["status"]
            if status == "CompileError":
                continue
            if status not in {"Killed", "Timeout", "Survived", "NoCoverage"}:
                raise ValueError("Mutation infrastructure error: " + status)
            total += 1
            if status in {"Killed", "Timeout"}:
                killed += 1
            else:
                line = mutant["location"]["start"]["line"]
                survivors.append(
                    {
                        "id": file + ":" + mutant["id"],
                        "file": file,
                        "line": line,
                        "status": status,
                        "contract_relevant": line in changed_lines.get(file, set()),
                        "replacement": mutant.get("replacement"),
                        "mutator": mutant.get("mutatorName"),
                    }
                )
    if total == 0:
        raise ValueError("No executable mutants")
    return {
        "score": 100 * killed / total,
        "killed_or_timeout": killed,
        "total": total,
        "survivors": survivors,
    }


def web(root: Path, files: list[str], lines: dict[str, set[int]], log: Path) -> MutationPacket:
    command = [
        "npm",
        "run",
        "mutation",
        "--",
        "--mutate",
        ",".join(f.removeprefix("web/") for f in files),
    ]
    with log.open("w") as stream:
        result = subprocess.run(command, cwd=root / "web", stdout=stream, stderr=subprocess.STDOUT)
    report = root / "web/coverage/mutation.json"
    packet = score_web(json.loads(report.read_text()), lines)
    text = log.read_text()
    match = re.search(r"Ran\s+([\d.]+)\s+tests? per mutant", text)
    if not match or float(match[1]) <= 0:
        raise ValueError("Stryker did not prove positive tests per mutant")
    packet["command"] = command
    packet["runner_exit"] = result.returncode
    packet["report"] = str(report)
    return packet


def python(root: Path, files: list[str], lines: dict[str, set[int]], log: Path) -> MutationPacket:
    executable = str(root / ".venv/bin/mutmut")
    modules = {f.removeprefix("src/").removesuffix(".py").replace("/", "."): f for f in files}
    command = [executable, "run", "--max-children", "1", *[m + ".*" for m in modules]]
    with log.open("w") as stream:
        result = subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT)
        subprocess.run(
            [executable, "export-cicd-stats"],
            cwd=root,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=True,
        )
    statuses = output([executable, "results", "--all", "true"], root)
    killed, total = 0, 0
    survivors: list[Survivor] = []
    for row in statuses.splitlines():
        if ": " not in row:
            continue
        name, status = row.strip().rsplit(": ", 1)
        module = next((m for m in modules if name.startswith(m + ".")), None)
        if not module:
            continue
        if status not in {"killed", "timeout", "survived", "no tests"}:
            raise ValueError("Incomplete Python mutation: " + row)
        total += 1
        if status in {"killed", "timeout"}:
            killed += 1
        else:
            diff = output([executable, "show", name], root)
            hunk = re.search(r"@@ -([0-9]+)", diff)
            if not hunk:
                raise ValueError("Survivor diff has no line evidence")
            line = int(hunk[1])
            file = modules[module]
            survivors.append(
                {
                    "id": name,
                    "file": file,
                    "line": line,
                    "status": status,
                    "contract_relevant": line in lines[file],
                    "diff": diff,
                }
            )
    if total == 0:
        raise ValueError("No selected Python mutants")
    return {
        "score": 100 * killed / total,
        "killed_or_timeout": killed,
        "total": total,
        "survivors": survivors,
        "command": command,
        "runner_exit": result.returncode,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changed", nargs="+", required=True)
    parser.add_argument("--language", choices=["python", "web"], required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--threshold", type=float, default=70)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    lines = {
        f: added_lines(
            output(["git", "diff", "--unified=0", args.base, args.candidate, "--", f], args.repo)
        )
        for f in args.changed
    }
    try:
        packet = (web if args.language == "web" else python)(
            args.repo, args.changed, lines, args.out.with_suffix(".log")
        )
        args.out.write_text(json.dumps(packet, indent=2) + "\n")
        if packet["score"] < args.threshold or packet["runner_exit"] != 0:
            raise ValueError("Mutation threshold or runner failed")
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, "FAIL: " + str(error) + "\n")
    print("PASS")


if __name__ == "__main__":
    main()
