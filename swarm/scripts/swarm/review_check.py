import argparse
import json
import os
import subprocess
from pathlib import Path

import jsonschema
from gate_evidence import (
    FINAL_ONLY,
    POC_TEST_POLICY,
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


def exact(values, expected, label):
    if len(values) != len(set(values)) or set(values) != set(expected):
        raise ValueError(label + " missing, duplicated or unexpected")


P2_ART = "/tmp/factory-fp-0925/swarm/interior-p2/ART/"
LOST_P2_TEST = "FAILED tests/phase2/test_environment.py::"
LOST_P2_FILE = " - AssertionError: Required file is missing: "


def lost_p2_art_only(row):
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


def gates_valid(gate, candidate, spec, actual_fingerprint, u8_approved=False):
    if gate["candidate_sha"] != candidate["candidate_sha"]:
        raise ValueError("Gate candidate differs")
    if gate["fingerprint"] != actual_fingerprint:
        raise ValueError("Environment fingerprint differs")
    policy = gate.get("test_policy") == POC_TEST_POLICY
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
    exact([g["id"] for g in gate["gates"]], [f"G{i}" for i in range(1, 19)], "Gates")
    for row in gate["gates"]:
        disposition = row["disposition"]
        if disposition in {"retained", "applicable"}:
            if not policy and lost_p2_art_only(row):
                continue
            if row["exit_code"] != 0:
                raise ValueError(row["id"] + " failed")
            if row.get("comparison", "max") == "min":
                passed = row["measured"] >= row["threshold"]
            else:
                passed = row["measured"] <= row["threshold"]
            if not passed:
                raise ValueError(row["id"] + " threshold failed")
        elif disposition == "deferred":
            allowed = {"G11"} | (FINAL_ONLY if gate.get("stage") == "pre" else set())
            if (
                not policy
                or row["id"] not in allowed
                or row.get("approval") != POC_TEST_POLICY
                or row.get("exit_code") != 0
                or row.get("command") != []
                or not row.get("evidence")
            ):
                raise ValueError("Unauthorized test deferral")
        elif disposition == "waived":
            if (
                row["id"] not in {"G13", "G18"}
                or row.get("approval") != "U8"
                or u8_approved is not True
            ):
                raise ValueError("Unapproved gate waiver")
        elif disposition == "not_applicable":
            files = (
                gate.get("qualification_diff_files", [])
                if gate.get("stage") == "final"
                else gate.get("diff_files", [])
            )
            if policy:
                files = [
                    f
                    for f in files
                    if f.startswith(("scripts/swarm/", "tests/swarm/"))
                    == (gate["test_suite"] == "swarm")
                ]
            allowed = (
                (row["id"] == "G9" and not spec.get("property_test_paths"))
                or (row["id"] == "G13" and not production(files, "python"))
                or (row["id"] == "G18" and not production(files, "web"))
                or (
                    row["id"] in {"G14", "G15", "G16"}
                    and not any(f.startswith("web/") for f in files)
                )
            )
            if not allowed or not row.get("evidence"):
                raise ValueError("Unsupported not-applicable disposition")
        else:
            raise ValueError("Unknown gate disposition")


def validate(
    candidate,
    gate,
    ids,
    spec,
    reviews,
    open_findings,
    survivors,
    actual_fingerprint,
    u8_approved=False,
):
    gates_valid(gate, candidate, spec, actual_fingerprint, u8_approved)
    exact(ids, set(ids), "Acceptance ids")
    expected_modes = ["code", "product"] if spec.get("product_ids") else ["code"]
    exact([r["mode"] for r in reviews], expected_modes, "Review modes")
    exact(candidate["expected_review_modes"], expected_modes, "Candidate modes")
    schema = json.loads(Path(__file__).with_name("review-schema.json").read_text())
    for review in reviews:
        try:
            jsonschema.validate(review, schema)
        except jsonschema.ValidationError as error:
            raise ValueError(error.message) from error
        mode = review["mode"]
        if (
            review["candidate_sha"] != candidate["candidate_sha"]
            or review["base_sha"] != candidate["main_sha"]
            or review["unit"] != candidate["feature"]
        ):
            raise ValueError("Review identity differs")
        owned_ids = ids if mode == "code" else spec["product_ids"]
        exact([a["id"] for a in review["acceptance"]], owned_ids, "Reviewed acceptance")
        for item in review["acceptance"]:
            if (
                item["coverage"] != "asserted"
                or item["oracle"] in {"from_code_under_test", "none"}
                or any(not item[k].strip() for k in ("evidence", "test_node", "contract_ref"))
            ):
                raise ValueError("Acceptance lacks an independent asserted oracle")
        exact(
            [c["check"] for c in review["checks"]],
            CHECKS if mode == "code" else [],
            "Review checks",
        )
        if any(c["result"] != "pass" or not c["evidence"].strip() for c in review["checks"]):
            raise ValueError("Reviewer check failed or unassessed")
        prior = [f for f in open_findings if f["mode"] == mode]
        exact([s["id"] for s in review["statuses"]], [f["id"] for f in prior], "Prior findings")
        for finding in prior:
            status = next(s for s in review["statuses"] if s["id"] == finding["id"])
            if not status["evidence"].strip():
                raise ValueError("Finding status has no evidence")
            if status["status"] != "fixed" and (
                finding["severity"] != "minor"
                or not finding.get("text")
                or not finding.get("deferred_reason")
            ):
                raise ValueError("Unresolved finding blocks review")
        for finding in review["findings"]:
            if finding["severity"] in {"blocker", "major"}:
                raise ValueError("Blocking finding")
            box = finding["box"]
            if box != [0, 0, 0, 0]:
                view = spec.get("views", {}).get(finding["view"])
                if not view or not (
                    0 <= box[0] <= box[2] <= view[0] and 0 <= box[1] <= box[3] <= view[1]
                ):
                    raise ValueError("Finding box exceeds evidence view")
        expected_mutants = survivors["survivors"] if mode == "code" else []
        exact(
            [m["mutant"] for m in review["mutants"]], [m["id"] for m in expected_mutants], "Mutants"
        )
        for mutant in review["mutants"]:
            source = next(m for m in expected_mutants if m["id"] == mutant["mutant"])
            if not mutant["evidence"].strip() or (
                mutant["disposition"] == "test_gap" and source.get("contract_relevant", True)
            ):
                raise ValueError("Mutation test gap")
        if review["needs_ruling"] is not None or review["merge"] is not True:
            raise ValueError("Review does not authorize merge")


def packet_artifacts(art, tag, gate, spec):
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


def finding_history(art, tag, candidate, findings):
    prior = {}

    def identity(mode, finding_id):
        key = (mode, finding_id)
        if key in prior:
            return key
        matches = [k for k, f in prior.items() if k[0] == mode and f["id"] == finding_id]
        if len(matches) > 1:
            raise ValueError("Ambiguous archived finding id; qualify it by attempt")
        return matches[0] if matches else key

    archive = art / "attempts" / candidate["feature"]
    for attempt in range(1, candidate["attempt"]):
        for mode in ("code", "product"):
            path = archive / str(attempt) / (tag + "-" + mode + "-review.json")
            if not path.exists():
                continue
            review = json.loads(path.read_text())
            for status in review["statuses"]:
                if status["status"] == "fixed":
                    prior.pop(identity(mode, status["id"]), None)
            for finding in review["findings"]:
                key = (mode, f"attempt-{attempt}:{finding['id']}")
                if key in prior:
                    raise ValueError("Duplicate archived findings")
                prior[key] = finding
    supplied = {identity(f["mode"], f["id"]): f for f in findings}
    if len(supplied) != len(findings):
        raise ValueError("Duplicate prior findings")
    for key, finding in prior.items():
        current = supplied.get(key)
        if current is None or any(current.get(k) != finding[k] for k in ("severity", "text")):
            raise ValueError("Archived finding omitted or changed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-final", action="store_true")
    for name in ("gate", "candidate", "ids", "open-findings"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--review", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        candidate = json.loads(args.candidate.read_text())
        gate = json.loads(args.gate.read_text())
        if args.require_final and (
            gate.get("test_policy") != POC_TEST_POLICY or gate.get("stage") != "final"
        ):
            raise ValueError("Final qualification requires a final gate receipt")
        root = Path(candidate["worktree"])
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
        files = changed(root, candidate["main_sha"], candidate["candidate_sha"])
        if files != gate["diff_files"]:
            raise ValueError("Recorded diff differs")
        if gate.get("stage") == "final":
            qualification_base = gate["qualification_base_sha"]
            subprocess.run(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    qualification_base,
                    candidate["candidate_sha"],
                ],
                cwd=root,
                check=True,
            )
            if changed(root, qualification_base, candidate["candidate_sha"]) != gate.get(
                "qualification_diff_files"
            ):
                raise ValueError("Qualification diff differs")
        prefix = args.candidate.name.removesuffix("-candidate.json")
        art = args.candidate.parent
        spec = acceptance(art / (prefix + "-acceptance.md"))
        packet_artifacts(art, prefix, gate, spec)
        actual_diff = subprocess.check_output(
            ["git", "diff", candidate["main_sha"], candidate["candidate_sha"]], cwd=root, text=True
        )
        if (art / (prefix + "-diff.patch")).read_text() != actual_diff:
            raise ValueError("Review diff differs from the exact candidate")
        state = json.loads(Path(os.environ.get("PILOT_RUN", str(DEFAULT_RUN))).read_text())
        findings = json.loads(args.open_findings.read_text())
        finding_history(art, prefix, candidate, findings)
        validate(
            candidate,
            gate,
            args.ids.read_text().splitlines(),
            spec,
            [json.loads(p.read_text()) for p in args.review],
            findings,
            json.loads(
                (
                    art
                    / (
                        prefix
                        + ("-final" if gate.get("stage") == "final" else "")
                        + "-survivors.json"
                    )
                ).read_text()
            ),
            fingerprint(root),
            state.get("approvals", {}).get("U8", False),
        )
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, "FAIL: " + str(error) + "\n")
    print("PASS")


if __name__ == "__main__":
    main()
