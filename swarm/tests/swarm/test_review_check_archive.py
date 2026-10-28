import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
TAG = "unit"
FEATURE = "feature-x"


@pytest.fixture
def review_check(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts/swarm"))
    return importlib.import_module("review_check")


def finding(finding_id: str, text: str, severity: str = "major") -> dict[str, object]:
    return {"id": finding_id, "severity": severity, "text": text}


def archive_review(
    art: Path,
    attempt: int,
    findings: list[dict[str, object]],
    fixed: list[str] | None = None,
) -> None:
    path = art / "attempts" / FEATURE / str(attempt) / f"{TAG}-code-review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    statuses = [{"id": fid, "status": "fixed", "evidence": "fixed"} for fid in fixed or []]
    path.write_text(json.dumps({"findings": findings, "statuses": statuses}))


def run(
    review_check: ModuleType, art: Path, attempt: int, findings: list[dict[str, object]]
) -> None:
    review_check.finding_history(art, TAG, {"feature": FEATURE, "attempt": attempt}, findings)


def test_repeated_archived_review_fixed_by_plain_id_passes(
    review_check: ModuleType, tmp_path: Path
) -> None:
    same = [finding("ITEM-01", "same text")]
    for attempt in (1, 2, 3):
        archive_review(tmp_path, attempt, same)
    archive_review(tmp_path, 4, [], fixed=["ITEM-01"])
    run(review_check, tmp_path, 5, [])


def test_fixed_finding_is_not_required_again_alongside_new_finding(
    review_check: ModuleType, tmp_path: Path
) -> None:
    same = [finding("ITEM-01", "same text")]
    for attempt in (1, 2, 3):
        archive_review(tmp_path, attempt, same)
    archive_review(tmp_path, 4, [finding("NEW-01", "new", "minor")], fixed=["ITEM-01"])
    current = [{"mode": "code", **finding("NEW-01", "new", "minor")}]
    run(review_check, tmp_path, 5, current)


def test_same_id_with_different_text_stays_ambiguous(
    review_check: ModuleType, tmp_path: Path
) -> None:
    archive_review(tmp_path, 1, [finding("ITEM-01", "first text")])
    archive_review(tmp_path, 2, [finding("ITEM-01", "second text")])
    archive_review(tmp_path, 3, [], fixed=["ITEM-01"])
    with pytest.raises(ValueError, match="Ambiguous archived finding id"):
        run(review_check, tmp_path, 4, [])


def test_unfixed_repeated_finding_omitted_is_refused(
    review_check: ModuleType, tmp_path: Path
) -> None:
    same = [finding("ITEM-01", "same text")]
    for attempt in (1, 2, 3):
        archive_review(tmp_path, attempt, same)
    with pytest.raises(ValueError, match="Archived finding omitted or changed"):
        run(review_check, tmp_path, 4, [])


@pytest.mark.parametrize("view", [None, "wide", [100], [100, "tall"], {"w": 1}])
def test_malformed_view_entry_refuses_finding_box(review_check: ModuleType, view: object) -> None:
    review = {"findings": [{"severity": "minor", "box": [1, 1, 2, 2], "view": "v1"}]}
    with pytest.raises(ValueError, match="Finding box exceeds evidence view"):
        review_check.check_review_findings(review, {"views": {"v1": view}})
