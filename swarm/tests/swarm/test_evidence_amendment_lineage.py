import hashlib
import importlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from test_evidence_write import Fixture, git

if TYPE_CHECKING:
    from unit_gate import Gate


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    monkeypatch.delenv("REPO", raising=False)
    fixture = Fixture(tmp_path)
    (fixture.art / "u-test-map.json").write_text("[]")
    fixture.red(fixture.tests)
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode == 0, result.stderr
    root_inventory = (fixture.art / "u-s1-tests.sha256").read_bytes()
    root = {
        "predecessor": fixture.predecessor,
        "tests_commit": fixture.tests,
        "red_receipt": "u-red.json",
        "red_sha256": digest(fixture.art / "u-red.json"),
        "test_map_sha256": digest(fixture.art / "u-test-map.json"),
        "inventory_sha256": hashlib.sha256(root_inventory).hexdigest(),
    }
    before = digest(fixture.repo / "tests/test_unit.py")
    amended = fixture.commit({"tests/test_unit.py": "def test_unit() -> None:\n    assert False\n"})
    preservation = {
        "passed": True,
        "predecessor": fixture.tests,
        "tests_commit": amended,
        "test_map_sha256": digest(fixture.art / "u-test-map.json"),
    }
    (fixture.art / "u-preservation.json").write_text(json.dumps(preservation))
    equivalence = {"assertions_before": ["assert False"], "assertions_after": ["assert False"]}
    (fixture.art / "u-equivalence.json").write_text(json.dumps(equivalence))
    amendment = {
        "parent_inventory_sha256": root["inventory_sha256"],
        "predecessor": fixture.tests,
        "tests_commit": amended,
        "paths": {
            "tests/test_unit.py": {
                "before_sha256": before,
                "after_sha256": digest(fixture.repo / "tests/test_unit.py"),
            }
        },
        "source_dependencies": [fixture.predecessor],
        "preservation_receipt": "u-preservation.json",
        "preservation_sha256": digest(fixture.art / "u-preservation.json"),
        "assertion_equivalence": "u-equivalence.json",
        "assertion_equivalence_sha256": digest(fixture.art / "u-equivalence.json"),
    }
    (fixture.art / "u-approval.json").write_text(
        json.dumps(
            {
                "owner": "u",
                "amendment_sha256": hashlib.sha256(
                    json.dumps(amendment, sort_keys=True).encode()
                ).hexdigest(),
            }
        )
    )
    amendment["owner_approval"] = "u-approval.json"
    amendment["owner_approval_sha256"] = digest(fixture.art / "u-approval.json")
    record = json.loads((fixture.art / "u-s1-tests.json").read_text())
    record["lineage"] = {"version": 1, "root": root, "amendments": [amendment]}
    (fixture.art / "u-s1-tests.json").write_text(json.dumps(record))
    return fixture


def correction(fixture: Fixture, approval: list[str] | None = None) -> tuple[int, str]:
    if approval is None:
        approval = ["--approval-sha256", digest(fixture.art / "u-approval.json")]
    result = fixture.run(
        "s1",
        "u",
        git(fixture.repo, "rev-parse", "HEAD"),
        fixture.predecessor,
        "--red",
        "u-red.json",
        "--correction",
        "Approved assertion-preserving amendment",
        *approval,
    )
    return result.returncode, result.stderr


def test_approved_amendment_retains_original_red_and_archives(lineage: Fixture) -> None:
    old = lineage.snapshot()
    code, error = correction(lineage)
    assert code == 0, f"approved amendment rejected: {error}"
    assert (lineage.art / "u-red.json").read_bytes() == old["u-red.json"]
    record = json.loads((lineage.art / "u-s1-tests.json").read_text())
    assert record["tests_commit"] == git(lineage.repo, "rev-parse", "HEAD")
    assert record["lineage"] == json.loads(old["u-s1-tests.json"])["lineage"]
    for suffix in ("json", "sha256"):
        name = f"u-s1-tests.{suffix}"
        backup = lineage.art / "logs" / f"{name}.{lineage.tests[:7]}.bak"
        assert backup.read_bytes() == old[name]
    assert (lineage.art / "u-s1-tests.sha256").read_text() == (
        f"{digest(lineage.repo / 'tests/test_unit.py')}  tests/test_unit.py\n"
    )


def test_correction_accepts_matching_approval_digest(lineage: Fixture) -> None:
    code, error = correction(
        lineage, ["--approval-sha256", digest(lineage.art / "u-approval.json")]
    )
    assert code == 0, f"matching approval digest rejected: {error}"


@pytest.mark.parametrize("approval", [[], ["--approval-sha256", "0" * 64]])
def test_correction_refuses_unbound_approval_digest(lineage: Fixture, approval: list[str]) -> None:
    old = lineage.snapshot()
    code, error = correction(lineage, approval)
    assert code != 0, "writer accepted an approval the lead did not bind"
    assert "Amendment lineage: unapproved amendment" in error, error
    assert lineage.snapshot() == old


@pytest.mark.parametrize(
    ("target", "field", "value", "reason"),
    [
        ("root", "red_sha256", "0" * 64, "invalid root"),
        ("amendment", "parent_inventory_sha256", "0" * 64, "wrong parent inventory"),
        ("amendment", "owner_approval", "missing.json", "unapproved amendment"),
        ("amendment", "preservation_sha256", "0" * 64, "preservation digest differs"),
        ("amendment", "assertion_equivalence_sha256", "0" * 64, "assertion proof differs"),
    ],
)
def test_correction_rejects_invalid_relation(
    lineage: Fixture, target: str, field: str, value: str, reason: str
) -> None:
    path = lineage.art / "u-s1-tests.json"
    record = json.loads(path.read_text())
    relation = record["lineage"]["root"] if target == "root" else record["lineage"]["amendments"][0]
    relation[field] = value
    path.write_text(json.dumps(record))
    old = lineage.snapshot()
    code, error = correction(lineage)
    assert code != 0, f"writer accepted {reason}"
    assert f"Amendment lineage: {reason}" in error, error
    assert lineage.snapshot() == old


def test_correction_rejects_mismatched_preservation_tuple(lineage: Fixture) -> None:
    path = lineage.art / "u-preservation.json"
    receipt = json.loads(path.read_text())
    receipt["tests_commit"] = lineage.tests
    path.write_text(json.dumps(receipt))
    record_path = lineage.art / "u-s1-tests.json"
    record = json.loads(record_path.read_text())
    record["lineage"]["amendments"][0]["preservation_sha256"] = digest(path)
    record_path.write_text(json.dumps(record))
    old = lineage.snapshot()
    code, error = correction(lineage)
    assert code != 0, "writer accepted mismatched preservation tuple"
    assert "Amendment lineage: preservation tuple differs" in error, error
    assert lineage.snapshot() == old


def test_correction_rejects_altered_assertions(lineage: Fixture) -> None:
    lineage.commit({"tests/test_unit.py": "def test_unit() -> None:\n    assert True\n"})
    old = lineage.snapshot()
    code, error = correction(lineage)
    assert code != 0, "writer accepted altered assertions"
    assert "Amendment lineage: final inventory differs" in error, error
    assert lineage.snapshot() == old


def test_correction_rejects_missing_cross_owner_oracle(lineage: Fixture) -> None:
    lineage.commit({"tests/test_oracle.py": "def test_oracle() -> None:\n    assert 1 == 1\n"})
    old = lineage.snapshot()
    code, error = correction(lineage)
    assert code != 0, "writer accepted an unlisted cross-owner oracle"
    assert "Amendment lineage: unlisted test path" in error, error
    assert lineage.snapshot() == old


def test_legacy_exact_tuple_still_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPO", raising=False)
    fixture = Fixture(tmp_path)
    red = (fixture.art / "u-red.json").read_bytes()
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode == 0, result.stderr
    record = json.loads((fixture.art / "u-s1-tests.json").read_text())
    assert record["tests_commit"] == fixture.tests
    assert record["predecessor"] == fixture.predecessor
    assert "lineage" not in record
    assert (fixture.art / "u-red.json").read_bytes() == red


def test_g1_rejects_unverified_amendment_inventory(
    lineage: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/swarm"))
    module = importlib.import_module("unit_gate")
    path = lineage.art / "u-s1-tests.json"
    record = json.loads(path.read_text())
    record["tests_commit"] = git(lineage.repo, "rev-parse", "HEAD")
    record["lineage"]["amendments"][0]["parent_inventory_sha256"] = "0" * 64
    path.write_text(json.dumps(record))
    (lineage.art / "u-s1-tests.sha256").write_text(
        f"{digest(lineage.repo / 'tests/test_unit.py')}  tests/test_unit.py\n"
    )
    (lineage.art / "u-test-map.json").write_text("[]")
    (lineage.art / "u-acceptance.md").write_text("```json\n{}\n```\n")
    gate = module.Gate(lineage.repo, lineage.art, "u", lineage.predecessor, record["tests_commit"])
    error = ""
    try:
        gate.frozen()
    except ValueError as failure:
        error = str(failure)
    assert "Amendment lineage: wrong parent inventory" in error, (
        "G1 accepted an unverified amendment inventory: " + error
    )


def test_g2_rejects_root_predecessor_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPO", raising=False)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/swarm"))
    module = importlib.import_module("unit_gate")
    fixture = Fixture(tmp_path)
    (fixture.art / "u-test-map.json").write_text("[]")
    fixture.red(fixture.tests)
    (fixture.art / "u-acceptance.md").write_text("```json\n{}\n```\n")
    result = fixture.s1("--red", "u-red.json")
    assert result.returncode == 0, result.stderr
    path = fixture.art / "u-red.json"
    red = json.loads(path.read_text())
    red["predecessor"] = fixture.tests
    path.write_text(json.dumps(red))
    gate = module.Gate(fixture.repo, fixture.art, "u", fixture.predecessor, fixture.tests)

    def refuse_execution(identifier: str, commands: list[list[str]]) -> None:
        raise AssertionError("G2 accepted a root predecessor mismatch before mapped execution")

    monkeypatch.setattr(gate, "commands", refuse_execution)
    error = ""
    try:
        gate.green()
    except ValueError as failure:
        error = str(failure)
    assert "Red proof differs from frozen tests" in error, (
        "G2 accepted a root predecessor mismatch: " + error
    )


def lineage_gate(
    lineage: Fixture, monkeypatch: pytest.MonkeyPatch, change: tuple[str, str, str] | None = None
) -> "tuple[Gate, str]":
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/swarm"))
    module = importlib.import_module("unit_gate")
    path = lineage.art / "u-s1-tests.json"
    record = json.loads(path.read_text())
    record["tests_commit"] = git(lineage.repo, "rev-parse", "HEAD")
    if change:
        target, field, value = change
        relation = record["lineage"]["root" if target == "root" else "amendments"]
        (relation if target == "root" else relation[0])[field] = value
    path.write_text(json.dumps(record))
    (lineage.art / "u-acceptance.md").write_text("```json\n{}\n```\n")
    gate = module.Gate(lineage.repo, lineage.art, "u", lineage.predecessor, record["tests_commit"])

    def refuse_execution(identifier: str, commands: list[list[str]]) -> None:
        raise AssertionError("G2 ran mapped execution before refusing the lineage")

    monkeypatch.setattr(gate, "commands", refuse_execution)
    return gate, record["tests_commit"]


def test_g2_accepts_approved_amendment_bound_to_lineage_root(
    lineage: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, tests = lineage_gate(lineage, monkeypatch)
    assert tests != lineage.tests
    assert hasattr(gate, "red_proof"), "Gate does not expose red_proof"
    gate.red_proof()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (("root", "predecessor", "0" * 40), "invalid root"),
        (("amendment", "parent_inventory_sha256", "0" * 64), "wrong parent inventory"),
    ],
)
def test_g2_refuses_invalid_lineage_before_mapped_execution(
    lineage: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    change: tuple[str, str, str],
    reason: str,
) -> None:
    gate, _ = lineage_gate(lineage, monkeypatch, change)
    error = ""
    try:
        gate.green()
    except ValueError as failure:
        error = str(failure)
    assert f"Amendment lineage: {reason}" in error, "G2 accepted an invalid lineage: " + error


@pytest.mark.parametrize("claimed", [["assert True"], ["assert False", "assert True"]])
def test_correction_rejects_assertions_not_derived_from_test_blobs(
    lineage: Fixture, claimed: list[str]
) -> None:
    equivalence = lineage.art / "u-equivalence.json"
    equivalence.write_text(json.dumps({"assertions_before": claimed, "assertions_after": claimed}))
    record_path = lineage.art / "u-s1-tests.json"
    record = json.loads(record_path.read_text())
    amendment = record["lineage"]["amendments"][0]
    amendment["assertion_equivalence_sha256"] = digest(equivalence)
    body = {k: v for k, v in amendment.items() if not k.startswith("owner_approval")}
    approval = lineage.art / "u-approval.json"
    approval.write_text(
        json.dumps(
            {
                "owner": "u",
                "amendment_sha256": hashlib.sha256(
                    json.dumps(body, sort_keys=True).encode()
                ).hexdigest(),
            }
        )
    )
    amendment["owner_approval_sha256"] = digest(approval)
    record_path.write_text(json.dumps(record))
    old = lineage.snapshot()
    code, error = correction(lineage)
    assert code != 0, "writer accepted assertions that the test blobs do not contain"
    assert "Amendment lineage: assertion proof differs" in error, error
    assert lineage.snapshot() == old
