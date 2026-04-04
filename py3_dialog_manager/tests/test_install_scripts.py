from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_install_repo_wrapper_points_to_powershell_entrypoint() -> None:
    wrapper = _read("quickstarts/install/install_repo.bat")

    assert 'set "SCRIPT_PATH=%~dp0install_repo.ps1"' in wrapper
    assert 'powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_PATH%"' in wrapper


def test_install_repo_script_contains_profile_flow_and_package_targets() -> None:
    script = _read("quickstarts/install/install_repo.ps1")

    assert 'Wat wil je doen? [installeren / alleen verifieren / alleen credentials bijwerken]' in script
    assert 'Ugh!' in script
    assert 'Welk profiel wil je installeren? [gebruiker / ontwikkelaar]' in script
    assert 'Mooi! Dan installeren we alleen de modules die nodig zijn om de applicatie te draaien.' in script
    assert 'OK! Dan installeren we alles. Zowel de runtime als de aanvullende tooling om te testen en verder te ontwikkelen.' in script
    assert '$DmDir = Join-Path $RepoRoot "py3_dialog_manager"' in script
    assert '$SrDir = Join-Path $RepoRoot "py3_script_runner"' in script
    assert '$BmDir = Join-Path $RepoRoot "py3_nao_behavior_manager"' in script
    assert '$BaseDir = Join-Path $RepoRoot "py2_nao_base_controller"' in script
    assert '$StoryDir = Join-Path $RepoRoot "py3_story_engine"' in script
    assert '$CmdRecDir = Join-Path $RepoRoot "py3_command_recognition_train"' in script
    assert 'install_paths.local.json' in script
    assert 'runtime-only/runtime+tests/tests-only/command-recognition-training/credentials bijwerken/alles verifieren' not in script
    assert 'command-recognition-training' not in script


def test_install_repo_script_handles_python_detection_credentials_and_models() -> None:
    script = _read("quickstarts/install/install_repo.ps1")

    assert 'Get-PythonFromLauncher -PyArg "-3"' in script or '-LauncherArg "-3"' in script
    assert 'Get-PythonFromLauncher -PyArg "-2"' in script or '-LauncherArg "-2"' in script
    assert '[Environment]::SetEnvironmentVariable($Name, $targetValue, "User")' in script
    assert "Ik zie dat nog niet alle omgevingsvariabelen op de computer staan ingesteld." in script
    assert "Zonder die variabelen werken de cloud services niet." in script
    assert "Als je deze tooling hebt ontvangen van mij dan heb ik je hier instructies over gegeven." in script
    assert "Wil je Azure keys aangeven via dit script?" in script
    assert "Wil je Ollama keys aangeven via dit script?" in script
    assert 'AZURE_SPEECH_KEY' in script
    assert 'OLLAMA_API_KEY' in script
    assert 'OLLAMA_HOST is optioneel' in script
    assert 'OLLAMA_HOST (optioneel; leeg = standaard host gebruiken)' in script
    assert 'https://portal.azure.com/' in script
    assert 'https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-recognize-speech' in script
    assert 'https://ollama.com/settings/keys' in script
    assert 'https://docs.ollama.com/api/authentication' in script
    assert 'https://docs.ollama.com/api/introduction' in script
    assert 'Wil je uitleg over Azure cloud services?' in script
    assert 'Wil je uitleg over Ollama cloud?' in script
    assert 'Wil je uitleg over Ollama?' in script
    assert 'Wil je Ollama installeren?' in script
    assert 'Get-Command "winget"' in script
    assert '--id Ollama.Ollama' in script
    assert 'download_piper_voices.py' in script
    assert 'download_vosk_models.ps1' in script
    assert 'vosk-model-small-nl-0.22' in script
    assert 'Ensure-VoskModels' in script
    assert 'ollama pull' in script
    assert 'gemma:2b' in script
    assert 'granite3.2:8b' in script
    assert 'Install-CmdRecPackage -Python3Path $Python3Path -IncludeTests:$IncludeTests' in script


def test_install_repo_script_keeps_cmdrec_runtime_and_developer_tests_split() -> None:
    script = _read("quickstarts/install/install_repo.ps1")

    assert 'Install-CmdRecPackage' in script
    assert 'if ($IncludeTests) {' in script
    assert 'Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "-e", ".[test]")' in script
    assert 'Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "-e", ".")' in script
    assert 'Python 2.7 is verplicht voor deze runtime-install.' in script
    assert 'function Configure-Credentials {' in script
    assert 'Apply-CredentialPlan' not in script
    assert 'Collect-CredentialPlan' not in script


def test_install_repo_script_uses_compact_verify_summary() -> None:
    script = _read("quickstarts/install/install_repo.ps1")

    assert "Er missen nog libraries of lokale modellen:" in script
    assert "Er missen nog cloud variabelen:" in script
    assert "Er missen nog Ollama onderdelen:" in script
    assert "OLLAMA_HOST niet gezet; standaard host wordt gebruikt." in script
    assert 'Wil je ook de missende paden zien?' in script
    assert "Missende paden:" in script
    assert "Alles lijkt geinstalleerd en geconfigureerd." in script


def test_gitignore_ignores_installer_local_state() -> None:
    gitignore = _read(".gitignore")

    assert "quickstarts/install/install_paths.local.json" in gitignore
    assert "quickstarts/dm/preset_ports.local.json" in gitignore
