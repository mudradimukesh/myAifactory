from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_pilot_lead_has_only_the_reviewer_delegate():
    text = (ROOT / ".claude/agents/p1-pilot-lead.md").read_text()
    assert "model: claude-opus-5-5" in text
    assert "effort: medium" in text
    assert "Agent(p1-pilot-reviewer)" in text
    assert "$LEAD_BRIEF" in text
    assert "p1-planner" not in text
    assert "p1-architect" not in text


def test_reviewer_requires_separate_modes_and_missing_evidence_failure():
    text = (ROOT / ".claude/agents/p1-pilot-reviewer.md").read_text()
    for required in [
        "model: claude-opus-5-5",
        "effort: medium",
        "maxTurns: 40",
        "<tag>-code-review.json",
        "<tag>-product-review.json",
        "coverage: missing",
        "merge: false",
        "not_assessed",
        "<tag>-test-map.json",
        "<tag>-open-findings.json",
    ]:
        assert required in text
