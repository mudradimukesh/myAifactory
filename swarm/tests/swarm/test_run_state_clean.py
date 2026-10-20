import re
from pathlib import Path


def test_run_state_has_no_typing_escapes_or_comments() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "scripts/swarm/run_state.py").read_text(encoding="utf-8")

    assert not re.search(r"\bAny\b", source)
    assert not re.search(r"\bcast\s*\(", source)
    assert not re.search(r"type:\s*ignore", source)
    for number, line in enumerate(source.splitlines()):
        assert not line.lstrip().startswith("#") or (number == 0 and line.startswith("#!"))
