from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NotRequired, TypedDict


class MapEntry(TypedDict):
    oracle: NotRequired[str]
    id: str
    runner: str
    test_node: NotRequired[str]
    test_nodes: NotRequired[list[str]]
    projects: NotRequired[list[str]]
    preservation: NotRequired[bool]
    expected_failure: NotRequired[str]


class BrowserError(TypedDict, total=False):
    message: str


class BrowserResult(TypedDict):
    status: str
    errors: NotRequired[list[BrowserError]]


class BrowserTest(TypedDict):
    projectName: str
    results: NotRequired[list[BrowserResult]]


class BrowserSpec(TypedDict):
    title: str
    file: NotRequired[str]
    tests: NotRequired[list[BrowserTest]]


class BrowserSuite(TypedDict, total=False):
    file: str
    specs: list[BrowserSpec]
    suites: list[BrowserSuite]
    errors: list[BrowserError]


def browser_rows(document: BrowserSuite) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def visit(suite: BrowserSuite) -> None:
        for spec in suite.get("specs", []):
            file = spec.get("file", suite.get("file", ""))
            for test in spec.get("tests", []):
                for result in test.get("results", []):
                    errors = result.get("errors", [])
                    rows.append(
                        {
                            "node": file + "::" + spec["title"],
                            "project": test["projectName"],
                            "status": result["status"],
                            "error": "\n".join(e.get("message", "") for e in errors),
                        }
                    )
        for child in suite.get("suites", []):
            visit(child)

    for suite in document.get("suites", []):
        visit(suite)
    if document.get("errors"):
        raise ValueError("Browser runner infrastructure errors")
    return rows


def python_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for case in ET.parse(path).iter("testcase"):
        error = case.find("error")
        failure = case.find("failure")
        skipped = case.find("skipped")
        status = (
            "error"
            if error is not None
            else "failed"
            if failure is not None
            else "skipped"
            if skipped is not None
            else "passed"
        )
        detail = error if error is not None else failure
        file = case.get("file") or case.get("classname", "").replace(".", "/") + ".py"
        rows.append(
            {
                "node": file + "::" + case.get("name", ""),
                "project": "pytest",
                "status": status,
                "error": ""
                if detail is None
                else (detail.get("message", "") + "\n" + (detail.text or "")),
            }
        )
    return rows


def mapped_nodes(item: MapEntry) -> list[str]:
    nodes = item.get("test_nodes", [item["test_node"]] if "test_node" in item else [])
    if not nodes or len(nodes) != len(set(nodes)) or ("test_nodes" in item and "test_node" in item):
        raise ValueError("Mapped test nodes must be nonempty and unique")
    return nodes


def light_mapping(mapping: list[MapEntry]) -> list[MapEntry]:
    result = []
    for item in mapping:
        if item["runner"] == "playwright":
            projects = [p for p in item.get("projects", []) if not p.startswith("dark-")]
            if not projects:
                raise ValueError(item["id"] + " has no light-theme acceptance project")
            item = {**item, "projects": projects}
        result.append(item)
    return result


def check(
    mapping: list[MapEntry],
    ids: list[str],
    reports: dict[str, list[dict[str, str]]],
    repeats: int = 1,
) -> None:
    if (
        len(ids) != len(set(ids))
        or len(mapping) != len(ids)
        or {m["id"] for m in mapping} != set(ids)
    ):
        raise ValueError("Acceptance map has missing, unknown or duplicate ids")
    for item in mapping:
        if item["oracle"] in {"from_code_under_test", "none"}:
            raise ValueError("Oracle must be independent")
        for node in mapped_nodes(item):
            rows = [r for r in reports[item["runner"]] if r["node"] == node]
            projects = item.get("projects", ["pytest"])
            if {r["project"] for r in rows} != set(projects):
                raise ValueError(item["id"] + " missing or unexpected projects")
            for project in projects:
                selected = [r for r in rows if r["project"] == project]
                if len(selected) != repeats or any(r["status"] != "passed" for r in selected):
                    raise ValueError(item["id"] + " did not pass every repetition")


def assertion_red(row: dict[str, str], expected: str | None) -> bool:
    text = re.sub(r"\[[0-?]*[ -/]*[@-~]", "", row.get("error", ""))
    setup = re.search(
        "ImportError|ModuleNotFoundError|SyntaxError|fixture .* not fou"
        "nd|browserType.launch|ECONNREFUSED|Test timeout",
        text,
    )
    return bool(
        row["status"] == "failed"
        and not setup
        and expected
        and expected in text
        and (
            re.search(r"AssertionError|assert |expect\(", text)
            or (
                row.get("project") == "pytest"
                and text.startswith("Failed: DID NOT RAISE ValueError\n")
                and re.search(r"^E[ \t]+Failed: DID NOT RAISE ValueError$", text, re.MULTILINE)
            )
        )
    )


def retroactive_red(row: dict[str, str], expected: str | None) -> bool:
    """Red for tests written with their code (RULING494e): the expected failure of any kind."""
    text = re.sub(r"\[[0-?]*[ -/]*[@-~]", "", row.get("error", ""))
    return bool(row["status"] in ("failed", "error") and expected and expected in text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", type=Path, required=True)
    parser.add_argument("--ids", type=Path, required=True)
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--playwright", type=Path)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    reports = {}
    try:
        if args.junit:
            reports["pytest"] = python_rows(args.junit)
        if args.playwright:
            reports["playwright"] = browser_rows(json.loads(args.playwright.read_text()))
        check(
            light_mapping(json.loads(args.map.read_text())),
            args.ids.read_text().splitlines(),
            reports,
            args.repeats,
        )
    except (ValueError, KeyError, OSError, ET.ParseError) as error:
        parser.exit(1, "FAIL: " + str(error) + "\n")
    print("PASS")


if __name__ == "__main__":
    main()
