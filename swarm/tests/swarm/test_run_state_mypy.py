import subprocess
from pathlib import Path


def test_run_state_passes_strict_mypy() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            str(root / ".venv/bin/python"),
            "-m",
            "mypy",
            "--config-file",
            str(root / "pyproject.toml"),
            "scripts/swarm/run_state.py",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "mypy reported errors:\n" + result.stdout + result.stderr
