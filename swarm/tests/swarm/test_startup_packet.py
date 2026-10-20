import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/swarm"))
from startup_packet import MAX_ENTRIES, commit, render  # noqa: E402

sys.path.pop(0)


def new_entries(packet):
    return packet.split("New or changed entries")[1]


@pytest.fixture
def art(tmp_path):
    (tmp_path / "run.json").write_text(
        json.dumps(
            {
                "active_lead": {"model_session_id": "lead-1", "pid": 999999, "pane": "7"},
                "inventory": {"threads": [{"role": "orchestrator", "pane_id": 8, "pid": 1}]},
                "reservations": {
                    "done": {"released": True},
                    "unit-1": {"role": "builder", "active_run": "unit-1", "observed": 5},
                },
                "lead_scopes": {"lead-1": [str(tmp_path / "missing-wt")]},
                "blockers": [{"id": "BLOCKER1g", "status": "open", "reason": "merge gate"}],
            }
        )
    )
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/packet-lead-delivered.json").write_text("[]")
    return tmp_path


def test_packet_carries_ledger_and_evidence(art):
    (art / "unit-candidate.json").write_text(
        json.dumps({"attempt": 2, "candidate_sha": "abcdef123456", "status": "prepared"})
    )
    (art / "unit-gate.json").write_text(
        json.dumps({"candidate_sha": "abcdef12", "gates": [{"id": "G10", "exit_code": 1}]})
    )
    (art / "questions.md").write_text("")
    packet = render(art, "lead", "k1")
    assert "session lead-1, pid 999999 (absent), pane 7" in packet
    assert "Newest orchestrator: pane 8" in packet
    assert f"{art / 'missing-wt'}: missing" in packet
    assert "- unit-1: builder" in packet
    assert "done:" not in packet
    assert "BLOCKER1g: merge gate" in packet
    assert "unit: candidate attempt 2 abcdef12 status prepared" in packet
    assert "failed ['G10']" in packet
    assert "missing acceptance.md, ids.txt, test-map.json" in packet


def test_old_unfinished_candidate_is_still_listed(art):
    import os

    old = art / "stale-candidate.json"
    old.write_text(json.dumps({"attempt": 1, "status": "prepared"}))
    os.utime(old, (0, 0))
    pushed = art / "done-candidate.json"
    pushed.write_text(json.dumps({"attempt": 1, "status": "pushed"}))
    os.utime(pushed, (0, 0))
    (art / "questions.md").write_text("")
    packet = render(art, "lead", "k1")
    assert "- stale: candidate attempt 1" in packet
    assert "- done:" not in packet


def test_heading_entries_and_explicit_answers(art):
    (art / "questions.md").write_text(
        "## RULING1a (orchestrator): headed ruling\n"
        "Q-lead-0123abcd-1: first question\n"
        "Q-lead-0123abcd-2: still waiting for an answer to Q-lead-0123abcd-1\n"
        "- RULING1b (orchestrator, answers Q-lead-0123abcd-3): yes\n"
        "### Q-lead-0123abcd-3: answered question\n"
    )
    packet = render(art, "lead", "k1")
    assert "[1] ## RULING1a (orchestrator): headed ruling" in packet
    unanswered = packet.split("Unanswered lead questions")[1].split("New or changed")[0]
    assert "first question" in unanswered
    assert "still waiting" in unanswered
    assert "answered question" not in unanswered


def test_entries_are_committed_only_after_acknowledgement(art):
    (art / "questions.md").write_text("RULING1a (orchestrator): first\n")
    assert "RULING1a" in new_entries(render(art, "lead", "k1"))
    # The launch failed, so nothing was committed: the retry re-delivers the entry.
    assert "RULING1a" in new_entries(render(art, "lead", "k1"))
    commit(art, "lead", "k1")
    assert "RULING1a" not in new_entries(render(art, "lead", "k1"))


def test_edited_entry_counts_as_new(art):
    (art / "questions.md").write_text("RULING1a (orchestrator): approved\n")
    render(art, "lead", "k1")
    commit(art, "lead", "k1")
    (art / "questions.md").write_text("RULING1a (orchestrator): approved\nRevoked: not approved\n")
    assert "Revoked: not approved" in new_entries(render(art, "lead", "k1"))


def test_entries_beyond_the_limit_stay_unacknowledged(art):
    total = MAX_ENTRIES + 5
    (art / "questions.md").write_text("".join(f"RULING{i}a (o): r\n" for i in range(total)))
    packet = render(art, "lead", "k1")
    assert f"5 more not shown here (lines {MAX_ENTRIES + 1}-{total})" in packet
    assert f"[{total}] RULING{total - 1}a" in packet
    commit(art, "lead", "k1")
    later = new_entries(render(art, "lead", "k1"))
    assert f"RULING{total - 1}a" in later
    assert "RULING0a" not in later


def test_truncated_and_restored_file_redelivers_nothing_old(art):
    (art / "questions.md").write_text("RULING1a (o): one\nRULING1b (o): two\n")
    render(art, "lead", "k1")
    commit(art, "lead", "k1")
    (art / "questions.md").write_text("RULING1a (o): one\n")
    assert "- none" in new_entries(render(art, "lead", "k1"))
    commit(art, "lead", "k1")
    (art / "questions.md").write_text("RULING1a (o): one\nRULING1b (o): two\n")
    assert "- none" in new_entries(render(art, "lead", "k1"))


def test_packet_reports_unreadable_sections(tmp_path):
    packet = render(tmp_path, "orchestrator")
    assert "## ledger: unavailable" in packet
    assert "## questions: unavailable" in packet


def test_commit_covers_only_the_acknowledged_packet(art):
    (art / "questions.md").write_text("RULING1a (o): in A\n")
    render(art, "lead", "A")
    with (art / "questions.md").open("a") as stream:
        stream.write("RULING1b (o): only in B\n")
    assert "RULING1b" in new_entries(render(art, "lead", "B"))
    commit(art, "lead", "A")
    later = new_entries(render(art, "lead", "C"))
    assert "RULING1a" not in later
    assert "RULING1b" in later
    with pytest.raises(ValueError):
        commit(art, "lead", "A")


def test_preview_without_key_records_nothing(art):
    (art / "questions.md").write_text("RULING1a (o): x\n")
    render(art, "lead")
    assert not list((art / "logs").glob("packet-lead-pending-*"))
