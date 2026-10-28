import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/swarm"))
try:
    from acceptance_check import assertion_red, python_rows
finally:
    sys.path.pop(0)

JUNIT = (
    "<testsuites><testsuite>\n"
    '<testcase classname="tests.parse.test_score_hash_contract" name="test_ha'
    'sh_rejected[missing-automatic]" time="0.005"><failure message="Failed: D'
    "ID NOT RAISE ValueError\">documents = (PosixPath('/private/var/folders/dk"
    "/170xdt3x0gdd1xtt20_x3r740000gn/T/pytest-of-mukeshkumarmudradi/pytest-88"
    "20/test_has...dt3x0gdd1xtt20_x3r740000gn/T/pytest-of-mukeshkumarmudradi/"
    "pytest-8820/test_hash_rejected_missing_aut0/reference.json'))\n"
    "side = 0, value = None\n"
    "\n"
    '    @pytest.mark.parametrize("side", [0, 1], ids=["automatic", "referenc'
    'e"])\n'
    '    @pytest.mark.parametrize("value", [None, "b" * 64], ids=["missing", '
    '"wrong"])\n'
    "    def test_hash_rejected(documents: tuple[Path, Path], side: int, valu"
    "e: str | None) -&gt; None:\n"
    "        path = documents[side]\n"
    "        document = json.loads(path.read_text())\n"
    "        if value is None:\n"
    '            del document["levels"][0]["source"]["sha256"]\n'
    "        else:\n"
    '            document["levels"][0]["source"]["sha256"] = value\n'
    "        parse_plan(document)\n"
    "        path.write_text(json.dumps(document))\n"
    '&gt;       with pytest.raises(ValueError, match="hash"):\n'
    "             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
    "E       Failed: DID NOT RAISE ValueError\n"
    "\n"
    "tests/parse/test_score_hash_contract.py:76: Failed</failure></testcase>\n"
    '<testcase classname="tests.parse.test_score_hash_contract" name="test_ha'
    'sh_rejected[missing-reference]" time="0.007"><failure message="Failed: D'
    "ID NOT RAISE ValueError\">documents = (PosixPath('/private/var/folders/dk"
    "/170xdt3x0gdd1xtt20_x3r740000gn/T/pytest-of-mukeshkumarmudradi/pytest-88"
    "20/test_has...dt3x0gdd1xtt20_x3r740000gn/T/pytest-of-mukeshkumarmudradi/"
    "pytest-8820/test_hash_rejected_missing_ref0/reference.json'))\n"
    "side = 1, value = None\n"
    "\n"
    '    @pytest.mark.parametrize("side", [0, 1], ids=["automatic", "referenc'
    'e"])\n'
    '    @pytest.mark.parametrize("value", [None, "b" * 64], ids=["missing", '
    '"wrong"])\n'
    "    def test_hash_rejected(documents: tuple[Path, Path], side: int, valu"
    "e: str | None) -&gt; None:\n"
    "        path = documents[side]\n"
    "        document = json.loads(path.read_text())\n"
    "        if value is None:\n"
    '            del document["levels"][0]["source"]["sha256"]\n'
    "        else:\n"
    '            document["levels"][0]["source"]["sha256"] = value\n'
    "        parse_plan(document)\n"
    "        path.write_text(json.dumps(document))\n"
    '&gt;       with pytest.raises(ValueError, match="hash"):\n'
    "             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
    "E       Failed: DID NOT RAISE ValueError\n"
    "\n"
    "tests/parse/test_score_hash_contract.py:76: Failed</failure></testcase>\n"
    '<testcase classname="tests.parse.test_score_hash_contract" name="test_ha'
    'sh_rejected[wrong-automatic]" time="0.010"><failure message="Failed: DID'
    " NOT RAISE ValueError\">documents = (PosixPath('/private/var/folders/dk/1"
    "70xdt3x0gdd1xtt20_x3r740000gn/T/pytest-of-mukeshkumarmudradi/pytest-8820"
    "/test_has...dt3x0gdd1xtt20_x3r740000gn/T/pytest-of-mukeshkumarmudradi/py"
    "test-8820/test_hash_rejected_wrong_autom0/reference.json'))\n"
    "side = 0\n"
    "value = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "b'\n"
    "\n"
    '    @pytest.mark.parametrize("side", [0, 1], ids=["automatic", "referenc'
    'e"])\n'
    '    @pytest.mark.parametrize("value", [None, "b" * 64], ids=["missing", '
    '"wrong"])\n'
    "    def test_hash_rejected(documents: tuple[Path, Path], side: int, valu"
    "e: str | None) -&gt; None:\n"
    "        path = documents[side]\n"
    "        document = json.loads(path.read_text())\n"
    "        if value is None:\n"
    '            del document["levels"][0]["source"]["sha256"]\n'
    "        else:\n"
    '            document["levels"][0]["source"]["sha256"] = value\n'
    "        parse_plan(document)\n"
    "        path.write_text(json.dumps(document))\n"
    '&gt;       with pytest.raises(ValueError, match="hash"):\n'
    "             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
    "E       Failed: DID NOT RAISE ValueError\n"
    "\n"
    "tests/parse/test_score_hash_contract.py:76: Failed</failure></testcase>\n"
    '<testcase classname="tests.parse.test_score_hash_contract" name="test_ha'
    'sh_rejected[wrong-reference]" time="0.010"><failure message="Failed: DID'
    " NOT RAISE ValueError\">documents = (PosixPath('/private/var/folders/dk/1"
    "70xdt3x0gdd1xtt20_x3r740000gn/T/pytest-of-mukeshkumarmudradi/pytest-8820"
    "/test_has...dt3x0gdd1xtt20_x3r740000gn/T/pytest-of-mukeshkumarmudradi/py"
    "test-8820/test_hash_rejected_wrong_refer0/reference.json'))\n"
    "side = 1\n"
    "value = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "b'\n"
    "\n"
    '    @pytest.mark.parametrize("side", [0, 1], ids=["automatic", "referenc'
    'e"])\n'
    '    @pytest.mark.parametrize("value", [None, "b" * 64], ids=["missing", '
    '"wrong"])\n'
    "    def test_hash_rejected(documents: tuple[Path, Path], side: int, valu"
    "e: str | None) -&gt; None:\n"
    "        path = documents[side]\n"
    "        document = json.loads(path.read_text())\n"
    "        if value is None:\n"
    '            del document["levels"][0]["source"]["sha256"]\n'
    "        else:\n"
    '            document["levels"][0]["source"]["sha256"] = value\n'
    "        parse_plan(document)\n"
    "        path.write_text(json.dumps(document))\n"
    '&gt;       with pytest.raises(ValueError, match="hash"):\n'
    "             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
    "E       Failed: DID NOT RAISE ValueError\n"
    "\n"
    "tests/parse/test_score_hash_contract.py:76: Failed</failure></testcase>\n"
    "</testsuite></testsuites>"
)
CASES = ["missing-automatic", "missing-reference", "wrong-automatic", "wrong-reference"]
EXPECTED = "Failed: DID NOT RAISE ValueError"


@pytest.fixture
def rows(tmp_path: Path) -> list[dict[str, str]]:
    report = tmp_path / "scorer11.xml"
    report.write_text(JUNIT)
    return python_rows(report)


@pytest.mark.parametrize("case", CASES)
def test_scorer11_raises_failure_is_assertion_red(rows: list[dict[str, str]], case: str) -> None:
    node = "tests/parse/test_score_hash_contract.py::test_hash_rejected[" + case + "]"
    row = next(row for row in rows if row["node"] == node)
    assert assertion_red(row, EXPECTED), (
        "pytest.raises ValueError failure must be assertion-level red"
    )


def test_scorer11_records_cover_every_failure(rows: list[dict[str, str]]) -> None:
    assert [row["node"] for row in rows] == [
        "tests/parse/test_score_hash_contract.py::test_hash_rejected[" + case + "]"
        for case in CASES
    ]
    assert all(row["status"] == "failed" and row["project"] == "pytest" for row in rows)
    assert all(row["error"].startswith(EXPECTED + "\n") for row in rows)
    assert all("test_score_hash_contract.py:76: Failed" in row["error"] for row in rows)


@pytest.mark.parametrize(
    "expected", [None, "", "hash identity mismatch", "DID NOT RAISE TypeError", EXPECTED + "!"]
)
def test_raises_failure_keeps_exact_expected_binding(
    rows: list[dict[str, str]], expected: str | None
) -> None:
    for row in rows:
        assert not assertion_red(row, expected)


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        ("error", EXPECTED, EXPECTED),
        ("error", "AssertionError: setup failed", "setup failed"),
        ("error", "AssertionError: collection failed", "collection failed"),
        ("failed", "ImportError: missing dependency", "missing dependency"),
        ("failed", "ModuleNotFoundError: missing dependency", "missing dependency"),
        ("failed", "SyntaxError: invalid syntax", "invalid syntax"),
        ("failed", "fixture missing not found", "missing"),
        ("failed", "ValueError: runtime failure", "runtime failure"),
        ("failed", "ValueError: " + EXPECTED, EXPECTED),
        ("failed", "PermissionError: access denied", "access denied"),
        ("failed", "Failed: infrastructure unavailable", "infrastructure unavailable"),
        ("failed", "Failed: process exited with code 137", "process exited"),
        ("failed", "Fatal Python error: Segmentation fault", "Segmentation fault"),
        ("failed", "worker crashed", "worker crashed"),
        ("failed", "ECONNREFUSED", "ECONNREFUSED"),
        ("failed", "browserType.launch: unavailable", "unavailable"),
        ("failed", "Test timeout", "Test timeout"),
        ("skipped", EXPECTED, EXPECTED),
        ("passed", EXPECTED, EXPECTED),
        ("crashed", EXPECTED, EXPECTED),
        ("failed", "", EXPECTED),
    ],
    ids=[
        "setup-raises",
        "setup-assertion",
        "collection",
        "import",
        "module-import",
        "syntax",
        "fixture",
        "runtime-value",
        "runtime-value-quoted-raises",
        "permission",
        "generic-failed",
        "process-exit",
        "segfault",
        "worker-crash",
        "connection",
        "browser-launch",
        "timeout",
        "skip",
        "passed",
        "crashed",
        "empty",
    ],
)
def test_non_assertion_failures_stay_rejected(status: str, detail: str, expected: str) -> None:
    assert not assertion_red({"status": status, "error": detail}, expected)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("AssertionError: expected Sign in", "expected Sign in"),
        ("E assert 1 == 2", "1 == 2"),
        ("Error: expect(received).toBe(expected)", "toBe(expected)"),
        (
            "Error: \x1b[2mexpect(\x1b[22mreceived).toBe(expected)\nExpected: \x1b[32m1\x1b[39m",
            "Expected: 1",
        ),
    ],
    ids=["assertion-error", "assert-expression", "playwright", "colored-playwright"],
)
def test_recognized_assertions_stay_red(detail: str, expected: str) -> None:
    row = {"status": "failed", "error": detail}
    assert assertion_red(row, expected)
    assert not assertion_red(row, expected + "!")
