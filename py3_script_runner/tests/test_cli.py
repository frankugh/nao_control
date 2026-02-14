from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_cli_direct_execution_works_without_relative_import_error():
    runner_dir = Path(__file__).resolve().parents[1]
    cli_path = runner_dir / "cli.py"

    # Use a missing script on purpose: this should fail with schema error,
    # but must not fail with "attempted relative import with no known parent package".
    proc = subprocess.run(
        [sys.executable, str(cli_path), "--script", "does_not_exist.json"],
        cwd=str(runner_dir),
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert "attempted relative import with no known parent package" not in combined
    assert "Schema error:" in combined
