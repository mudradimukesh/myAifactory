import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BOUND = {"run": "r", "feature": "f", "sha": "abc", "acceptance_sha256": "def"}


@pytest.fixture
def jev(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "jev_triage", ROOT / "scripts/swarm/jev_triage.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keys = tmp_path / "keys"
    keys.write_text("TYPE_SAFE_KEY=test\n")
    monkeypatch.setattr(module, "KEYS", keys)
    monkeypatch.setattr(module, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(sys, "argv", ["jev_triage.py", "test"])
    module.requests = []
    return module


def reply(jev, monkeypatch, answers=None, error=None):
    def urlopen(request, timeout):
        jev.requests.append(json.loads(request.data))
        if error:
            raise error
        body = answers(jev.requests[-1]["questions"]) if callable(answers) else answers
        return io.BytesIO(
            json.dumps(
                {"model": "jev-1.13.0", "answers": body, "usage": {"input_tokens": 1000}}
            ).encode()
        )

    monkeypatch.setattr(jev.urllib.request, "urlopen", urlopen)


def scores(values, confidence=0.9):
    return lambda questions: {
        k: {"type": "score", "score": values[k[-1]][k[:-1]], "confidence": confidence}
        for k in questions
    }


def candidates():
    return BOUND | {
        "goal": "customer plan workspace to parser",
        "candidates": [
            {"id": "upload", "group": 2, "order": 1, "receipts": ["poc-execution.md#2"]},
            {"id": "a11y", "group": 2, "order": 2, "receipts": ["review.json#4"]},
            {"id": "history", "group": 7, "order": 1},
            {"id": "parser", "group": 2, "order": 3, "blocked_by": ["upload not accepted"]},
        ],
    }


LOW = {"journey": 1, "reliability": 0, "unblocking": 0}
HIGH = {"journey": 4, "reliability": 3, "unblocking": 4}


def test_priority_ranks_only_first_eligible_group(jev, monkeypatch):
    reply(jev, monkeypatch, scores({"0": LOW, "1": HIGH}))
    out = jev.priority(candidates())
    assert out["customer_order"] == ["upload", "a11y"]
    assert out["excluded"] == ["history", "parser"]
    assert out["ranked"] == ["a11y", "upload"] and out["outcome"] == "advisory_rank"
    assert out["scores"]["a11y"]["composite"] == pytest.approx(0.5 + 0.3 * 0.75 + 0.2)
    assert out["evidence"]["upload"] == ["poc-execution.md#2"]
    sent = jev.requests[0]
    assert sent["model"] == "jev-1.13.0" and len(sent["questions"]) == 6
    assert all("order" not in c and "group" not in c for c in sent["state"]["candidates"])
    receipt = out["receipt"]
    assert receipt["returned_model"] == "jev-1.13.0" and receipt["usage"] == {"input_tokens": 1000}
    assert (
        {k: receipt[k] for k in BOUND} == BOUND
        and receipt["raw_answers"]
        and receipt["latency_s"] is not None
    )
    ledger = json.loads((jev.LEDGER).read_text())
    assert ledger["input_sha256"] == receipt["input_sha256"] and ledger[
        "cost_usd"
    ] == pytest.approx(0.000042)


def test_deferred_history_never_outranks_required_group(jev, monkeypatch):
    reply(jev, monkeypatch, scores({"0": LOW, "1": LOW}))
    packet = candidates()
    packet["candidates"][2]["group"] = 9
    assert "history" not in jev.priority(packet)["ranked"]


@pytest.mark.parametrize(
    "values,confidence", [({"0": HIGH, "1": HIGH}, 0.9), ({"0": LOW, "1": HIGH}, 0.3)]
)
def test_ties_and_low_confidence_keep_customer_order(jev, monkeypatch, values, confidence):
    reply(jev, monkeypatch, scores(values, confidence))
    out = jev.priority(candidates())
    assert out["ranked"] == ["upload", "a11y"] and out["outcome"] == "customer_order"


@pytest.mark.parametrize(
    "answers,error",
    [
        (None, TimeoutError("timed out")),
        ({}, None),
        (lambda q: {k: {"score": 9, "confidence": 0.9} for k in q}, None),
        (lambda q: {k: {"score": "high", "confidence": 0.9} for k in q}, None),
    ],
)
def test_priority_failure_keeps_customer_order(jev, monkeypatch, answers, error):
    reply(jev, monkeypatch, answers, error)
    out = jev.priority(candidates())
    assert out["ranked"] == ["upload", "a11y"] and out["outcome"] == "customer_order"
    assert out["receipt"]["error"]


def test_single_candidate_makes_no_call(jev, monkeypatch):
    reply(jev, monkeypatch, {})
    packet = candidates()
    packet["candidates"] = packet["candidates"][:1]
    assert jev.priority(packet)["ranked"] == ["upload"] and jev.requests == []


def evidence(**checks):
    return BOUND | {
        "deterministic": checks,
        "acceptance": "POC-1D account isolation",
        "receipts": "red.json: 52 failed",
        "claim": "all green",
        "next_step": "merge",
    }


@pytest.fixture
def passing(tmp_path):
    red = tmp_path / "red.json"
    red.write_text(json.dumps({"passed": True, "tests_commit": "t1"}))
    return evidence(**dict.fromkeys(MANDATORY, True)) | {
        "red_receipt": str(red),
        "tests_commit": "t1",
    }


MANDATORY = (
    "required_receipts",
    "exact_sha",
    "frozen_hashes",
    "acceptance_coverage",
    "assertion_red",
    "ownership",
)


@pytest.mark.parametrize(
    "checks", [{}, {"exact_sha": False, "red_proof": True}, {"stale_receipt": "unknown"}]
)
@pytest.mark.parametrize("noul", [0.0, 0.99])
def test_deterministic_failure_blocks_for_every_jev_output(jev, monkeypatch, checks, noul):
    reply(jev, monkeypatch, lambda q: {k: {"noul": noul} for k in q})
    out = jev.evidence(evidence(**checks))
    assert out["outcome"] == "BLOCKED" and jev.requests == []
    assert {k: out["receipt"][k] for k in BOUND} == BOUND and out["receipt"]["called"] is False
    assert out["receipt"]["failed_checks"] == out["failed_checks"]


@pytest.mark.parametrize(
    "flag",
    ["contradicts_checks", "omits_requirement", "repeats_failed_premise", "fixture_as_journey"],
)
def test_any_concerning_flag_routes_to_review(jev, monkeypatch, flag, passing):
    reply(jev, monkeypatch, lambda q: {k: {"noul": 0.5 if k == flag else 0.1} for k in q})
    out = jev.evidence(passing)
    assert out["outcome"] == "REVIEW" and out["flags"][flag] == 0.5


def test_benign_report_is_no_flag_not_pass(jev, monkeypatch, passing):
    reply(jev, monkeypatch, lambda q: {k: {"noul": 0.1} for k in q})
    out = jev.evidence(passing)
    assert out["outcome"] == "NO_FLAG" and out["receipt"]["raw_answers"]


@pytest.mark.parametrize(
    "answers,error",
    [
        (None, TimeoutError("timed out")),
        ({"contradicts_checks": {}}, None),
        (lambda q: {k: {"noul": 1.5} for k in q}, None),
    ],
)
def test_evidence_failure_is_unknown(jev, monkeypatch, answers, error, passing):
    reply(jev, monkeypatch, answers, error)
    out = jev.evidence(passing)
    assert out["outcome"] == "UNKNOWN" and out["receipt"]["error"]


def test_cap_refusal_is_unknown(jev, monkeypatch, passing):
    reply(jev, monkeypatch, lambda q: {k: {"noul": 0.1} for k in q})
    jev.LEDGER.write_text(json.dumps({"cost_usd": 999}) + "\n")
    out = jev.evidence(passing)
    assert out["outcome"] == "UNKNOWN" and "cap" in out["receipt"]["error"] and jev.requests == []


def test_oversized_input_is_unknown_without_call(jev, monkeypatch, passing):
    reply(jev, monkeypatch, {})
    packet = passing | {"receipts": "x" * (jev.PICK_MAX_CHARS + 1)}
    assert jev.evidence(packet)["outcome"] == "UNKNOWN" and jev.requests == []


def test_missing_candidate_receipts_keep_order_without_call(jev, monkeypatch):
    reply(jev, monkeypatch, scores({"0": LOW, "1": HIGH}))
    packet = candidates()
    packet["candidates"][1]["receipts"] = []
    out = jev.priority(packet)
    assert out["ranked"] == ["upload", "a11y"] and out["outcome"] == "customer_order"
    assert "a11y" in out["reason"] and jev.requests == []
    assert {k: out["receipt"][k] for k in BOUND} == BOUND and out["receipt"]["called"] is False


@pytest.mark.parametrize("first,second,moved", [(1.992, 2.008, False), (1.0, 1.42, True)])
def test_near_ties_keep_order_across_old_buckets(jev, monkeypatch, first, second, moved):
    values = {"0": {"journey": first, "reliability": 0, "unblocking": 0}}
    values["1"] = {"journey": second, "reliability": 0, "unblocking": 0}
    reply(jev, monkeypatch, scores(values))
    out = jev.priority(candidates())
    assert out["ranked"] == (["a11y", "upload"] if moved else ["upload", "a11y"])


def test_advisory_never_passes_a_near_tie(jev):
    scores = {"a": {"composite": 0.30}, "b": {"composite": 0.34}, "c": {"composite": 0.38}}
    ranked = jev.advisory(["a", "b", "c"], scores)
    for x in ranked:
        for y in ranked[ranked.index(x) + 1 :]:
            if "abc".index(x) > "abc".index(y):
                assert scores[x]["composite"] - scores[y]["composite"] > jev.TIE


def test_single_candidate_receipt_is_bound(jev, monkeypatch):
    reply(jev, monkeypatch, {})
    packet = candidates()
    packet["candidates"] = packet["candidates"][:1]
    receipt = jev.priority(packet)["receipt"]
    assert {k: receipt[k] for k in BOUND} == BOUND and receipt["called"] is False


@pytest.mark.parametrize("check", MANDATORY)
def test_each_mandatory_check_is_required(jev, monkeypatch, passing, check):
    reply(jev, monkeypatch, lambda q: {k: {"noul": 0.0} for k in q})
    del passing["deterministic"][check]
    out = jev.evidence(passing)
    assert out["outcome"] == "BLOCKED" and check in out["failed_checks"] and jev.requests == []


@pytest.mark.parametrize(
    "red",
    [None, {"passed": False, "tests_commit": "t1"}, {"passed": True, "tests_commit": "other"}],
)
def test_red_proof_requires_matching_passed_receipt(jev, monkeypatch, passing, red):
    reply(jev, monkeypatch, lambda q: {k: {"noul": 0.0} for k in q})
    path = Path(passing["red_receipt"])
    if red is None:
        path.unlink()
    else:
        path.write_text(json.dumps(red))
    out = jev.evidence(passing)
    assert out["outcome"] == "BLOCKED" and out["checks"]["assertion_red"] is False


@pytest.mark.parametrize("noul", [0.0, 0.99])
def test_diagnostic_call_retains_failed_gates(jev, monkeypatch, passing, noul):
    reply(jev, monkeypatch, lambda q: {k: {"noul": noul} for k in q})
    Path(passing["red_receipt"]).unlink()
    out = jev.evidence(passing | {"diagnostic": True})
    assert out["outcome"] == "BLOCKED" and out["failed_checks"] == ["assertion_red"]
    assert out["checks"]["assertion_red"] is False and out["flags"]
    assert jev.requests[0]["state"]["failed_checks"] == ["assertion_red"]
    receipt = out["receipt"]
    assert receipt["called"] is True and receipt["failed_checks"] == ["assertion_red"]
    assert receipt["checks"]["assertion_red"] is False


def test_cli_requires_explicit_ledger(jev, monkeypatch, tmp_path):
    packet = tmp_path / "p.json"
    packet.write_text(json.dumps(evidence()))
    monkeypatch.delenv("JEV_LEDGER", raising=False)
    monkeypatch.setattr(sys, "argv", ["jev_triage.py", "evidence", str(packet)])
    with pytest.raises(SystemExit, match="JEV_LEDGER"):
        jev.main()
    assert not packet.with_suffix(".jev.json").exists()
