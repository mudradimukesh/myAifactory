import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def test_dev_extra_pins_every_gate_tool_exactly() -> None:
    path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with open(path, "rb") as stream:
        dev: list[str] = tomllib.load(stream)["project"]["optional-dependencies"]["dev"]
    required_names = {
        "diff-cover",
        "hypothesis",
        "jsonschema",
        "mutmut",
        "mypy",
        "pytest-cov",
        "pytest-randomly",
        "pytest-repeat",
    }
    pinned_names: set[str] = set()
    invalid_names: set[str] = set()
    for entry in dev:
        requirement = Requirement(entry)
        name = canonicalize_name(requirement.name)
        specifiers = list(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
            invalid_names.add(name)
            continue
        pinned_names.add(name)
        if name == "mutmut" and Version(specifiers[0].version) < Version("3.8"):
            invalid_names.add(name)
    violations: list[str] = sorted((required_names - pinned_names) | invalid_names)
    assert violations == [], f"pin violations: {violations}"
