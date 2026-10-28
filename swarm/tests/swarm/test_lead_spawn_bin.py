"""A lead-spawn.sh rollover must give BIN every module fp-guard.py imports."""

from __future__ import annotations

import os
import resource
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SWARM = Path(__file__).resolve().parents[2] / "scripts" / "swarm"


def make_repo(root: Path, drop: str | None = None) -> Path:
    scripts = root / "repo" / "scripts" / "swarm"
    shutil.copytree(SWARM, scripts)
    if drop:
        (scripts / drop).unlink()
    return root / "repo"


def write_exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/zsh\n" + body)
    path.chmod(0o755)


def limit_file_size(size: int) -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))


def group_is_gone(pgid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, signal.SIGCONT)
        except ProcessLookupError:
            return True
        except PermissionError:
            continue
    return False


def install(
    repo: Path, bin_dir: Path, log: Path, fsize_limit: int | None = None
) -> subprocess.CompletedProcess[str]:
    stubs = log.parent / "stubs"
    stubs.mkdir()
    (log.parent / "brief.md").write_text("brief\n")
    write_exe(stubs / "ps", "print claude\n")
    write_exe(stubs / "wezterm", "exit 1\n")
    write_exe(stubs / "nohup", 'print -r -- "$@" > ' + str(log.parent / "nohup.args") + "\n")
    env = {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "REPO": str(repo),
        "BIN": str(bin_dir),
        "LEAD_LOG": str(log),
        "LEAD_BRIEF": str(log.parent / "brief.md"),
        "FP_LEAD": "1",
        "WEZTERM_PANE": "1",
    }
    env.pop("LEAD_AGENT", None)
    with subprocess.Popen(
        ["/bin/zsh", str(SWARM / "lead-spawn.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=(lambda: limit_file_size(fsize_limit)) if fsize_limit else None,
    ) as proc:
        out, err = proc.communicate()
        assert group_is_gone(proc.pid)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def test_install_adds_missing_sibling_module(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(SWARM / "fp-guard.py", bin_dir / "fp-guard.py")
    result = install(repo, bin_dir, tmp_path / "log")
    assert result.returncode == 0, result.stderr
    assert (bin_dir / "run_state.py").is_file()


def test_installed_guard_imports_cleanly(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(SWARM / "fp-guard.py", bin_dir / "fp-guard.py")
    assert install(repo, bin_dir, tmp_path / "log").returncode == 0
    guard = subprocess.run(
        [sys.executable, str(bin_dir / "fp-guard.py")],
        input="{}",
        capture_output=True,
        text=True,
        check=False,
        env={
            k: v for k, v in os.environ.items() if k not in {"LEAD_AGENT", "FP_LEAD", "PILOT_RUN"}
        },
    )
    code = guard.returncode
    assert code == 0


def test_missing_source_module_refuses_and_names_it(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, drop="run_state.py")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    result = install(repo, bin_dir, tmp_path / "log")
    code = result.returncode
    assert code != 0
    assert "run_state" in result.stdout + result.stderr
    assert not (tmp_path / "log").exists()


def test_existing_bin_file_is_not_overwritten(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "fp-guard.py").write_text("kept\n")
    (bin_dir / "builder-settings.json").write_text("kept\n")
    result = install(repo, bin_dir, tmp_path / "log")
    assert result.returncode == 0, result.stderr
    assert (bin_dir / "fp-guard.py").read_text() == "kept\n"
    assert (bin_dir / "builder-settings.json").read_text() == "kept\n"


def test_install_starts_no_background_process(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    result = install(repo, bin_dir, tmp_path / "log")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "nohup.args").read_text().startswith("/bin/zsh ")


def test_unwritable_bin_refuses_and_leaves_nothing(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bin_dir.chmod(0o555)
    try:
        result = install(repo, bin_dir, tmp_path / "log")
        leftovers = list(bin_dir.iterdir())
    finally:
        bin_dir.chmod(0o755)
    assert result.returncode != 0
    assert "fp-guard" in result.stderr or str(bin_dir) in result.stderr
    assert not (tmp_path / "log").exists()
    assert leftovers == []


def test_failed_copy_leaves_no_file_at_destination(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    result = install(repo, bin_dir, tmp_path / "log", fsize_limit=1024)
    assert list(bin_dir.iterdir()) == []
    assert result.returncode != 0
    assert any(path.name in result.stdout + result.stderr for path in SWARM.glob("*.py"))
    assert not (tmp_path / "log").exists()
