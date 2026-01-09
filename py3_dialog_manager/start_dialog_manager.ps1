Write-Host "RUNNING IN: $($Host.Name)"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"
# start_dialog_manager.ps1
$ErrorActionPreference = "Stop"

try {
  Set-Location $PSScriptRoot

  # Kies hier je config  
  $config = "configs\run_laptop_whisper_echo_console.json"
  #$config = "configs\run_laptop_whisper_echo_nao.json"
  #$config = "configs\run_laptop_whisper_ollama_cloud_console.json"
  #$config = "configs\run_laptop_whisper_ollama_cloud_nao.json"
  #$config = "configs\run_laptop_whisper_ollama_local_console.json"
  #$config = "configs\run_laptop_whisper_ollama_local_nao.json"
  

  if (-not (Test-Path "venv\Scripts\python.exe")) {
    Write-Host "[ERROR] venv\Scripts\python.exe niet gevonden. Draai eerst install_behavior_manager.bat"
    exit 1
  }

  # UTF-8 output (emoji)
  [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

  # Imports robuust
  $env:PYTHONPATH = $PSScriptRoot

  #Write-Host "[INFO] Start run_from_json met config=$config"
  #& "$PSScriptRoot\venv\Scripts\python.exe" -m scripts.run_from_json --config $config
  
  Write-Host "[INFO] Start run_from_json met config=$config"
  & "$PSScriptRoot\venv\Scripts\python.exe" -m webapp_server --config $config --host 0.0.0.0 --port 8080
}
catch {
  Write-Host ""
  Write-Host "[ERROR] $($_.Exception.Message)"
  Write-Host $_
}
finally {
  Read-Host "Press Enter to close"
}
