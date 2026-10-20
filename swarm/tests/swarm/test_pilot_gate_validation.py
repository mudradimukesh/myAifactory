import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import IO

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def gate_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    module = importlib.import_module("unit_gate")
    monkeypatch.setattr(module, "changed", lambda root, base, candidate: [])
    (tmp_path / "sample-acceptance.md").write_text(
        '```json\n{"journey_specs": ["journey.spec.ts"]}\n```\n'
    )
    (tmp_path / "sample-test-map.json").write_text("[]")
    return module


@pytest.fixture
def checks():
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "review_check", ROOT / "scripts/swarm/review_check.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.pop(0)


def packet():
    candidate = {
        "candidate_sha": "b" * 40,
        "main_sha": "a" * 40,
        "expected_review_modes": ["code"],
        "feature": "fixture",
    }
    gate = {
        "candidate_sha": "b" * 40,
        "fingerprint": {"python": "fixture"},
        "gates": [
            {
                "id": f"G{i}",
                "disposition": "retained",
                "exit_code": 0,
                "measured": 0,
                "threshold": 0,
                "comparison": "max",
            }
            for i in range(1, 19)
        ],
    }
    names = [
        "frozen_tests_unchanged",
        "no_fixture_literals",
        "no_special_case_branch",
        "no_skip_or_xfail",
        "no_operator_or_equality_override",
        "no_broad_except_or_success_fallback",
        "no_type_escape",
        "no_comments",
        "deterministic_tests",
    ]
    review = {
        "schema_version": "p1-review-2",
        "mode": "code",
        "unit": "fixture",
        "base_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "statuses": [],
        "acceptance": [
            {
                "id": "A1",
                "test_node": "test_node",
                "contract_ref": "acceptance:1",
                "coverage": "asserted",
                "oracle": "spec_literal",
                "evidence_kind": "observed",
                "evidence": "runner result",
            }
        ],
        "checks": [{"check": n, "result": "pass", "evidence": "observed"} for n in names],
        "mutants": [],
        "findings": [],
        "needs_ruling": None,
        "merge": True,
    }
    return candidate, gate, review


def test_complete_packet_passes_and_duplicates_fail(checks):
    candidate, gate, review = packet()
    checks.validate(
        candidate,
        gate,
        ["A1"],
        {"product_ids": []},
        [review],
        [],
        {"survivors": []},
        {"python": "fixture"},
    )
    gate["gates"].append(gate["gates"][0])
    with pytest.raises(ValueError):
        checks.validate(
            candidate,
            gate,
            ["A1"],
            {"product_ids": []},
            [review],
            [],
            {"survivors": []},
            {"python": "fixture"},
        )


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_sha",
        "missing_id",
        "weak_oracle",
        "major",
        "missing_mutant",
        "duplicate_check",
        "unfixed",
        "fingerprint",
        "missing_product",
    ],
)
def test_review_defects_fail_closed(checks, defect):
    candidate, gate, review = packet()
    acceptance = {"product_ids": []}
    findings = []
    survivors = {"survivors": []}
    fingerprint = {"python": "fixture"}
    if defect == "wrong_sha":
        review["candidate_sha"] = "c" * 40
    elif defect == "missing_id":
        review["acceptance"] = []
    elif defect == "weak_oracle":
        review["acceptance"][0]["oracle"] = "from_code_under_test"
    elif defect == "major":
        review["merge"] = False
    elif defect == "missing_mutant":
        survivors["survivors"] = [{"id": "m1", "contract_relevant": True}]
    elif defect == "duplicate_check":
        review["checks"].append(review["checks"][0])
    elif defect == "unfixed":
        findings = [{"id": "F1", "mode": "code", "severity": "major", "text": "broken"}]
    elif defect == "fingerprint":
        fingerprint = {"python": "changed"}
    else:
        acceptance["product_ids"] = ["A1"]
    with pytest.raises(ValueError):
        checks.validate(
            candidate, gate, ["A1"], acceptance, [review], findings, survivors, fingerprint
        )


def test_gate_cannot_authorize_its_own_mutation_waiver(checks):
    candidate, gate, review = packet()
    gate["u8_approved"] = True
    gate["gates"][12].update(disposition="waived", approval="U8")
    with pytest.raises(ValueError, match="Unapproved"):
        checks.validate(
            candidate,
            gate,
            ["A1"],
            {"product_ids": []},
            [review],
            [],
            {"survivors": []},
            {"python": "fixture"},
        )


def test_packet_hashes_reject_changed_or_missing_required_artifact(checks, tmp_path):
    import hashlib
    import json

    files = [
        "acceptance.md",
        "ids.txt",
        "test-map.json",
        "s1-tests.json",
        "s1-tests.sha256",
        "red.json",
        "survivors.json",
        "screenshots.json",
    ]
    hashes = {}
    for name in files:
        path = tmp_path / ("sample-" + name)
        path.write_text(json.dumps({"artifacts": []}) if name == "screenshots.json" else "fixture")
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    gate = {"artifact_hashes": hashes}
    spec = {"required_artifacts": ["screenshots.json"]}
    checks.packet_artifacts(tmp_path, "sample", gate, spec)
    (tmp_path / "sample-screenshots.json").write_text("changed")
    with pytest.raises(ValueError, match="hash"):
        checks.packet_artifacts(tmp_path, "sample", gate, spec)
    del gate["artifact_hashes"]["sample-screenshots.json"]
    with pytest.raises(ValueError, match="hash"):
        checks.packet_artifacts(tmp_path, "sample", gate, spec)


def test_archived_findings_cannot_disappear_or_change_severity(checks, tmp_path):
    import json

    archive = tmp_path / "attempts/feature/1"
    archive.mkdir(parents=True)
    finding = {"id": "F1", "severity": "major", "text": "Sign-out keeps the credential"}
    (archive / "feature-code-review.json").write_text(
        json.dumps({"mode": "code", "statuses": [], "findings": [finding]})
    )
    candidate = {"feature": "feature", "attempt": 2}
    with pytest.raises(ValueError):
        checks.finding_history(tmp_path, "feature", candidate, [])
    with pytest.raises(ValueError):
        checks.finding_history(
            tmp_path, "feature", candidate, [{**finding, "mode": "code", "severity": "minor"}]
        )
    checks.finding_history(tmp_path, "feature", candidate, [{**finding, "mode": "code"}])


@pytest.mark.parametrize("fixed", [None, "attempt-1:CR-1", "CR-1"])
def test_reused_finding_ids_keep_each_attempt(checks, tmp_path, fixed):
    import json

    findings = []
    for attempt in (1, 2):
        folder = tmp_path / "attempts/feature" / str(attempt)
        folder.mkdir(parents=True)
        finding = {"id": "CR-1", "severity": "major", "text": f"Defect {attempt}"}
        (folder / "feature-code-review.json").write_text(
            json.dumps({"statuses": [], "findings": [finding]})
        )
        findings.append({**finding, "mode": "code", "id": f"attempt-{attempt}:CR-1"})
    candidate = {"feature": "feature", "attempt": 3}
    if fixed is not None:
        folder = tmp_path / "attempts/feature/3"
        folder.mkdir()
        (folder / "feature-code-review.json").write_text(
            json.dumps({"statuses": [{"id": fixed, "status": "fixed"}], "findings": []})
        )
        candidate["attempt"] = 4
        if fixed == "CR-1":
            with pytest.raises(ValueError, match="Ambiguous"):
                checks.finding_history(tmp_path, "feature", candidate, findings)
            return
        findings = findings[1:]
    checks.finding_history(tmp_path, "feature", candidate, findings)
    with pytest.raises(ValueError, match="Archived"):
        checks.finding_history(tmp_path, "feature", candidate, findings[1:])
    with pytest.raises(ValueError, match="Archived"):
        checks.finding_history(
            tmp_path, "feature", candidate, [{**f, "text": "Changed"} for f in findings]
        )


@pytest.mark.parametrize(
    ("extra", "passes"),
    [
        (None, True),
        ("FAILED tests/swarm/test_other.py::test_x - AssertionError: boom", False),
        ("present", False),
        ("outside", False),
    ],
)
def test_g10_accepts_only_lost_p2_art_failures(checks, tmp_path, monkeypatch, extra, passes):
    monkeypatch.setattr(checks, "P2_ART", f"{tmp_path}/ART/")
    candidate, gate, _ = packet()
    missing = checks.LOST_P2_TEST + "test_{} - AssertionError: Required file is missing: {}"
    lines = [missing.format(n, tmp_path / "ART/run/environment.json") for n in "ab"]
    if extra == "present":
        (tmp_path / "ART").mkdir()
        (tmp_path / "ART/present.json").write_text("{}")
        lines.append(missing.format("c", tmp_path / "ART/present.json"))
    elif extra == "outside":
        lines.append(missing.format("c", tmp_path / "other/environment.json"))
    elif extra:
        lines.append(extra)
    log = tmp_path / "G10.log"
    log.write_text("\n".join([*lines, f"{len(lines)} failed, 854 passed in 1.00s"]))
    gate["gates"][9].update(exit_code=1, measured=1, evidence=str(log))
    if passes:
        checks.gates_valid(gate, candidate, {}, {"python": "fixture"})
    else:
        with pytest.raises(ValueError, match="G10 failed"):
            checks.gates_valid(gate, candidate, {}, {"python": "fixture"})


def test_fast_packet_hashes_fast_survivors(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        from gate_evidence import artifact_hashes
    finally:
        sys.path.pop(0)
    names = ["acceptance.md", "ids.txt", "test-map.json", "s1-tests.json", "s1-tests.sha256"]
    for name in [*names, "red.json", "fast-survivors.json"]:
        (tmp_path / ("sample-" + name)).write_text("fixture")
    hashes = artifact_hashes(tmp_path, "sample", {}, fast=True)
    assert "sample-fast-survivors.json" in hashes
    with pytest.raises(OSError):
        artifact_hashes(tmp_path, "sample", {})


@pytest.mark.parametrize("report_kind", ["missing", "empty", "error", "passed", "failed"])
def test_g10_runner_report_validation(
    gate_module: ModuleType, tmp_path: Path, report_kind: str
) -> None:
    gate = gate_module.Gate(tmp_path, tmp_path, "sample", "base", "candidate")
    cases = {
        "empty": "<testsuites/>",
        "error": '<testsuites><testcase classname="tests.runner" name="test_setup">'
        '<error message="fixture missing"/></testcase></testsuites>',
        "passed": '<testsuites><testcase classname="tests.runner" name="test_ok"/></testsuites>',
        "failed": '<testsuites><testcase classname="tests.runner" name="test_bad">'
        '<failure message="assertion failed"/></testcase>'
        '<testcase classname="tests.runner" name="test_bad"><failure/></testcase>'
        '<testcase classname="tests.runner" name="test_ok"/></testsuites>',
    }
    content = cases.get(report_kind)
    runner = (
        "import pathlib, sys; "
        "report = pathlib.Path(sys.argv[1].removeprefix('--junitxml=')); "
        f"content = {content!r}; "
        "report.write_text(content) if content is not None else None; "
        f"sys.exit({int(report_kind == 'failed')})"
    )
    command = [sys.executable, "-c", runner]
    if report_kind in {"missing", "empty", "error"}:
        message = "G10 test report missing" if report_kind == "missing" else "infrastructure errors"
        with pytest.raises(ValueError, match=message):
            gate.commands("G10", [command])
        return
    result = gate.commands("G10", [command])
    assert result["command"] == [[*command, "--junitxml=" + str(gate.file("G10-tests.xml"))]]
    assert result["exit_code"] == int(report_kind == "failed")
    assert result["measured"] == int(report_kind == "failed")
    assert result["failed_test_ids"] == (
        ["tests/runner.py::test_bad"] if report_kind == "failed" else []
    )
    assert result["paths"] == ["src/", "tests/", "scripts/swarm/"]


@pytest.mark.parametrize("report_kind", ["missing", "empty", "error", "passed", "nonzero"])
def test_g17_runner_report_validation(
    gate_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_kind: str,
) -> None:
    gate = gate_module.Gate(tmp_path, tmp_path, "sample", "base", "candidate", fast=True)
    gate.mapping = [
        {
            "id": "J1",
            "runner": "playwright",
            "test_node": "journey.spec.ts::journey",
            "projects": ["chromium"],
            "oracle": "spec_literal",
        }
    ]
    document: dict[str, object] = {
        "suites": [
            {
                "specs": [
                    {
                        "file": "journey.spec.ts",
                        "title": "journey",
                        "tests": [
                            {
                                "projectName": "chromium",
                                "results": [
                                    {"status": "failed" if report_kind == "nonzero" else "passed"}
                                ],
                            }
                        ],
                    }
                ]
            }
        ]
    }
    if report_kind == "empty":
        document = {"suites": []}
    elif report_kind == "error":
        document = {"errors": [{"message": "browser launch failed"}]}
    real_run = subprocess.run

    def run(
        command: list[str], *, cwd: Path, env: dict[str, str], stdout: IO[str], stderr: int
    ) -> subprocess.CompletedProcess[bytes]:
        assert command == [
            "npm",
            "--prefix",
            "web",
            "run",
            "e2e",
            "--",
            "journey.spec.ts",
            "--retries=0",
            "--repeat-each=1",
            "--workers=1",
            "--reporter=json",
        ]
        report = env["PLAYWRIGHT_JSON_OUTPUT_NAME"]
        runner = (
            "import pathlib, sys; "
            + (
                f"pathlib.Path({report!r}).write_text({json.dumps(document)!r}); "
                if report_kind != "missing"
                else ""
            )
            + f"sys.exit({int(report_kind == 'nonzero')})"
        )
        return real_run(
            [sys.executable, "-c", runner], cwd=cwd, env=env, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(gate_module.subprocess, "run", run)
    if report_kind == "missing":
        with pytest.raises(FileNotFoundError):
            gate.journey()
    elif report_kind in {"empty", "error"}:
        message = (
            "G17 runner report is empty" if report_kind == "empty" else "infrastructure errors"
        )
        with pytest.raises(ValueError, match=message):
            gate.journey()
    else:
        result = gate.journey()
        assert result["exit_code"] == int(report_kind == "nonzero")
        assert result["failed_test_ids"] == (
            ["journey.spec.ts::journey"] if report_kind == "nonzero" else []
        )
        assert result["paths"] == ["web/", "src/", "scripts/swarm/", "tests/e2e/"]


def test_gate_execute_stops_after_cheap_gate_error(
    gate_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = gate_module.Gate(tmp_path, tmp_path, "sample", "base", "candidate")

    def unexpected_command(*args: object, **kwargs: object) -> None:
        pytest.fail("Gate.execute ran a command after G1 failed")

    monkeypatch.setattr(gate_module.subprocess, "run", unexpected_command)
    rows = gate.execute()
    assert [row["id"] for row in rows] == [f"G{i}" for i in range(1, 19)]
    assert rows[0]["exit_code"] == 1
    assert "sample-s1-tests.json" in rows[0]["error"]
    assert all(row["exit_code"] == 1 for row in rows[1:])
    assert all(row["evidence"] == "skipped: prior failure" for row in rows[1:])
    assert json.loads(gate.file("survivors.json").read_text()) == {"survivors": []}


@pytest.mark.parametrize("unrelated_failure", [False, True])
def test_g10_lost_p2_exception_from_runner_evidence(
    checks: ModuleType,
    gate_module: ModuleType,
    tmp_path: Path,
    unrelated_failure: bool,
) -> None:
    runner_gate = gate_module.Gate(tmp_path, tmp_path, "sample", "base", "candidate")
    missing = Path(checks.P2_ART) / tmp_path.name / "environment.json"
    assert not missing.exists()
    node = "tests/phase2/test_environment.py::test_required_file"
    summary = [f"FAILED {node} - AssertionError: Required file is missing: {missing}"]
    xml = (
        '<testsuites><testcase classname="tests.phase2.test_environment" '
        'name="test_required_file"><failure message="Required file is missing"/></testcase>'
    )
    if unrelated_failure:
        summary.append("FAILED tests/other.py::test_other - AssertionError: unrelated")
        xml += '<testcase classname="tests.other" name="test_other"><failure/></testcase>'
    xml += "</testsuites>"
    summary.append(f"{2 if unrelated_failure else 1} failed, 1 passed in 0.01s")
    runner = (
        "import pathlib, sys; "
        f"pathlib.Path(sys.argv[1].removeprefix('--junitxml=')).write_text({xml!r}); "
        f"print({chr(10).join(summary)!r}); sys.exit(1)"
    )
    row = runner_gate.commands("G10", [[sys.executable, "-c", runner]])
    assert row["exit_code"] == 1
    assert node in row["failed_test_ids"]
    rows: list[dict[str, object]] = [
        {
            "id": f"G{i}",
            "disposition": "retained",
            "exit_code": 0,
            "measured": 0,
            "threshold": 0,
        }
        for i in range(1, 19)
    ]
    gate = {"candidate_sha": "candidate", "fingerprint": {}, "gates": rows}
    rows[9].update(row)
    if unrelated_failure:
        with pytest.raises(ValueError, match="G10 failed"):
            checks.gates_valid(gate, {"candidate_sha": "candidate"}, {}, {})
    else:
        checks.gates_valid(gate, {"candidate_sha": "candidate"}, {}, {})
