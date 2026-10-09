import os
from pathlib import Path

HOME = Path(__file__).resolve().parents[2]
REPO = Path(os.environ.get("REPO", str(Path.cwd()))).resolve()
ART = Path(os.environ.get("ART", str(REPO / ".swarm"))).resolve()
BIN = Path(os.environ.get("BIN", str(HOME / "scripts/swarm"))).resolve()
RUN = Path(os.environ.get("PILOT_RUN", str(ART / "run.json"))).resolve()
STATE = Path(os.environ.get("SWARM_STATE", str(ART / "state.md"))).resolve()
