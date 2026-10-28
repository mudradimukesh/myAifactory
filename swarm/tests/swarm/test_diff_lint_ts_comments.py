import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINT = ROOT / "scripts/swarm/diff_lint.py"


def typescript_modules() -> Path:
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return Path(common).parent / "web/node_modules"


def lint_added(tmp_path: Path, name: str, body: str) -> list[dict[str, object]]:
    modules = typescript_modules()
    assert (modules / "typescript").is_dir(), f"typescript is not installed under {modules}"
    repo = tmp_path / "repo"
    (repo / "web/src").mkdir(parents=True)
    (repo / "web/node_modules").symlink_to(modules)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    git("init", "-q")
    (repo / "web/src/.keep").write_text("")
    git("add", "web/src/.keep")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "web/src" / name).write_text(body)
    git("add", "web/src")
    git("commit", "-qm", "candidate")
    result = subprocess.run(
        [sys.executable, str(LINT), "--base", base, "--candidate", "HEAD", "--repo", str(repo)],
        cwd=ROOT / "scripts/swarm",
        text=True,
        capture_output=True,
    )
    findings: list[dict[str, object]] = json.loads(result.stdout)
    assert bool(findings) == bool(result.returncode)
    return [f for f in findings if f["reason"] == "added comment"]


def lines(findings: list[dict[str, object]]) -> list[int]:
    return [int(str(f["line"])) for f in findings]


def test_comment_after_template_substitution_is_reported(tmp_path: Path) -> None:
    body = (
        "export const path = (x: number, y: number) => `${x},${y}`;\n"
        "\n"
        "// whole-line comment after a template substitution\n"
        "export const k = 1;\n"
    )
    assert lines(lint_added(tmp_path, "a.ts", body)) == [3]


def test_comments_after_nested_template_and_jsx_are_reported(tmp_path: Path) -> None:
    body = (
        'export const label = (a: boolean) => `x${a ? `y${a}` : ""}z`;\n'
        "/* block\n"
        "   comment */\n"
        "export const View = () => <div>{`${label(true)}`}</div>;\n"
        "// trailing whole line\n"
        "export const n = 2; // inline\n"
    )
    assert lines(lint_added(tmp_path, "b.tsx", body)) == [2, 3, 5, 6]


def test_comment_tokens_inside_literals_and_jsx_text_are_not_reported(tmp_path: Path) -> None:
    body = (
        'export const a = "http://example.test";\n'
        "export const b = `// not a comment ${a} /* nor this */`;\n"
        "export const c = /\\/\\/ x/;\n"
        "export const d = 'a /* b */ c';\n"
        "export const View = () => <p>see // and /* here */ ok</p>;\n"
    )
    assert lint_added(tmp_path, "c.tsx", body) == []


def test_comment_only_in_added_lines_of_a_modified_file_is_reported(tmp_path: Path) -> None:
    body = (
        "export const t = (x: number) => `${x}`;\n"
        "export const u = (x: number) => `${x}-${x}`;\n"
        "  // indented comment\n"
    )
    assert lines(lint_added(tmp_path, "d.ts", body)) == [3]
