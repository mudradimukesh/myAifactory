"""Row and check shapes shared by unit_gate and its gate packet."""

from collections.abc import Callable
from typing import NotRequired, TypedDict


class GateRow(TypedDict, total=False):
    id: str
    disposition: str
    comparison: str
    command: list[str] | list[list[str]]
    exit_code: int
    measured: float
    threshold: int
    evidence: str
    duration_seconds: float
    error: str
    errors: list[dict[str, str | int]]
    approval: str
    functional_evidence: str
    failed_test_ids: list[str]
    paths: list[str]


class BaselineRow(TypedDict):
    command: list[str]
    exit_code: int
    disposition: str
    evidence: NotRequired[str]
    known_evidence: NotRequired[str | None]
    duration_seconds: float


Rows = list[GateRow]
Action = Callable[[], GateRow]
Check = tuple[str, Action]
Reports = dict[str, list[dict[str, str]]]
