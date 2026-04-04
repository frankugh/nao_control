from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_fixed_dm_quickstarts_use_preset_port_and_open_browser():
    alex = _read("quickstarts/dm/start_dm_alex.bat")
    renee = _read("quickstarts/dm/start_dm_renee.bat")
    virtual_robot = _read("quickstarts/dm/start_dm_virtuele_robot.bat")

    assert "--port 5301 --preset alex" in alex
    assert "Start-Process 'http://127.0.0.1:5301/'" in alex
    assert "py3_dialog_manager\\webapp_server.py" in alex

    assert "--port 5302 --preset renee" in renee
    assert "Start-Process 'http://127.0.0.1:5302/'" in renee

    assert "--port 5303 --preset virtuele_robot" in virtual_robot
    assert "Start-Process 'http://127.0.0.1:5303/'" in virtual_robot


def test_dm_builder_supports_default_preset_listing_and_explicit_no_preset():
    wrapper = _read("quickstarts/dm/start_dm_builder.bat")
    script = _read("quickstarts/dm/start_dm_builder.ps1")

    assert 'set "SCRIPT_PATH=%~dp0start_dm_builder.ps1"' in wrapper
    assert '$DefaultPreset = "virtuele_robot"' in script
    assert '$PresetPortsPath = Join-Path (Split-Path -Parent $ScriptPath) "preset_ports.local.json"' in script
    assert 'Read-Host "Wil je een preset gebruiken? [$DefaultPreset] ([l] om lijst van opties te zien)"' in script
    assert '$normalized -eq "l"' in script
    assert '$normalized -in @("none", "geen")' in script
    assert '$NewPresetDefaultPort = "8080"' in script
    assert 'function Get-PresetPortMap {' in script
    assert 'function Save-PresetPort {' in script
    assert '$port = Read-PortSelection -PresetId $preset -PresetPorts $presetPorts' in script
    assert 'Save-PresetPort -PresetId $preset -Port $port' in script
    assert script.index('Save-PresetPort -PresetId $preset -Port $port') < script.index('& $PythonExe @commandArgs')
    assert 'Read-Host "Wil je de front-end automatisch openen? [N/y]"' in script
    assert '$commandArgs += @("--preset", $preset)' in script


def test_script_runner_and_test_service_quickstarts_point_to_expected_entrypoints():
    script_builder = _read("quickstarts/script_runner/start_script_builder.bat")
    example_summary = _read("quickstarts/script_runner/start_example_workshop_summary.bat")
    demo_gate_bat = _read("quickstarts/test_scripts/start_demo_gate.bat")
    demo_gate = _read("quickstarts/test_scripts/start_demo_gate.ps1")
    perf_runner = _read("quickstarts/test_scripts/run_perf_tests.bat")
    all_services = _read("quickstarts/services/start_all_services.bat")
    base_controller = _read("quickstarts/services/start_base_controller.bat")
    behavior_manager = _read("quickstarts/services/start_behavior_manager.bat")

    assert "py3_script_runner\\venv\\Scripts\\python.exe py3_script_runner\\script_runner_app.py %*" in script_builder
    assert '"%PYTHON_EXE%" -m py3_script_runner.cli --script "%SCRIPT_PATH%" %*' in example_summary
    assert "py3_script_runner\\scripts\\example_workshop_summary.json" in example_summary
    assert 'set "SCRIPT_PATH=%~dp0start_demo_gate.ps1"' in demo_gate_bat
    assert 'Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $ScriptPath))' in demo_gate
    assert 'set "BASELINE_FILE=py3_dialog_manager\\logs\\perf\\baseline_run_id.txt"' in perf_runner
    assert 'set "SET_BASELINE_SCRIPT=py3_dialog_manager\\scripts\\perf_set_baseline.py"' in perf_runner
    assert 'set "COMPARE_SCRIPT=py3_dialog_manager\\scripts\\perf_compare_latest.py"' in perf_runner
    assert 'Welke tests wil je draaien? [all/dm/sr/perf] [all]:' in perf_runner
    assert 'if /I "!TEST_SUITE!"=="all" (' in perf_runner
    assert 'set "RUN_DM=1"' in perf_runner
    assert 'set "RUN_SR=1"' in perf_runner
    assert 'if /I "!TEST_SUITE!"=="dm" (' in perf_runner
    assert 'if /I "!TEST_SUITE!"=="sr" (' in perf_runner
    assert 'if /I "!TEST_SUITE!"=="perf" (' in perf_runner
    assert 'set "RUN_PERF_ANALYSIS=1"' in perf_runner
    assert '"%DM_PYTHON_EXE%" -m pytest %DM_TEST_TARGET% -ra --durations=15 %*' in perf_runner
    assert '"%SR_PYTHON_EXE%" -m pytest %SR_TEST_TARGET% -ra --durations=15 %*' in perf_runner
    assert 'Hoe wil je perf draaien? [auto/tests/baseline/compare] [auto]:' in perf_runner
    assert 'if /I "!PERF_MODE!"=="tests" (' in perf_runner
    assert 'if /I "!PERF_MODE!"=="baseline" (' in perf_runner
    assert ') else if /I "!PERF_MODE!"=="compare" (' in perf_runner
    assert '"%DM_PYTHON_EXE%" -m pytest %DM_TEST_TARGET% -m perf -ra --durations=15 %*' in perf_runner
    assert 'if exist "%BASELINE_FILE%" (' in perf_runner
    assert '"%DM_PYTHON_EXE%" %COMPARE_SCRIPT%' in perf_runner
    assert '"%DM_PYTHON_EXE%" %SET_BASELINE_SCRIPT%' in perf_runner

    assert 'set "BASE_PORT=5101"' in base_controller
    assert "py2_nao_base_controller\\nao_api.py --host 127.0.0.1 --port %BASE_PORT%" in base_controller

    assert 'set "BEHAVIOR_PORT=5201"' in behavior_manager
    assert "py3_nao_behavior_manager\\py3_server.py --host 127.0.0.1 --port %BEHAVIOR_PORT%" in behavior_manager
    assert 'start "NAO Py2 Base Controller" cmd /k "%~dp0start_base_controller.bat"' in all_services
    assert 'start "NAO Py3 Behavior Manager" cmd /k "%~dp0start_behavior_manager.bat"' in all_services


def test_old_dialog_manager_launcher_outside_quickstarts_is_removed():
    assert not (REPO_ROOT / "py3_dialog_manager" / "start_dialog_manager.bat").exists()
    assert not (REPO_ROOT / "start_demo_gate.bat").exists()
    assert not (REPO_ROOT / "run_dialog_perf_tests.bat").exists()
    assert not (REPO_ROOT / "start_example_workshop_summary.bat").exists()
    assert not (REPO_ROOT / "start_servers.bat").exists()
