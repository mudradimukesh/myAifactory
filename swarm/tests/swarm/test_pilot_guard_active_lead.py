import importlib.util
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def active_lead(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts/swarm"))
    spec = importlib.util.spec_from_file_location(
        "pilot_guard_active_lead", root / "scripts/swarm/pilot_guard.py"
    )
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    lead = {**guard.processes()[os.getpid()], "model_session_id": "active-session"}
    scope = tmp_path / "draining"
    scope.mkdir()
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "lifecycle": "running",
                "active_lead": lead,
                "draining_leads": {
                    "old-session": {
                        "lead": {"model_session_id": "old-session"},
                        "scopes": [str(scope)],
                        "gates": [],
                    }
                },
            }
        )
    )
    file = tmp_path / "file.txt"
    file.write_text("x|y\n`literal`\n")
    return guard, path, file, scope


@pytest.mark.parametrize(
    "command",
    [
        "jq -c '{{a: .b}} | .a' {run}",
        "rg -n -e 'x|y' {file}",
        "rg -n -e '`literal`' {file}",
        'rg -n -e "x|y;z&<>" {file}',
        "rg -n -e 'first\nsecond' {file}",
        "rg -n -e '$literal' {file}",
        "rg -n -e x\\|y {file}",
    ],
    ids=[
        "jq-filter",
        "rg-alternation",
        "quoted-backtick",
        "double-quoted-operators",
        "quoted-newline",
        "quoted-dollar",
        "escaped-pipe",
    ],
)
def test_active_lead_allows_quoted_arguments(active_lead, command):
    guard, path, file, _scope = active_lead
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": command.format(run=path, file=file)},
        "session_id": "active-session",
    }
    try:
        result = guard.check(event, json.loads(path.read_text()), path, 100, 95000)
    except ValueError as error:
        pytest.fail(str(error))
    assert result


@pytest.mark.parametrize(
    "command, message",
    [
        ("rg x {file} | sh", "exact native command"),
        ("cat {scope}/file | head", "scope overlaps"),
        ("rg x {file} | head -3 && cat {scope}/file", "scope overlaps"),
        ("rg x {file} > {file}", "exact native command"),
        ("rg x {file} < {file}", "exact native command"),
        ("rg x {file} &", "exact native command"),
        ("rg x {file}\nrm {file}", "exact native command"),
        ("rg -e $(touch {file}) {file}", "exact native command"),
        ('rg -e "$(touch {file})" {file}', "exact native command"),
        ('rg -e "`touch {file}`" {file}', "exact native command"),
        ("rg -e `pwd` {file}", "exact native command"),
        ("cat {scope}/file", "scope overlaps"),
        ("rg -e 'x|y' {scope}/file", "scope overlaps"),
        (
            "/tmp/factory-fp-0925/swarm/bin/unit_gate.sh fast tag {scope}/file cand",
            "scope overlaps",
        ),
        ("touch {file}", "exact native command"),
        ("/tmp/factory-fp-0925/swarm/bin/unknown.sh {file}", "exact native command"),
        (
            "UNKNOWN=value /tmp/factory-fp-0925/swarm/bin/unit_gate.sh fast tag base cand",
            "unknown environment key",
        ),
        (
            "ART={art} PILOT_RUN={run} PILOT_BASE_REF=other "
            "/tmp/factory-fp-0925/swarm/bin/prepare_candidate.sh p1-poc-6-parse p1-poc-6-parse",
            "unknown environment key",
        ),
    ],
    ids=[
        "pipe-to-shell",
        "pipe-into-draining-path",
        "sequence-into-draining-path",
        "redirect",
        "input-redirect",
        "background",
        "newline-write",
        "substitution",
        "double-quoted-substitution",
        "double-quoted-backtick",
        "unquoted-backtick",
        "read-draining-path",
        "quoted-pattern-draining-path",
        "native-draining-path",
        "write-command",
        "unknown-native",
        "unknown-env",
        "invalid-base-ref",
    ],
)
def test_active_lead_rejects_operators_and_draining_paths(active_lead, command, message):
    guard, path, file, scope = active_lead
    event = {
        "tool_name": "Bash",
        "tool_input": {
            "command": command.format(file=file, scope=scope, art=path.parent, run=path)
        },
        "session_id": "active-session",
    }
    with pytest.raises(ValueError, match=message):
        guard.check(event, json.loads(path.read_text()), path, 100, 95000)


@pytest.mark.parametrize("base_ref", ["pre-main", "main"])
def test_active_lead_allows_prepare_candidate_base_ref(active_lead, base_ref):
    guard, path, _file, _scope = active_lead
    event = {
        "tool_name": "Bash",
        "tool_input": {
            "command": f"ART={path.parent} PILOT_RUN={path} PILOT_BASE_REF={base_ref} "
            "/tmp/factory-fp-0925/swarm/bin/prepare_candidate.sh p1-poc-6-parse p1-poc-6-parse"
        },
        "session_id": "active-session",
    }
    assert guard.check(event, json.loads(path.read_text()), path, 100, 95000)


@pytest.mark.parametrize(
    "command", ["git status", "git diff", "git log --oneline -3"], ids=["status", "diff", "log"]
)
def test_active_lead_allows_plain_git_reads(active_lead, command):
    guard, path, _file, _scope = active_lead
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "active-session",
    }
    try:
        result = guard.check(event, json.loads(path.read_text()), path, 100, 95000)
    except ValueError as error:
        pytest.fail(str(error))
    assert result


@pytest.mark.parametrize("command", ["cat {file}", "rg -n -e 'x|y' {file}"], ids=["cat", "rg"])
@pytest.mark.parametrize(
    "dispatch_frozen, used", [(True, 100), (False, 95001)], ids=["frozen", "over-cap"]
)
def test_active_lead_allows_reads_under_dispatch_limits(
    active_lead, command, dispatch_frozen, used
):
    guard, path, file, _scope = active_lead
    state = json.loads(path.read_text())
    state["dispatch_frozen"] = dispatch_frozen
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": command.format(file=file)},
        "session_id": "active-session",
    }
    try:
        result = guard.check(event, state, path, used, 95000)
    except ValueError as error:
        pytest.fail(str(error))
    assert result


@pytest.mark.parametrize(
    "command",
    [
        "rg -e $'x|y' {file}",
        "cat <<< 'x|y'",
        "cat <(cat {file})",
        "cat {file}\\\n; rm {file}",
    ],
    ids=["ansi-c-quote", "here-string", "process-substitution", "backslash-newline-second-command"],
)
def test_active_lead_rejects_shell_expansions(active_lead, command):
    guard, path, file, _scope = active_lead
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": command.format(file=file)},
        "session_id": "active-session",
    }
    with pytest.raises(ValueError, match="exact native command"):
        guard.check(event, json.loads(path.read_text()), path, 100, 95000)


@pytest.mark.parametrize(
    "command",
    [
        "rg x {file} | cat",
        "rg x {file}; cat {file}",
        "rg x {file} && cat {file}",
        "rg x {file}\ncat {file}",
        "cat {file}\\\n; cat {file}",
    ],
    ids=["pipe", "semicolon", "and", "newline", "backslash-newline"],
)
def test_active_lead_batches_read_only_stages(active_lead, command):
    # Read-only pipelines and sequences pass so leads can batch reads (user fix 3,
    # 2026-10-20); each stage is still checked for draining paths and writes.
    guard, path, file, _scope = active_lead
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": command.format(file=file)},
        "session_id": "active-session",
    }
    assert guard.check(event, json.loads(path.read_text()), path, 100, 95000)
