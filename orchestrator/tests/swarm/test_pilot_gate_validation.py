import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


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
        "checks": [
            {"check": n, "result": "pass", "evidence": "observed"} for n in names
        ],
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
            candidate,
            gate,
            ["A1"],
            acceptance,
            [review],
            findings,
            survivors,
            fingerprint,
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
        path.write_text(
            json.dumps({"artifacts": []}) if name == "screenshots.json" else "fixture"
        )
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
            tmp_path,
            "feature",
            candidate,
            [{**finding, "mode": "code", "severity": "minor"}],
        )
    checks.finding_history(
        tmp_path, "feature", candidate, [{**finding, "mode": "code"}]
    )


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
