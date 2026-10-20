import importlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def coverage_source_contains(source: str, changed_file: Path) -> bool:
    path = ROOT / source
    if path.is_dir():
        return changed_file.resolve().is_relative_to(path.resolve())
    try:
        spec = importlib.util.find_spec(source)
    except (ImportError, ValueError):
        return False
    if spec is None:
        return False
    if spec.submodule_search_locations:
        return any(
            changed_file.resolve().is_relative_to(Path(location).resolve())
            for location in spec.submodule_search_locations
        )
    return spec.origin is not None and changed_file.resolve() == Path(spec.origin).resolve()


@pytest.mark.parametrize("identifier", ["G5", "G6", "G12"])
def test_swarm_only_change_gets_python_gate_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identifier: str
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    module = importlib.import_module("unit_gate")
    changed_file = "scripts/swarm/gate_evidence.py"
    (tmp_path / "probe-acceptance.md").write_text("```json\n{}\n```\n")
    (tmp_path / "probe-test-map.json").write_text(json.dumps([]))
    monkeypatch.setattr(module, "changed", lambda *args: [changed_file])
    gate = module.Gate(tmp_path, tmp_path, "probe", "base", "candidate")
    monkeypatch.setattr(gate, "commands", lambda name, commands, cwd=".": {"command": commands})
    monkeypatch.setattr(
        module,
        "run_all",
        lambda checks, workers=1: dict(checks)[identifier](),
    )
    commands = gate.execute()["command"]
    assert commands, f"{identifier} must emit Python commands for {changed_file}"
    if identifier == "G12":
        assert (
            any(
                "--cov-report=xml:" + str(gate.file("cov.xml")) in cmd
                and any(
                    coverage_source_contains(arg.removeprefix("--cov="), ROOT / changed_file)
                    for arg in cmd
                    if arg.startswith("--cov=")
                )
                for cmd in commands
            )
            and [
                str(gate.root / ".venv/bin/diff-cover"),
                str(gate.file("cov.xml")),
                "--compare-branch=" + gate.base,
                "--fail-under=90",
            ]
            in commands
        )
    else:
        assert any(changed_file in command for command in commands)


def test_python_production_excludes_tests_and_preserves_web_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    module = importlib.import_module("gate_evidence")
    files = [
        "src/package/module.py",
        "scripts/swarm/module.py",
        "scripts/swarm/nested/module.py",
        "tests/swarm/test_module.py",
        "scripts/swarm/test_module.py",
        "scripts/swarm/module_test.py",
        "scripts/swarm/tests/module.py",
        "src/package/tests/module.py",
        "src/package/test_module.py",
        "src/package/module_test.py",
        "scripts/other/module.py",
        "scripts/swarm/module.txt",
        "web/src/module.ts",
        "web/src/component.tsx",
        "web/tests/module.ts",
    ]
    assert module.production(files, "python") == files[:3]
    assert module.production(files, "web") == ["web/src/module.ts", "web/src/component.tsx"]


def test_web_production_excludes_test_and_spec_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    module = importlib.import_module("gate_evidence")
    production_files = [
        "web/src/module.ts",
        "web/src/component.tsx",
        "web/src/auth/SignInPage.tsx",
        "web/src/nested/test.ts",
        "web/src/nested/spec.tsx",
        "web/src/module.test.helper.ts",
        "web/src/component.spec.helper.tsx",
    ]
    files = [
        "web/src/module.test.ts",
        "web/src/component.test.tsx",
        "web/src/module.spec.ts",
        "web/src/component.spec.tsx",
        "web/src/auth/SignInPage.test.tsx",
        *production_files,
        "web/tests/module.ts",
        "web/src/module.js",
        "scripts/module.ts",
    ]
    assert module.production(files, "web") == production_files, (
        "web selection must exclude test and spec files and keep production files"
    )
