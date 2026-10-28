import json
import subprocess
import sys
from pathlib import Path

from test_diff_lint_ts_comments import LINT, ROOT, lines, typescript_modules


def lint_modified(tmp_path: Path, base_body: str, new_body: str) -> list[dict[str, object]]:
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
    target = repo / "web/src/m.ts"
    target.write_text(base_body)
    git("add", "web/src")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    target.write_text(new_body)
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


def test_only_the_added_comment_is_reported_when_old_comments_remain(tmp_path: Path) -> None:
    base = (
        "// existing line comment\n"
        "export const a = 1;\n"
        "/* existing block comment */\n"
        "export const b = 2;\n"
    )
    new = (
        "// existing line comment\n"
        "export const a = 1;\n"
        "/* existing block comment */\n"
        "// new line comment\n"
        "export const b = 3;\n"
    )
    assert lines(lint_modified(tmp_path, base, new)) == [4]


def test_deleted_comment_line_is_not_reported(tmp_path: Path) -> None:
    base = "// to be removed\nexport const a = 1;\n"
    new = "export const a = 1;\n"
    assert lint_modified(tmp_path, base, new) == []


def test_only_added_middle_lines_of_a_block_comment_are_reported(tmp_path: Path) -> None:
    base = "export const a = 1;\n/* one\n   two\n   three\n   four */\nexport const b = 2;\n"
    new = (
        "export const a = 1;\n"
        "/* one\n"
        "   two\n"
        "   added one\n"
        "   added two\n"
        "   three\n"
        "   four */\n"
        "export const b = 2;\n"
    )
    assert lines(lint_modified(tmp_path, base, new)) == [4, 5]


def test_added_jsdoc_plain_and_tagged_is_reported(tmp_path: Path) -> None:
    base = "export const a = 1;\n"
    new = (
        "export const a = 1;\n"
        "/** plain text */\n"
        "export function f(x: number) { return x; }\n"
        "/** @param x tagged text */\n"
        "export function g(x: number) { return x; }\n"
    )
    assert lines(lint_modified(tmp_path, base, new)) == [2, 4]


def test_added_unterminated_block_comment_to_end_of_file_is_reported(tmp_path: Path) -> None:
    base = "export const a = 1;\n"
    new = "export const a = 1;\n/* never closed\nstill open\n"
    assert lines(lint_modified(tmp_path, base, new)) == [2, 3]


def test_comment_like_text_in_added_literals_is_not_reported(tmp_path: Path) -> None:
    base = "export const a = 1;\n"
    new = (
        "export const a = 1;\n"
        "export const s = \"// no\" + '/* no */' + `// no ${a} /* no */` + /\\/\\/ no/.source;\n"
    )
    assert lint_modified(tmp_path, base, new) == []
