"""Exercise the command with a fake WezTerm server, without closing user tabs."""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/swarm/close_stale_panes.py"


def test_cli_preserves_live_and_unregistered_panes(tmp_path):
    run = {
        "active_lead": {"pane": 2},
        "draining_leads": {"drain": {"lead": {"pane": 3}}},
        "drain_history": [
            {"lead": {"pane": p}, "retired_at": "done", "receipt": "receipt"} for p in (0, 2, 3, 4)
        ],
        "inventory": {
            "threads": [
                {"role": "orchestrator", "pane_id": 5, "complete": True},
                {"role": "orchestrator", "pane_id": 6},
            ]
        },
    }
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(run))
    panes_path = tmp_path / "panes.json"
    panes_path.write_text(
        json.dumps(
            [
                {
                    "pane_id": p,
                    "tab_id": p,
                    "title": "editor",
                    "cwd": "file:///repo/FloorInteriorDesignAndRenderer",
                }
                for p in (0, 2, 3, 4, 5, 6, 99)
            ]
        )
    )
    fake = tmp_path / "wezterm"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
p = Path(os.environ['TEST_PANES'])
rows = json.loads(p.read_text())
if sys.argv[2] == 'list':
    print(json.dumps(rows))
else:
    target = int(sys.argv[-1])
    p.write_text(json.dumps([r for r in rows if r['pane_id'] != target]))
""")
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "WEZTERM_PANE": "4",
        "TEST_PANES": str(panes_path),
    }

    def call(*args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--run", str(run_path), *args],
            env=env,
            capture_output=True,
            text=True,
        )

    preview = call()
    assert preview.returncode == 0, preview.stderr
    assert "would close pane 0" in preview.stdout
    assert "would close pane 5" in preview.stdout
    assert "pane 99" not in preview.stdout
    assert len(json.loads(panes_path.read_text())) == 7
    refused = call("--close", "--pane-id", "2")
    assert refused.returncode == 1
    assert len(json.loads(panes_path.read_text())) == 7
    closed = call("--close")
    assert closed.returncode == 0, closed.stderr
    assert {r["pane_id"] for r in json.loads(panes_path.read_text())} == {2, 3, 4, 6, 99}
    assert call("--close").stdout == "No eligible retired panes are open.\n"
