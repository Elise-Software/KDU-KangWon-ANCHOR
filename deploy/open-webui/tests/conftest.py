from __future__ import annotations

import sys
from pathlib import Path


DEPLOY_ROOT = Path(__file__).resolve().parents[1]
P1_API = DEPLOY_ROOT / "p1-api"
BOOTSTRAP = DEPLOY_ROOT / "bootstrap"
for path in (P1_API, BOOTSTRAP):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
