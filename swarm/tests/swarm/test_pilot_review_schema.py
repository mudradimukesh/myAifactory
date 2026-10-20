import json
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_pilot_schema_matches_proposed_contract_verbatim():
    roles = (ROOT / "prompts/phase-1/roles.md").read_text()
    expected = (
        roles.split("Proposed `review-schema.json` v2", 1)[1]
        .split("```json\n", 1)[1]
        .split("```", 1)[0]
    )
    assert (ROOT / "scripts/swarm/review-schema.json").read_text() == expected


def test_new_review_valid_and_old_review_rejected():
    schema = json.loads((ROOT / "scripts/swarm/review-schema.json").read_text())
    review = {
        "schema_version": "p1-review-2",
        "mode": "code",
        "unit": "fixture",
        "base_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "statuses": [],
        "acceptance": [],
        "checks": [],
        "mutants": [],
        "findings": [],
        "needs_ruling": None,
        "merge": True,
    }
    jsonschema.validate(review, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"merge": True, "findings": []}, schema)
