import argparse
import builtins
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/swarm/evidence_write.py"
LEGACY_ART = Path("/tmp/factory-fp-0925/swarm/interior-p1-poc")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        self.art = tmp_path / "art"
        (self.repo / "scripts/swarm").mkdir(parents=True)
        (self.art / "logs").mkdir(parents=True)
        self.legacy = tmp_path / "legacy-default"
        self.legacy.mkdir(mode=0o555)
        source = SCRIPT.read_text()
        assert str(LEGACY_ART) in source
        (self.repo / "scripts/swarm/evidence_write.py").write_text(
            source.replace(str(LEGACY_ART), str(self.legacy))
        )
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        self.predecessor = self.commit({"file": "base"}, "base")
        self.tests = self.commit({"tests/test_unit.py": "def test_unit():\n    assert False\n"})
        (self.art / "u-test-map.json").write_text('{"tag": "u"}\n')
        self.red(self.tests)

    def commit(self, files: dict[str, str | None], message: str = "tests") -> str:
        for name, text in files.items():
            if text is None:
                git(self.repo, "rm", "-q", name)
                continue
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
            git(self.repo, "add", name)
        git(self.repo, "commit", "-qm", message)
        return git(self.repo, "rev-parse", "HEAD")

    def red(self, tests_commit: str, passed: bool = True) -> None:
        receipt = {
            "passed": passed,
            "tests_commit": tests_commit,
            "predecessor": self.predecessor,
            "test_map_sha256": hashlib.sha256(
                (self.art / "u-test-map.json").read_bytes()
            ).hexdigest(),
        }
        (self.art / "u-red.json").write_text(json.dumps(receipt))

    def run(self, *args: str, art: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "ART": str(art or self.art)}
        return subprocess.run(
            [sys.executable, str(self.repo / "scripts/swarm/evidence_write.py"), *args],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
        )

    def s1(self, *extra: str, art: Path | None = None) -> subprocess.CompletedProcess[str]:
        return self.run("s1", "u", self.tests, self.predecessor, *extra, art=art)

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.art)): path.read_bytes()
            for path in sorted(self.art.rglob("*"))
            if path.is_file()
        }


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    monkeypatch.delenv("REPO", raising=False)
    return Fixture(tmp_path)


def live_record(fixture: Fixture) -> None:
    record = {
        "feature": "u",
        "predecessor": fixture.predecessor,
        "tests_commit": "b5687a30fb726c50d5029be1d487cde61aef5220",
        "red_receipt": "u-red.json",
        "frozen_files": ["tests/test_unit.py"],
    }
    (fixture.art / "u-s1-tests.json").write_text(json.dumps(record))
    (fixture.art / "u-s1-tests.sha256").write_text("0" * 64 + "  tests/test_unit.py\n")


def writer(fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("ART", str(fixture.art))
    spec = importlib.util.spec_from_file_location(
        "evidence_write_fixture", fixture.repo / "scripts/swarm/evidence_write.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def s1_args(fixture: Fixture) -> argparse.Namespace:
    return argparse.Namespace(
        tag="u",
        tests=fixture.tests,
        predecessor=fixture.predecessor,
        red="u-red.json",
        correction=None,
    )


def frozen_rows(fixture: Fixture, *paths: str) -> str:
    return "".join(
        hashlib.sha256(
            subprocess.check_output(["git", "show", f"{fixture.tests}:{path}"], cwd=fixture.repo)
        ).hexdigest()
        + f"  {path}\n"
        for path in paths
    )


def test_default_art_is_the_p1_poc_run_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ART", raising=False)
    spec = importlib.util.spec_from_file_location("evidence_write_default", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ART == LEGACY_ART


def test_s1_writes_into_art_override_and_backs_up_existing_records(fixture: Fixture) -> None:
    live_record(fixture)
    old = fixture.snapshot()
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode == 0, result.stderr
    record = json.loads((fixture.art / "u-s1-tests.json").read_text())
    assert record == {
        "feature": "u",
        "predecessor": fixture.predecessor,
        "tests_commit": fixture.tests,
        "red_receipt": "u-red.json",
        "frozen_files": ["tests/test_unit.py"],
        "test_map": "u-test-map.json",
        "red": "u-red.json",
        "test_map_sha256": json.loads((fixture.art / "u-red.json").read_text())["test_map_sha256"],
    }
    digest = hashlib.sha256((fixture.repo / "tests/test_unit.py").read_bytes()).hexdigest()
    assert (fixture.art / "u-s1-tests.sha256").read_text() == f"{digest}  tests/test_unit.py\n"
    for name in ("u-s1-tests.json", "u-s1-tests.sha256"):
        assert (fixture.art / "logs" / f"{name}.b5687a3.bak").read_bytes() == old[name]
    assert not any(fixture.legacy.iterdir())


def test_s1_records_the_full_tests_commit_for_an_abbreviated_argument(fixture: Fixture) -> None:
    result = fixture.run(
        "s1", "u", fixture.tests[:8], fixture.predecessor[:8], "--red", "u-red.json"
    )
    assert result.returncode == 0, result.stderr
    record = json.loads((fixture.art / "u-s1-tests.json").read_text())
    assert record["tests_commit"] == fixture.tests
    assert record["predecessor"] == fixture.predecessor


def test_s1_refuses_missing_art(fixture: Fixture, tmp_path: Path) -> None:
    missing = tmp_path / "missing-art"
    result = fixture.s1("--red", "u-red.json", art=missing)
    assert result.returncode != 0
    assert "ART" in result.stderr and str(missing) in result.stderr
    assert "Traceback" not in result.stderr
    assert not missing.exists()


def test_s1_refuses_unwritable_art_and_leaves_live_records(fixture: Fixture) -> None:
    live_record(fixture)
    before = fixture.snapshot()
    fixture.art.chmod(0o555)
    try:
        result = fixture.s1("--red", "u-red.json")
    finally:
        fixture.art.chmod(0o755)
    assert result.returncode != 0
    assert "not writable" in result.stderr and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


def test_s1_refuses_unwritable_logs_before_any_write(fixture: Fixture) -> None:
    live_record(fixture)
    before = fixture.snapshot()
    (fixture.art / "logs").chmod(0o555)
    try:
        result = fixture.s1("--red", "u-red.json")
    finally:
        (fixture.art / "logs").chmod(0o755)
    assert result.returncode != 0
    assert "not writable" in result.stderr and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


def test_s1_refuses_missing_red_receipt(fixture: Fixture) -> None:
    live_record(fixture)
    (fixture.art / "u-red.json").unlink()
    before = fixture.snapshot()
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode != 0
    assert "Red receipt missing" in result.stderr and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


def test_s1_refuses_red_receipt_not_named_for_the_tag(fixture: Fixture) -> None:
    shutil.copy2(fixture.art / "u-red.json", fixture.art / "other-red.json")
    before = fixture.snapshot()
    result = fixture.s1("--red", "other-red.json")
    assert result.returncode != 0
    assert "u-red.json" in result.stderr and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


@pytest.mark.parametrize(
    "receipt",
    [
        {"passed": True, "tests_commit": "0" * 40},
        {"passed": False},
        {"passed": True, "predecessor": "0" * 40},
    ],
)
def test_s1_refuses_red_receipt_bound_to_other_commits(
    fixture: Fixture, receipt: dict[str, object]
) -> None:
    red = json.loads((fixture.art / "u-red.json").read_text())
    (fixture.art / "u-red.json").write_text(json.dumps({**red, **receipt}))
    live_record(fixture)
    before = fixture.snapshot()
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode != 0
    assert "Red receipt" in result.stderr and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


def test_s1_refuses_to_overwrite_a_different_existing_backup(fixture: Fixture) -> None:
    live_record(fixture)
    backup = fixture.art / "logs/u-s1-tests.sha256.b5687a3.bak"
    backup.write_text("earlier backup\n")
    before = fixture.snapshot()
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode != 0
    assert "Backup already exists" in result.stderr and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


@pytest.mark.parametrize("change", ["missing", "changed", "undigested"])
def test_s1_refuses_a_red_receipt_not_bound_to_the_current_map(
    fixture: Fixture, change: str
) -> None:
    live_record(fixture)
    test_map = fixture.art / "u-test-map.json"
    if change == "missing":
        test_map.unlink()
    elif change == "changed":
        test_map.write_text('{"tag": "changed"}\n')
    else:
        red = json.loads((fixture.art / "u-red.json").read_text())
        del red["test_map_sha256"]
        (fixture.art / "u-red.json").write_text(json.dumps(red))
    before = fixture.snapshot()
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode != 0
    assert "test map" in result.stderr.lower() and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


@pytest.mark.parametrize("present", ["u-s1-tests.json", "u-s1-tests.sha256"])
def test_s1_refuses_an_incomplete_live_pair(fixture: Fixture, present: str) -> None:
    live_record(fixture)
    for name in {"u-s1-tests.json", "u-s1-tests.sha256"} - {present}:
        (fixture.art / name).unlink()
    before = fixture.snapshot()
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode != 0
    assert "Incomplete s1 pair" in result.stderr and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


def test_s1_restores_the_live_pair_when_the_second_publication_fails(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_record(fixture)
    before = fixture.snapshot()
    module = writer(fixture, monkeypatch)
    replace = os.replace

    def fail_record(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) == fixture.art / "u-s1-tests.json":
            raise OSError("injected record publication failure")
        replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_record)
    with pytest.raises(SystemExit, match="injected record publication failure"):
        module.s1(s1_args(fixture))
    assert fixture.snapshot() == before


def test_s1_keeps_verified_backups_when_restoring_the_live_pair_fails(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_record(fixture)
    before = fixture.snapshot()
    module = writer(fixture, monkeypatch)
    replace = os.replace

    def fail_record(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) == fixture.art / "u-s1-tests.json":
            raise OSError("injected record publication failure")
        replace(src, dst)

    def fail_restore(path: Path, data: bytes) -> int:
        raise OSError("injected restore failure")

    monkeypatch.setattr(os, "replace", fail_record)
    monkeypatch.setattr(Path, "write_bytes", fail_restore)
    with pytest.raises(SystemExit, match="injected restore failure") as refused:
        module.s1(s1_args(fixture))
    for name in ("u-s1-tests.json", "u-s1-tests.sha256"):
        backup = fixture.art / "logs" / f"{name}.b5687a3.bak"
        assert str(backup) in str(refused.value)
        assert backup.read_bytes() == before[name]


def test_s1_keeps_the_live_pair_when_a_backup_copy_fails(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_record(fixture)
    before = fixture.snapshot()
    module = writer(fixture, monkeypatch)
    copy2 = shutil.copy2

    def fail_hashes_backup(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> object:
        if Path(dst).name == "u-s1-tests.sha256.b5687a3.bak":
            Path(dst).write_bytes(b"partial")
            raise OSError("injected backup copy failure")
        return copy2(src, dst)

    monkeypatch.setattr(shutil, "copy2", fail_hashes_backup)
    with pytest.raises(SystemExit, match="injected backup copy failure"):
        module.s1(s1_args(fixture))
    assert fixture.snapshot() == before


def test_s1_refuses_a_map_changed_after_publication_starts(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_record(fixture)
    test_map = fixture.art / "u-test-map.json"
    before = {**fixture.snapshot(), "u-test-map.json": b'{"tag": "stale"}\n'}
    module = writer(fixture, monkeypatch)
    copy2 = shutil.copy2

    def change_map(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> object:
        test_map.write_bytes(before["u-test-map.json"])
        return copy2(src, dst)

    monkeypatch.setattr(shutil, "copy2", change_map)
    with pytest.raises(SystemExit, match="Test map"):
        module.s1(s1_args(fixture))
    assert fixture.snapshot() == before


def test_s1_refuses_a_live_pair_changed_after_publication_starts(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_record(fixture)
    hashes = fixture.art / "u-s1-tests.sha256"
    before = {**fixture.snapshot(), "u-s1-tests.sha256": b"1" * 64 + b"  tests/test_unit.py\n"}
    module = writer(fixture, monkeypatch)
    copy2 = shutil.copy2

    def change_hashes(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> object:
        copied = copy2(src, dst)
        if Path(src) == hashes:
            hashes.write_bytes(before["u-s1-tests.sha256"])
        return copied

    monkeypatch.setattr(shutil, "copy2", change_hashes)
    with pytest.raises(SystemExit, match="changed while writing"):
        module.s1(s1_args(fixture))
    assert fixture.snapshot() == before


def entries(art: Path) -> dict[str, tuple[str, int, bytes]]:
    return {
        str(path.relative_to(art)): (
            ("link", 0, os.readlink(path).encode())
            if path.is_symlink()
            else ("file", path.stat().st_ino, path.read_bytes())
        )
        for path in sorted(art.rglob("*"))
        if path.is_symlink() or path.is_file()
    }


def occupy(fixture: Fixture, name: str, kind: str) -> None:
    staged = fixture.art / f".{name}.staged"
    if kind == "symlink":
        staged.symlink_to(name)
    elif kind == "hardlink":
        os.link(fixture.art / name, staged)
    else:
        staged.write_text("leftover\n")


@pytest.mark.parametrize("name", ["u-s1-tests.sha256", "u-s1-tests.json"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "regular"])
def test_s1_refuses_an_occupied_staging_path(fixture: Fixture, name: str, kind: str) -> None:
    live_record(fixture)
    occupy(fixture, name, kind)
    before = entries(fixture.art)
    result = fixture.s1("--red", "u-red.json")
    assert entries(fixture.art) == before
    assert result.returncode != 0
    assert "Staging path" in result.stderr and "Traceback" not in result.stderr


@pytest.mark.parametrize("name", ["u-s1-tests.sha256", "u-s1-tests.json"])
def test_s1_refuses_a_staging_path_occupied_after_preflight(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    live_record(fixture)
    module = writer(fixture, monkeypatch)
    git = module.git
    before: dict[str, tuple[str, int, bytes]] = {}

    def occupy_then_git(*args: str) -> bytes:
        if args[0] == "show" and not before:
            occupy(fixture, name, "symlink")
            before.update(entries(fixture.art))
        result: bytes = git(*args)
        return result

    monkeypatch.setattr(module, "git", occupy_then_git)
    with pytest.raises(SystemExit) as refused:
        module.s1(s1_args(fixture))
    assert before and entries(fixture.art) == before
    assert "Staging path" in str(refused.value)


@pytest.mark.parametrize("name", ["u-s1-tests.sha256", "u-s1-tests.json"])
def test_s1_refuses_a_staging_path_occupied_at_creation(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    live_record(fixture)
    staged = fixture.art / f".{name}.staged"
    before = {**entries(fixture.art), staged.name: ("link", 0, name.encode())}
    module = writer(fixture, monkeypatch)

    def at_creation[**P, R](opener: Callable[P, R]) -> Callable[P, R]:
        def occupy_then_open(*args: P.args, **kwargs: P.kwargs) -> R:
            if args[0] in (staged, str(staged)) and not os.path.lexists(staged):
                occupy(fixture, name, "symlink")
            return opener(*args, **kwargs)

        return occupy_then_open

    for owner, attribute in ((os, "open"), (io, "open"), (builtins, "open")):
        monkeypatch.setattr(owner, attribute, at_creation(getattr(owner, attribute)))
    with pytest.raises(SystemExit, match="Staging path"):
        module.s1(s1_args(fixture))
    assert entries(fixture.art) == before


def fail_record_publication(fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    replace = os.replace

    def fail_record(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) == fixture.art / "u-s1-tests.json":
            raise OSError("injected record publication failure")
        replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_record)


def fail_unlink(monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    unlink = Path.unlink

    def refuse(path: Path, missing_ok: bool = False) -> None:
        if path.name.endswith(suffix):
            raise OSError(f"injected cleanup failure for {path.name}")
        unlink(path, missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)


def test_s1_reports_the_publication_failure_when_staging_cleanup_fails(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_record(fixture)
    before = fixture.snapshot()
    module = writer(fixture, monkeypatch)
    fail_record_publication(fixture, monkeypatch)
    fail_unlink(monkeypatch, ".staged")
    with pytest.raises(SystemExit) as refused:
        module.s1(s1_args(fixture))
    message = str(refused.value)
    assert "injected record publication failure" in message
    assert "injected cleanup failure for .u-s1-tests.json.staged" in message
    assert str(fixture.art / ".u-s1-tests.json.staged") in message
    after = fixture.snapshot()
    assert ".u-s1-tests.json.staged" in after
    after.pop(".u-s1-tests.json.staged")
    assert after == before


def test_s1_names_retained_backups_when_restoration_and_staging_cleanup_fail(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_record(fixture)
    before = fixture.snapshot()
    module = writer(fixture, monkeypatch)

    def fail_restore(path: Path, data: bytes) -> int:
        raise OSError("injected restore failure")

    fail_record_publication(fixture, monkeypatch)
    monkeypatch.setattr(Path, "write_bytes", fail_restore)
    fail_unlink(monkeypatch, ".staged")
    with pytest.raises(SystemExit) as refused:
        module.s1(s1_args(fixture))
    message = str(refused.value)
    assert "injected record publication failure" in message
    assert "injected restore failure" in message
    assert "injected cleanup failure for .u-s1-tests.json.staged" in message
    for name in ("u-s1-tests.json", "u-s1-tests.sha256"):
        backup = fixture.art / "logs" / f"{name}.b5687a3.bak"
        assert str(backup) in message
        assert backup.read_bytes() == before[name]


def test_s1_reports_the_publication_failure_when_backup_cleanup_fails(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_record(fixture)
    before = fixture.snapshot()
    module = writer(fixture, monkeypatch)
    fail_record_publication(fixture, monkeypatch)
    fail_unlink(monkeypatch, ".bak")
    with pytest.raises(SystemExit) as refused:
        module.s1(s1_args(fixture))
    message = str(refused.value)
    assert "injected record publication failure" in message
    for name in ("u-s1-tests.json", "u-s1-tests.sha256"):
        backup = fixture.art / "logs" / f"{name}.b5687a3.bak"
        assert f"injected cleanup failure for {backup.name}" in message
        assert str(backup) in message
    after = fixture.snapshot()
    for name in ("u-s1-tests.json", "u-s1-tests.sha256"):
        assert after.pop(f"logs/{name}.b5687a3.bak") == before[name]
    assert after == before


def test_s1_does_not_report_success_when_staging_cleanup_fails(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = writer(fixture, monkeypatch)
    fail_unlink(monkeypatch, ".staged")
    with pytest.raises(SystemExit, match="injected cleanup failure") as refused:
        module.s1(s1_args(fixture))
    assert "u-s1-tests.json" in str(refused.value)
    record = json.loads((fixture.art / "u-s1-tests.json").read_text())
    assert record["tests_commit"] == fixture.tests


def test_s1_with_red_freezes_tests_after_an_intervening_production_commit(
    fixture: Fixture,
) -> None:
    fixture.predecessor = fixture.tests
    fixture.commit({"src/app.py": "app\n"}, "production")
    fixture.tests = fixture.commit({"tests/test_review.py": "def test_review():\n    pass\n"})
    fixture.red(fixture.tests)
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode == 0, result.stderr
    hashes = (fixture.art / "u-s1-tests.sha256").read_text()
    assert hashes == frozen_rows(fixture, "tests/test_review.py")
    record = json.loads((fixture.art / "u-s1-tests.json").read_text())
    assert (record["tests_commit"], record["predecessor"]) == (fixture.tests, fixture.predecessor)


def test_s1_without_red_still_refuses_an_intervening_production_commit(
    fixture: Fixture,
) -> None:
    fixture.predecessor = fixture.tests
    fixture.commit({"src/app.py": "app\n"}, "production")
    fixture.tests = fixture.commit({"tests/test_review.py": "def test_review():\n    pass\n"})
    before = fixture.snapshot()
    result = fixture.s1()
    assert result.returncode != 0
    assert "src/app.py" in result.stderr and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


def test_s1_freezes_every_test_file_across_earlier_and_tip_test_commits(
    fixture: Fixture,
) -> None:
    fixture.commit({"tests/test_a.py": "def test_a():\n    pass\n"})
    fixture.tests = fixture.commit({"tests/test_b.py": "def test_b():\n    pass\n"})
    fixture.red(fixture.tests)
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode == 0, result.stderr
    hashes = (fixture.art / "u-s1-tests.sha256").read_text()
    assert hashes == frozen_rows(
        fixture, "tests/test_a.py", "tests/test_b.py", "tests/test_unit.py"
    )


def test_s1_with_red_refuses_a_tip_commit_with_production_edits(fixture: Fixture) -> None:
    fixture.tests = fixture.commit(
        {"src/app.py": "app\n", "tests/test_review.py": "def test_review():\n    pass\n"}
    )
    fixture.red(fixture.tests)
    live_record(fixture)
    before = fixture.snapshot()
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode != 0
    assert "src/app.py" in result.stderr and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


@pytest.mark.parametrize("red", [True, False])
def test_s1_refuses_deleted_test_paths(fixture: Fixture, red: bool) -> None:
    fixture.predecessor = fixture.tests
    fixture.tests = fixture.commit(
        {"tests/test_unit.py": None, "tests/test_new.py": "def test_new():\n    pass\n"}
    )
    fixture.red(fixture.tests)
    live_record(fixture)
    before = fixture.snapshot()
    result = fixture.s1(*(("--red", "u-red.json") if red else ()))
    assert result.returncode != 0
    assert "tests/test_unit.py" in result.stderr and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


def test_s1_refreshes_frozen_files_and_keeps_the_np2_record_shape(fixture: Fixture) -> None:
    fixture.tests = fixture.commit(
        {
            "tests/test_unit.py": "def test_unit():\n    assert False\n",
            "tests/test_extra.py": "def test_extra():\n    assert False\n",
        }
    )
    fixture.red(fixture.tests)
    live_record(fixture)
    old = fixture.snapshot()
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode == 0, result.stderr
    text = (fixture.art / "u-s1-tests.json").read_text()
    record = json.loads(text)
    assert list(record) == [
        "feature",
        "predecessor",
        "tests_commit",
        "red_receipt",
        "frozen_files",
        "test_map",
        "red",
        "test_map_sha256",
    ]
    assert (
        record["test_map_sha256"]
        == json.loads((fixture.art / "u-red.json").read_text())["test_map_sha256"]
    )
    assert text == json.dumps(record)
    assert record["frozen_files"] == ["tests/test_extra.py", "tests/test_unit.py"]
    assert record["red_receipt"] == "u-red.json"
    rows = (fixture.art / "u-s1-tests.sha256").read_text().splitlines()
    assert [row.split("  ")[1] for row in rows] == record["frozen_files"]
    for name in ("u-s1-tests.json", "u-s1-tests.sha256"):
        assert (fixture.art / "logs" / f"{name}.b5687a3.bak").read_bytes() == old[name]


def test_s1_reuses_an_identical_existing_backup_pair(fixture: Fixture) -> None:
    live_record(fixture)
    old = fixture.snapshot()
    for name in ("u-s1-tests.json", "u-s1-tests.sha256"):
        (fixture.art / "logs" / f"{name}.b5687a3.bak").write_bytes(old[name])
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode == 0, result.stderr
    for name in ("u-s1-tests.json", "u-s1-tests.sha256"):
        assert (fixture.art / "logs" / f"{name}.b5687a3.bak").read_bytes() == old[name]


@pytest.mark.parametrize("name", ["u-s1-tests.json", "u-s1-tests.sha256"])
def test_s1_refuses_a_backup_name_holding_other_bytes_for_either_file(
    fixture: Fixture, name: str
) -> None:
    live_record(fixture)
    (fixture.art / "logs" / f"{name}.b5687a3.bak").write_text("someone else's bytes\n")
    before = fixture.snapshot()
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode != 0
    assert "Backup already exists" in result.stderr and "Traceback" not in result.stderr
    assert fixture.snapshot() == before


def map_digest(fixture: Fixture) -> str:
    return hashlib.sha256((fixture.art / "u-test-map.json").read_bytes()).hexdigest()


def test_s1_rebinding_with_red_refreshes_the_map_digest(fixture: Fixture) -> None:
    live_record(fixture)
    path = fixture.art / "u-s1-tests.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "test_map_sha256": "d" * 64}))
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode == 0, result.stderr
    record = json.loads(path.read_text())
    red = json.loads((fixture.art / "u-red.json").read_text())
    assert record.get("test_map_sha256") == red["test_map_sha256"] == map_digest(fixture), (
        f"record holds {record.get('test_map_sha256')}, receipt {red['test_map_sha256']}"
    )


def test_s1_new_record_with_red_records_the_map_digest(fixture: Fixture) -> None:
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode == 0, result.stderr
    record = json.loads((fixture.art / "u-s1-tests.json").read_text())
    red = json.loads((fixture.art / "u-red.json").read_text())
    assert record.get("test_map_sha256") == red["test_map_sha256"] == map_digest(fixture), (
        f"record holds {record.get('test_map_sha256')}, receipt {red['test_map_sha256']}"
    )


def test_s1_uses_the_repo_named_by_the_environment(fixture: Fixture, tmp_path: Path) -> None:
    installed = tmp_path / "installed/swarm/bin/evidence_write.py"
    installed.parent.mkdir(parents=True)
    shutil.copy(fixture.repo / "scripts/swarm/evidence_write.py", installed)
    env = {**os.environ, "ART": str(fixture.art), "REPO": str(fixture.repo)}
    result = subprocess.run(
        [
            sys.executable,
            str(installed),
            "s1",
            "u",
            fixture.tests,
            fixture.predecessor,
            *["--red", "u-red.json"],
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    record = json.loads((fixture.art / "u-s1-tests.json").read_text())
    assert record["tests_commit"] == fixture.tests


def test_s1_refuses_a_repo_that_is_not_a_work_tree(fixture: Fixture, tmp_path: Path) -> None:
    live_record(fixture)
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    before = fixture.snapshot()
    env = {**os.environ, "ART": str(fixture.art), "REPO": str(elsewhere)}
    result = subprocess.run(
        [
            sys.executable,
            str(fixture.repo / "scripts/swarm/evidence_write.py"),
            "s1",
            "u",
            *[fixture.tests, fixture.predecessor, "--red", "u-red.json"],
        ],
        cwd=fixture.repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"exit {result.returncode}, stderr {result.stderr!r}"
    assert str(elsewhere) in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert fixture.snapshot() == before
