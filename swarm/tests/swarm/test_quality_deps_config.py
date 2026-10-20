import tomllib
from pathlib import Path


def test_mypy_strict_with_pydantic_plugin_and_mutmut_on_src() -> None:
    path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with open(path, "rb") as stream:
        tool = tomllib.load(stream).get("tool", {})
    mypy: dict[str, object] = tool.get("mypy", {})
    mutmut: dict[str, object] = tool.get("mutmut", {})
    actual: dict[str, object] = {
        "mypy.strict": mypy.get("strict"),
        "mypy.plugins": mypy.get("plugins"),
        "mutmut.source_paths": mutmut.get("source_paths"),
    }
    expected: dict[str, object] = {
        "mypy.strict": True,
        "mypy.plugins": ["pydantic.mypy"],
        "mutmut.source_paths": ["src"],
    }
    assert actual == expected, f"tool config: {actual!r}"
