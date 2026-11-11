"""XYR Shield — private data API + shielded (commit-reveal) trade log + no-repaint R oracle on NEAR."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("SHIELD_DATA", ROOT / "data"))   # one data dir per strategy/instance
