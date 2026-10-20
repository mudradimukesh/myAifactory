import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lint():
    sys.path.insert(0, str(ROOT / "scripts/swarm"))
    try:
        spec = importlib.util.spec_from_file_location(
            "diff_lint", ROOT / "scripts/swarm/diff_lint.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.pop(0)


def test_changed_line_mapping_ignores_diff_headers(lint):
    patch = "@@ -1,2 +1,3 @@\n unchanged\n-old\n+new\n+second\n"
    assert lint.added_lines(patch) == {2, 3}


def test_added_bans_and_comments_fail_but_unchanged_lines_do_not(lint):
    text = "from typing import Any\nvalue = 1\n# added comment\n"
    assert lint.python_violations(text, {2}) == []
    assert lint.python_violations(text, {1, 3})
