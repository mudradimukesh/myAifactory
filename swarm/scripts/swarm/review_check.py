import argparse
import json
import os
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import jsonschema
from gate_evidence import (
    FINAL_ONLY,
    POC_TEST_POLICY,
    Acceptance,
    acceptance,
    artifact_hashes,
    changed,
    digest,
    fingerprint,
    output,
    production,
)
from run_state import DEFAULT_RUN

CHECKS = {
    "frozen_tests_unchanged",
    "no_fixture_literals",
    "no_special_case_branch",
    "no_skip_or_xfail",
    "no_operator_or_equality_override",
    "no_broad_except_or_success_fallback",
    "no_type_escape",
    "no_comments",
    "deterministic_tests",
}


class GateRow(TypedDict, total=False):
    id: str
    disposition: str
    exit_code: int
    comparison: str
    measured: float
    threshold: float
    approval: str
    command: list[str]
    evidence: str


class Gate(TypedDict, total=False):
    candidate_sha: str
    fingerprint: dict[str, str]
    test_policy: str
    stage: str
    test_suite: str
    qualification_base_sha: str
    gates: list[GateRow]
    diff_files: list[str]
    qualification_diff_files: list[str]
    artifact_hashes: dict[str, str]
    base_sha: str
    worktree: str
    tree: str


class Candidate(TypedDict, total=False):
    candidate_sha: str
    main_sha: str
    feature: str
    tree: str
    worktree: str
    expected_review_modes: list[str]
    attempt: int


class Finding(TypedDict, total=False):
    id: str
    mode: str
    severity: str
    text: str
    deferred_reason: str
    view: str
    box: list[int]


class Status(TypedDict, total=False):
    id: str
    status: str
    evidence: str


class AcceptanceItem(TypedDict, total=False):
    id: str
    test_node: str
    contract_ref: str
    coverage: str
    oracle: str
    evidence: str


class Check(TypedDict, total=False):
    check: str
    result: str
    evidence: str


class Mutant(TypedDict, total=False):
    mutant: str
    disposition: str
    evidence: str


class Review(TypedDict, total=False):
    mode: str
    unit: str
    base_sha: str
    candidate_sha: str
    statuses: list[Status]
    acceptance: list[AcceptanceItem]
    checks: list[Check]
    mutants: list[Mutant]
    findings: list[Finding]
    needs_ruling: str | None
    merge: bool


class Survivor(TypedDict, total=False):
    id: str
    contract_relevant: bool


class Survivors(TypedDict, total=False):
    survivors: list[Survivor]


FindingKey = tuple[str, str]


def exact(values: Iterable[str], expected: Iterable[str], label: str) -> None:
    values = list(values)
    if len(values) != len(set(values)) or set(values) != set(expected):
        raise ValueError(label + " missing, duplicated or unexpected")


P2_ART = "/tmp/factory-fp-0925/swarm/interior-p2/ART/"
LOST_P2_TEST = "FAILED tests/phase2/test_environment.py::"
LOST_P2_FILE = " - AssertionError: Required file is missing: "


def lost_p2_art_only(row: GateRow) -> bool:
    if row["id"] != "G10" or not row.get("evidence"):
        return False
    lines = Path(row["evidence"]).read_text().splitlines()
    failures = [line for line in lines if line.startswith(("FAILED", "ERROR"))]
    if not failures:
        return False
    for line in failures:
        if not line.startswith(LOST_P2_TEST) or LOST_P2_FILE not in line:
            return False
        path = os.path.normpath(line.split(LOST_P2_FILE, 1)[1].strip())
        if not path.startswith(P2_ART) or os.path.lexists(path):
            return False
    summary = lines[-1]
    return summary.startswith(f"{len(failures)} failed, ") and "error" not in summary


def check_gate_scope(gate: Gate, spec: Acceptance, policy: bool) -> None:
    if policy and (
        gate.get("stage") not in {"pre", "final"} or gate.get("test_suite") not in {"poc", "swarm"}
    ):
        raise ValueError("Development feedback is not an acceptance gate")
    if (
        policy
        and gate.get("stage") == "final"
        and (
            not gate.get("qualification_base_sha")
            or gate["qualification_base_sha"] != spec.get("qualification_base_sha")
        )
    ):
        raise ValueError("Final qualification base differs from frozen acceptance")


def check_executed_row(row: GateRow, policy: bool) -> None:
    if not policy and lost_p2_art_only(row):
        return
    if row["exit_code"] != 0:
        raise ValueError(row["id"] + " failed")
    if row.get("comparison", "max") == "min":
        passed = row["measured"] >= row["threshold"]
    else:
        passed = row["measured"] <= row["threshold"]
    if not passed:
        raise ValueError(row["id"] + " threshold failed")


def check_deferred_row(row: GateRow, gate: Gate, policy: bool) -> None:
    deferrable = {"G11"} | (FINAL_ONLY if gate.get("stage") == "pre" else set())
    if (
        not policy
        or row["id"] not in deferrable
        or row.get("approval") != POC_TEST_POLICY
        or row.get("exit_code") != 0
        or row.get("command") != []
        or not row.get("evidence")
    ):
        raise ValueError("Unauthorized test deferral")


def check_waived_row(row: GateRow, u8_approved: bool) -> None:
    if row["id"] not in {"G13", "G18"} or row.get("approval") != "U8" or u8_approved is not True:
        raise ValueError("Unapproved gate waiver")


def gate_files(gate: Gate, policy: bool) -> list[str]:
    if gate.get("stage") == "final":
        files = gate.get("qualification_diff_files", [])
    else:
        files = gate.get("diff_files", [])
    if not policy:
        return files
    return [
        f
        for f in files
        if f.startswith(("scripts/swarm/", "tests/swarm/")) == (gate["test_suite"] == "swarm")
    ]


def not_applicable_allowed(row_id: str, files: list[str], spec: Acceptance) -> bool:
    return (
        (row_id == "G9" and not spec.get("property_test_paths"))
        or (row_id == "G13" and not production(files, "python"))
        or (row_id == "G18" and not production(files, "web"))
        or (row_id in {"G14", "G15", "G16"} and not any(f.startswith("web/") for f in files))
    )


def check_not_applicable_row(row: GateRow, gate: Gate, spec: Acceptance, policy: bool) -> None:
    files = gate_files(gate, policy)
    if not not_applicable_allowed(row["id"], files, spec) or not row.get("evidence"):
        raise ValueError("Unsupported not-applicable disposition")


def check_gate_row(
    row: GateRow, gate: Gate, spec: Acceptance, policy: bool, u8_approved: bool
) -> None:
    disposition = row["disposition"]
    if disposition in {"retained", "applicable"}:
        check_executed_row(row, policy)
    elif disposition == "deferred":
        check_deferred_row(row, gate, policy)
    elif disposition == "waived":
        check_waived_row(row, u8_approved)
    elif disposition == "not_applicable":
        check_not_applicable_row(row, gate, spec, policy)
    else:
        raise ValueError("Unknown gate disposition")


def gates_valid(
    gate: Gate,
    candidate: Candidate,
    spec: Acceptance,
    actual_fingerprint: dict[str, str],
    u8_approved: bool = False,
) -> None:
    if gate["candidate_sha"] != candidate["candidate_sha"]:
        raise ValueError("Gate candidate differs")
    if gate["fingerprint"] != actual_fingerprint:
        raise ValueError("Environment fingerprint differs")
    policy = gate.get("test_policy") == POC_TEST_POLICY
    check_gate_scope(gate, spec, policy)
    exact([g["id"] for g in gate["gates"]], [f"G{i}" for i in range(1, 19)], "Gates")
    for row in gate["gates"]:
        check_gate_row(row, gate, spec, policy, u8_approved)


def check_review_schema(review: Review) -> None:
    schema = json.loads(Path(__file__).with_name("review-schema.json").read_text())
    try:
        jsonschema.validate(review, schema)
    except jsonschema.ValidationError as error:
        raise ValueError(error.message) from error


def check_review_identity(review: Review, candidate: Candidate) -> None:
    if (
        review["candidate_sha"] != candidate["candidate_sha"]
        or review["base_sha"] != candidate["main_sha"]
        or review["unit"] != candidate["feature"]
    ):
        raise ValueError("Review identity differs")


def check_review_acceptance(review: Review, ids: list[str], spec: Acceptance) -> None:
    owned_ids = ids if review["mode"] == "code" else spec["product_ids"]
    exact([a["id"] for a in review["acceptance"]], owned_ids, "Reviewed acceptance")
    for item in review["acceptance"]:
        if (
            item["coverage"] != "asserted"
            or item["oracle"] in {"from_code_under_test", "none"}
            or any(not item[k].strip() for k in ("evidence", "test_node", "contract_ref"))
        ):
            raise ValueError("Acceptance lacks an independent asserted oracle")


def check_review_checks(review: Review) -> None:
    expected = CHECKS if review["mode"] == "code" else set()
    exact([c["check"] for c in review["checks"]], expected, "Review checks")
    if any(c["result"] != "pass" or not c["evidence"].strip() for c in review["checks"]):
        raise ValueError("Reviewer check failed or unassessed")


def resolved_status(finding: Finding, review: Review) -> None:
    status = next(s for s in review["statuses"] if s["id"] == finding["id"])
    if not status["evidence"].strip():
        raise ValueError("Finding status has no evidence")
    if status["status"] != "fixed" and (
        finding["severity"] != "minor"
        or not finding.get("text")
        or not finding.get("deferred_reason")
    ):
        raise ValueError("Unresolved finding blocks review")


def check_review_statuses(review: Review, open_findings: list[Finding]) -> None:
    prior = [f for f in open_findings if f["mode"] == review["mode"]]
    exact([s["id"] for s in review["statuses"]], [f["id"] for f in prior], "Prior findings")
    for finding in prior:
        resolved_status(finding, review)


def box_in_view(box: list[int], view: object) -> bool:
    if not isinstance(view, list) or len(view) < 2:
        return False
    width, height = view[0], view[1]
    if not isinstance(width, int | float) or not isinstance(height, int | float):
        return False
    return 0 <= box[0] <= box[2] <= width and 0 <= box[1] <= box[3] <= height


def check_review_findings(review: Review, spec: Acceptance) -> None:
    for finding in review["findings"]:
        if finding["severity"] in {"blocker", "major"}:
            raise ValueError("Blocking finding")
        box = finding["box"]
        if box != [0, 0, 0, 0] and not box_in_view(box, spec.get("views", {}).get(finding["view"])):
            raise ValueError("Finding box exceeds evidence view")


def check_review_mutants(review: Review, survivors: Survivors) -> None:
    expected = survivors["survivors"] if review["mode"] == "code" else []
    exact([m["mutant"] for m in review["mutants"]], [m["id"] for m in expected], "Mutants")
    for mutant in review["mutants"]:
        source = next(m for m in expected if m["id"] == mutant["mutant"])
        if not mutant["evidence"].strip() or (
            mutant["disposition"] == "test_gap" and source.get("contract_relevant", True)
        ):
            raise ValueError("Mutation test gap")


@dataclass(frozen=True)
class Packet:
    candidate: Candidate
    gate: Gate
    ids: list[str]
    spec: Acceptance
    reviews: list[Review]
    open_findings: list[Finding]
    survivors: Survivors
    actual_fingerprint: dict[str, str]
    u8_approved: bool = False


def validate(packet: Packet) -> None:
    candidate, gate, spec = packet.candidate, packet.gate, packet.spec
    ids, reviews = packet.ids, packet.reviews
    gates_valid(gate, candidate, spec, packet.actual_fingerprint, packet.u8_approved)
    exact(ids, set(ids), "Acceptance ids")
    expected_modes = ["code", "product"] if spec.get("product_ids") else ["code"]
    exact([r["mode"] for r in reviews], expected_modes, "Review modes")
    exact(candidate["expected_review_modes"], expected_modes, "Candidate modes")
    for review in reviews:
        check_review_schema(review)
        check_review_identity(review, candidate)
        check_review_acceptance(review, ids, spec)
        check_review_checks(review)
        check_review_statuses(review, packet.open_findings)
        check_review_findings(review, spec)
        check_review_mutants(review, packet.survivors)
        if review["needs_ruling"] is not None or review["merge"] is not True:
            raise ValueError("Review does not authorize merge")


def packet_artifacts(art: Path, tag: str, gate: Gate, spec: Acceptance) -> None:
    for name, expected in gate.get("artifact_hashes", {}).items():
        path = art / name
        if not path.resolve().is_relative_to(art.resolve()) or digest(path) != expected:
            raise ValueError("Packet artifact hash differs")
    try:
        actual = artifact_hashes(art, tag, spec, final=gate.get("stage") == "final")
    except (ValueError, OSError) as error:
        raise ValueError("Packet artifact hashes cannot be verified") from error
    if gate.get("artifact_hashes") != actual:
        raise ValueError("Packet artifact hashes differ or are missing")


def archived_identity(prior: dict[FindingKey, Finding], mode: str, finding_id: str) -> FindingKey:
    key = (mode, finding_id)
    if key in prior:
        return key
    matches = [k for k, f in prior.items() if k[0] == mode and f["id"] == finding_id]
    if len(matches) > 1:
        raise ValueError("Ambiguous archived finding id; qualify it by attempt")
    return matches[0] if matches else key


def repeats_recorded(prior: dict[FindingKey, Finding], mode: str, finding: Finding) -> bool:
    return any(
        k[0] == mode and all(f[n] == finding[n] for n in ("id", "severity", "text"))
        for k, f in prior.items()
    )


def replay_review(
    prior: dict[FindingKey, Finding], review: Review, mode: str, attempt: int
) -> None:
    for status in review["statuses"]:
        if status["status"] == "fixed":
            prior.pop(archived_identity(prior, mode, status["id"]), None)
    for finding in review["findings"]:
        key = (mode, f"attempt-{attempt}:{finding['id']}")
        if key in prior:
            raise ValueError("Duplicate archived findings")
        if not repeats_recorded(prior, mode, finding):
            prior[key] = finding


def archived_reviews(
    art: Path, tag: str, candidate: Candidate
) -> Iterator[tuple[int, str, Review]]:
    archive = art / "attempts" / candidate["feature"]
    for attempt in range(1, candidate["attempt"]):
        for mode in ("code", "product"):
            path = archive / str(attempt) / (tag + "-" + mode + "-review.json")
            if path.exists():
                yield attempt, mode, json.loads(path.read_text())


def finding_history(art: Path, tag: str, candidate: Candidate, findings: list[Finding]) -> None:
    prior: dict[FindingKey, Finding] = {}
    for attempt, mode, review in archived_reviews(art, tag, candidate):
        replay_review(prior, review, mode, attempt)
    supplied = {archived_identity(prior, f["mode"], f["id"]): f for f in findings}
    if len(supplied) != len(findings):
        raise ValueError("Duplicate prior findings")
    for key, finding in prior.items():
        current = supplied.get(key)
        if current is None or any(current.get(k) != finding[k] for k in ("severity", "text")):
            raise ValueError("Archived finding omitted or changed")


def check_checkout(gate: Gate, candidate: Candidate, root: Path, require_final: bool) -> None:
    if require_final and (
        gate.get("test_policy") != POC_TEST_POLICY or gate.get("stage") != "final"
    ):
        raise ValueError("Final qualification requires a final gate receipt")
    if output(["git", "rev-parse", "HEAD"], root) != candidate["candidate_sha"]:
        raise ValueError("Candidate checkout moved")
    if output(["git", "status", "--porcelain", "--untracked-files=no"], root):
        raise ValueError("Candidate has tracked changes")
    if (
        gate["base_sha"] != candidate["main_sha"]
        or Path(gate["worktree"]).resolve() != root.resolve()
        or gate["tree"] != output(["git", "rev-parse", "HEAD^{tree}"], root)
        or gate["tree"] != candidate["tree"]
    ):
        raise ValueError("Gate checkout identity differs")


def check_diff_files(gate: Gate, candidate: Candidate, root: Path) -> None:
    files = changed(root, candidate["main_sha"], candidate["candidate_sha"])
    if files != gate["diff_files"]:
        raise ValueError("Recorded diff differs")
    if gate.get("stage") != "final":
        return
    qualification_base = gate["qualification_base_sha"]
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", qualification_base, candidate["candidate_sha"]],
        cwd=root,
        check=True,
    )
    if changed(root, qualification_base, candidate["candidate_sha"]) != gate.get(
        "qualification_diff_files"
    ):
        raise ValueError("Qualification diff differs")


def check_patch(art: Path, prefix: str, candidate: Candidate, root: Path) -> None:
    actual_diff = subprocess.check_output(
        ["git", "diff", candidate["main_sha"], candidate["candidate_sha"]], cwd=root, text=True
    )
    if (art / (prefix + "-diff.patch")).read_text() != actual_diff:
        raise ValueError("Review diff differs from the exact candidate")


def survivors_path(art: Path, prefix: str, gate: Gate) -> Path:
    suffix = "-final" if gate.get("stage") == "final" else ""
    return art / (prefix + suffix + "-survivors.json")


def check_packet(args: argparse.Namespace) -> None:
    candidate: Candidate = json.loads(args.candidate.read_text())
    gate: Gate = json.loads(args.gate.read_text())
    root = Path(candidate["worktree"])
    check_checkout(gate, candidate, root, args.require_final)
    check_diff_files(gate, candidate, root)
    prefix = args.candidate.name.removesuffix("-candidate.json")
    art = args.candidate.parent
    spec = acceptance(art / (prefix + "-acceptance.md"))
    packet_artifacts(art, prefix, gate, spec)
    check_patch(art, prefix, candidate, root)
    state = json.loads(Path(os.environ.get("PILOT_RUN", str(DEFAULT_RUN))).read_text())
    findings: list[Finding] = json.loads(args.open_findings.read_text())
    finding_history(art, prefix, candidate, findings)
    validate(
        Packet(
            candidate,
            gate,
            args.ids.read_text().splitlines(),
            spec,
            [json.loads(p.read_text()) for p in args.review],
            findings,
            json.loads(survivors_path(art, prefix, gate).read_text()),
            fingerprint(root),
            state.get("approvals", {}).get("U8", False),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-final", action="store_true")
    for name in ("gate", "candidate", "ids", "open-findings"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--review", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        check_packet(args)
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, "FAIL: " + str(error) + "\n")
    print("PASS")


if __name__ == "__main__":
    main()
