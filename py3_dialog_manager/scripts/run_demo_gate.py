from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DM_ROOT = REPO_ROOT / "py3_dialog_manager"
for candidate in (str(REPO_ROOT), str(DM_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import demo_gate


if __name__ == "__main__":
    raise SystemExit(demo_gate.main())
