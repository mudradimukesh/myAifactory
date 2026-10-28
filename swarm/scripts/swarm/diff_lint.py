import argparse
import csv
import io
import json
import re
import subprocess
import tokenize
from pathlib import Path

from gate_evidence import changed, output

BANNED = re.compile(
    "\\bAny\\b|\\bcast\\s*\\(|#\\s*(?:type:\\s*ignore|noqa)|except\\s+Excep"
    "tion|pytest\\.skip|\\bxfail\\b|suppress_health_check|@ts-ignore|@"
    "ts-expect-error|eslint-disable|\\.(?:only|skip)\\s*\\(|test\\.fixm"
    "e|waitForTimeout"
)


def added_lines(patch: str) -> set[int]:
    result = set()
    current = 0
    for line in patch.splitlines():
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
        if match:
            current = int(match[1])
        elif line.startswith("+") and not line.startswith("+++"):
            result.add(current)
            current += 1
        elif not line.startswith(("-", "\\")):
            current += 1
    return result


def python_violations(text: str, lines: set[int]) -> list[tuple[int, str]]:
    errors = [
        (i, "banned construct")
        for i, line in enumerate(text.splitlines(), 1)
        if i in lines and BANNED.search(line)
    ]
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT and token.start[0] in lines:
            errors.append((token.start[0], "added comment"))
    return errors


def ts_comments(root: Path, name: str, text: str) -> list[int]:
    program = (
        "const ts=require('typescript');let s='';process.stdin.on('data"
        "',b=>s+=b);process.stdin.on('end',()=>{const file=ts.createSour"
        "ceFile(process.argv[1],s,ts.ScriptTarget.Latest,true);const li"
        "nes=[];const seen=new Set();const visit=node=>{const kids=node"
        ".getChildren(file);if(kids.length){kids.forEach(visit);return}"
        "if(node.kind===ts.SyntaxKind.JsxText)return;const found=(ts.ge"
        "tLeadingCommentRanges(s,node.pos)||[]).concat(ts.getTrailingCo"
        "mmentRanges(s,node.pos)||[]);for(const c of found){if(seen.has"
        "(c.pos))continue;seen.add(c.pos);const start=s.slice(0,c.pos)."
        "split('\\n').length;const count=s.slice(c.pos,c.end).split('"
        "\\n').length;for(let i=0;i<count;i++)lines.push(start+i)}};vi"
        "sit(file);console.log(JSON.stringify(lines))});"
    )
    result = subprocess.run(
        ["node", "-e", program, name],
        cwd=root / "web",
        input=text,
        text=True,
        capture_output=True,
        check=True,
    )
    lines: list[int] = json.loads(result.stdout)
    return lines


def fixture_literals(root: Path) -> set[str]:
    literals: set[str] = set()
    manifest = root / "corpus/manifest-p1-dev.csv"
    if manifest.exists():
        for row in csv.DictReader(manifest.open()):
            for key, value in row.items():
                if value and ("family" in key or "sha256" in key):
                    literals.add(value)
    literals.update(p.stem for p in (root / "tests/fixtures").rglob("*") if p.is_file())
    return literals


def file_findings(
    root: Path, file: str, text: str, lines: set[int], literals: set[str]
) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    if file.endswith(".py"):
        findings = python_violations(text, lines)
    elif file.endswith((".ts", ".tsx")):
        findings = [
            (i, "banned construct")
            for i, line in enumerate(text.splitlines(), 1)
            if i in lines and BANNED.search(line)
        ]
        findings += [(i, "added comment") for i in ts_comments(root, file, text) if i in lines]
    if file.startswith(("src/", "web/src/")):
        for i, line in enumerate(text.splitlines(), 1):
            if i in lines and any(
                re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", line)
                for value in literals
            ):
                findings.append((i, "fixture literal"))
    return findings


def check(root: Path, base: str, candidate: str, max_lines: int) -> list[dict[str, str | int]]:
    errors: list[dict[str, str | int]] = []
    literals = fixture_literals(root)
    for file in changed(root, base, candidate):
        path = root / file
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeError:
            continue
        if len(text.splitlines()) > max_lines:
            errors.append(
                {"file": file, "line": max_lines + 1, "reason": "file exceeds line limit"}
            )
        patch = output(["git", "diff", "--unified=0", base, candidate, "--", file], root)
        findings = file_findings(root, file, text, added_lines(patch), literals)
        errors += [{"file": file, "line": i, "reason": reason} for i, reason in findings]
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--max-lines", type=int, default=1000)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    errors = check(args.repo, args.base, args.candidate, args.max_lines)
    print(json.dumps(errors, indent=2))
    raise SystemExit(bool(errors))


if __name__ == "__main__":
    main()
