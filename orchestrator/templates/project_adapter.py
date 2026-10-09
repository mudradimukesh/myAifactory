"""Starter for Python + static web projects whose tests own their test services.

Extend runtime for any external services; record ownership and clean up in finally.
"""

import os
import subprocess
import sys
from contextlib import contextmanager


def provision(root, log):
    env = {**os.environ, "CI": "1"}
    with log.open("w") as stream:
        if not (root / ".venv/bin/python").exists():
            subprocess.run(
                [sys.executable, "-m", "venv", str(root / ".venv")],
                check=True,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        for command in (
            [str(root / ".venv/bin/python"), "-m", "pip", "install", "-e", ".[dev]"],
            ["npm", "ci", "--prefix", "web"],
        ):
            subprocess.run(
                command,
                cwd=root,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=True,
            )
    return env


@contextmanager
def runtime(root, art, tag, env):
    # This adapter starts no service. Tests must own all required services.
    yield env


def baseline_commands(root):
    venv = root / ".venv/bin"
    return [
        [str(venv / "python"), "-m", "pytest", "-q", "-p", "randomly"],
        [str(venv / "ruff"), "check", "."],
        [str(venv / "ruff"), "format", "--check", "."],
        [str(venv / "mypy"), "src"],
        ["npm", "--prefix", "web", "run", "typecheck"],
        ["npm", "--prefix", "web", "run", "e2e"],
    ]
