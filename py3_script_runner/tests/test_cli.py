from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from py3_script_runner import cli


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


def test_cli_reports_preflight_failure_message(capsys, monkeypatch):
    script = {
        "version": 1,
        "robots": {"nao1": {"dm_url": "http://127.0.0.1:5301"}},
        "defaults": {"request_timeout_s": 12, "on_error": "prompt"},
        "ppt": {
            "enabled": True,
            "file": "C:/slides/demo.pptx",
            "fullscreen_required": True,
            "start_capture_on_run": True,
        },
        "steps": [
            {
                "id": "s1",
                "robot_id": "nao1",
                "start": {"mode": "manual"},
                "action": {"type": "say", "text": "hello"},
            }
        ],
    }

    class FakeRunner:
        def __init__(self, script_data):
            self.script_data = script_data

        def preflight(self):
            raise RuntimeError("PPT feature requires Windows + PowerPoint COM")

        def run(self):
            raise AssertionError("run should not be called when preflight fails")

    monkeypatch.setattr("py3_script_runner.cli.load_script", lambda _path: script)
    monkeypatch.setattr("py3_script_runner.cli.ScriptRunner", FakeRunner)

    code = cli.main(["--script", "unused.json"])
    captured = capsys.readouterr()
    assert code == 3
    assert "deprecated" in captured.err.lower()
    assert "Preflight failed: PPT feature requires Windows + PowerPoint COM" in captured.err
