import importlib.util
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_pilot_lead_launch_forces_bin_cap_fraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    spec = importlib.util.spec_from_file_location(
        "pilot_lead", ROOT / "scripts/swarm/pilot_lead.py"
    )
    assert spec is not None and spec.loader is not None
    lead = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lead)
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "lifecycle": "preparing",
                "consumed": {"overall": 0, "preparation": 0},
                "ceilings": {"overall": 1, "preparation": 1},
            }
        )
    )
    brief = tmp_path / "brief.md"
    brief.write_text("Launch fixture lead.")
    claude = tmp_path / "claude"
    claude.write_text('#!/bin/zsh\nprint -r -- "$FP_CAP_FRACTION"\n')
    claude.chmod(0o755)
    received: list[str] = []
    session = ""

    def spawn(command: list[str], *, text: bool) -> str:
        nonlocal session
        argv = shlex.split(command[-1].removeprefix("exec "))
        session = argv[argv.index("--session-id") + 1]
        result = subprocess.run(
            command[-3:],
            env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
            capture_output=True,
            text=text,
            check=True,
        )
        received.append(result.stdout.strip())
        return "fixture-pane"

    monkeypatch.setattr(lead.subprocess, "check_output", spawn)
    monkeypatch.setattr(
        lead,
        "processes",
        lambda: {123: {"pid": 123, "command": "claude --session-id " + session}},
    )
    lead.launch(
        path,
        {
            "REPO": str(tmp_path),
            "BIN": str(tmp_path),
            "LEAD_BRIEF": str(brief),
            "FP_CAP_FRACTION": "0.50",
        },
        "launch-token",
    )
    assert received == ["0.753012048192771"]


def launch_fixture(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts/swarm"
    scripts.mkdir(parents=True)
    for name in ("interior-lead.sh", "lead-spawn.sh"):
        shutil.copyfile(ROOT / "scripts/swarm" / name, scripts / name)
    return repo, scripts


def test_dry_run_preserves_settings_without_writes(tmp_path):
    repo, scripts = launch_fixture(tmp_path)
    target = tmp_path / "undeployed"
    env = dict(
        os.environ,
        REPO=str(repo),
        BIN=str(target),
        LEAD_BRIEF="/custom/brief.md",
        LEAD_AGENT="p1-pilot-lead",
        FP_CAP_FRACTION="0.50",
        BUILDER_LOGS=str(tmp_path / "logs"),
    )
    before = set(tmp_path.rglob("*"))
    result = subprocess.run(
        ["/bin/zsh", str(scripts / "interior-lead.sh"), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "/custom/brief.md" in result.stdout
    assert "--agent p1-pilot-lead" in result.stdout
    assert "FP_CAP_FRACTION=0.50" in result.stdout
    assert str(repo) in result.stdout
    assert set(tmp_path.rglob("*")) == before


def test_default_values_survive_launch_path(tmp_path):
    repo, scripts = launch_fixture(tmp_path)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"LEAD_BRIEF", "LEAD_AGENT", "FP_CAP_FRACTION", "FP_CONTEXT_CAP"}
    }
    env.update(REPO=str(repo), BIN=str(tmp_path / "bin"), BUILDER_LOGS=str(tmp_path / "logs"))
    result = subprocess.run(
        ["/bin/zsh", str(scripts / "interior-lead.sh"), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert str(repo / "prompts/phase-1/lead-brief.md") in result.stdout
    assert "--agent p1-lead" in result.stdout
    assert "FP_CAP_FRACTION=0.90" in result.stdout


def test_deployment_replaces_inodes_and_running_chain_finishes(tmp_path):
    repo, scripts = launch_fixture(tmp_path)
    deployed = tmp_path / "bin"
    deployed.mkdir()
    chain = deployed / "chain.sh"
    chain.write_text("#!/bin/zsh\nread -r answer\nprint old-chain\n")
    (scripts / "chain.sh").write_text("#!/bin/zsh\nprint new-chain\n")
    (scripts / "lead-spawn.sh").write_text("#!/bin/zsh\nprint stub-lead\n")
    (scripts / "__pycache__").mkdir()
    old_inode = chain.stat().st_ino
    running = subprocess.Popen(
        ["/bin/zsh", str(chain)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    env = dict(os.environ, REPO=str(repo), BIN=str(deployed), BUILDER_LOGS=str(tmp_path / "logs"))
    try:
        result = subprocess.run(
            ["/bin/zsh", str(scripts / "interior-lead.sh"), "--start"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert chain.stat().st_ino != old_inode
        output, errors = running.communicate("continue\n", timeout=5)
        assert running.returncode == 0, errors
        assert output.strip() == "old-chain"
        assert chain.read_text().endswith("print new-chain\n")
    finally:
        if running.poll() is None:
            running.terminate()
            running.wait(timeout=5)
