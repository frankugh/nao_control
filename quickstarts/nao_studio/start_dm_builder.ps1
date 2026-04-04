Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptPath = $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $ScriptPath))
$PythonExe = Join-Path $RepoRoot "py3_dialog_manager\venv\Scripts\python.exe"
$ServerScript = Join-Path $RepoRoot "py3_dialog_manager\webapp_server.py"
$AgentPresetsPath = Join-Path $RepoRoot "py3_dialog_manager\configs\agent_presets.json"
$PresetPortsPath = Join-Path (Split-Path -Parent $ScriptPath) "preset_ports.local.json"
$DefaultPreset = "virtuele_robot"
$FallbackPort = "5301"
$NewPresetDefaultPort = "8080"

function Get-StartupPresetIds {
    if (-not (Test-Path -LiteralPath $AgentPresetsPath)) {
        return @()
    }
    $payload = Get-Content -LiteralPath $AgentPresetsPath -Raw | ConvertFrom-Json
    if ($null -eq $payload.presets) {
        return @()
    }
    return @(
        $payload.presets |
            Where-Object { $_.startup_allowed -eq $true } |
            ForEach-Object { [string]$_.id } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
}

function Get-PresetPortMap {
    if (-not (Test-Path -LiteralPath $PresetPortsPath)) {
        return @{}
    }
    try {
        $payload = Get-Content -LiteralPath $PresetPortsPath -Raw | ConvertFrom-Json
    } catch {
        Write-Host "[dm-start] Kon preset-poortstate niet lezen; gebruik defaults."
        return @{}
    }
    $result = @{}
    foreach ($entry in $payload.PSObject.Properties) {
        $presetId = [string]$entry.Name
        $port = [string]$entry.Value
        if (-not [string]::IsNullOrWhiteSpace($presetId) -and $port -match '^\d+$') {
            $result[$presetId] = $port
        }
    }
    return $result
}

function Save-PresetPort {
    param(
        [string]$PresetId,
        [string]$Port
    )

    if ([string]::IsNullOrWhiteSpace($PresetId)) {
        return
    }
    $presetPorts = Get-PresetPortMap
    $presetPorts[$PresetId] = [string]$Port
    $presetPorts |
        ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath $PresetPortsPath -Encoding UTF8
}

function Read-PresetSelection {
    param(
        [string[]]$PresetIds
    )

    while ($true) {
        $raw = Read-Host "Wil je een preset gebruiken? [$DefaultPreset] ([l] om lijst van opties te zien)"
        $value = $raw.Trim()
        if ([string]::IsNullOrWhiteSpace($value)) {
            return $DefaultPreset
        }
        $normalized = $value.ToLowerInvariant()
        if ($normalized -eq "l") {
            if ($PresetIds.Count -eq 0) {
                Write-Host "[dm-start] Geen startup presets gevonden."
            } else {
                Write-Host "[dm-start] Beschikbare presets: $($PresetIds -join ', ')"
                Write-Host "[dm-start] Gebruik 'none' of 'geen' om zonder preset te starten."
            }
            continue
        }
        if ($normalized -in @("none", "geen")) {
            return ""
        }
        if ($PresetIds -contains $value) {
            return $value
        }
        Write-Host "[dm-start] Startup preset niet gevonden. Gebruik 'l' voor de lijst."
    }
}

function Read-PortSelection {
    param(
        [string]$PresetId,
        [hashtable]$PresetPorts
    )

    $defaultPort = $FallbackPort
    if (-not [string]::IsNullOrWhiteSpace($PresetId)) {
        $defaultPort = $NewPresetDefaultPort
        if ($PresetPorts.ContainsKey($PresetId)) {
            $defaultPort = [string]$PresetPorts[$PresetId]
        }
    }

    while ($true) {
        $raw = Read-Host "Welke port wil je gebruiken? (hou leeg voor default) [$defaultPort]"
        $value = $raw.Trim()
        if ([string]::IsNullOrWhiteSpace($value)) {
            return $defaultPort
        }
        try {
            $port = [int]$value
            if ($port -gt 0 -and $port -le 65535) {
                return [string]$port
            }
        } catch {
        }
        Write-Host "[dm-start] Ongeldige poort: $value"
    }
}

function Read-OpenBrowser {
    while ($true) {
        $raw = Read-Host "Wil je de front-end automatisch openen? [N/y]"
        $value = $raw.Trim().ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($value) -or $value -in @("n", "no", "nee")) {
            return $false
        }
        if ($value -in @("y", "yes", "j", "ja")) {
            return $true
        }
        Write-Host "[dm-start] Vul y of n in."
    }
}

function Start-BrowserDelayed {
    param(
        [string]$Port
    )

    $url = "http://127.0.0.1:$Port/"
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "Start-Sleep -Seconds 2; Start-Process '$url'"
    ) | Out-Null
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Host "[dm-start] Python venv niet gevonden: $PythonExe"
    throw "py3_dialog_manager venv ontbreekt."
}

if (-not (Test-Path -LiteralPath $ServerScript)) {
    Write-Host "[dm-start] Server script niet gevonden: $ServerScript"
    throw "webapp_server.py ontbreekt."
}

$presetIds = Get-StartupPresetIds
$presetPorts = Get-PresetPortMap
$preset = Read-PresetSelection -PresetIds $presetIds
$port = Read-PortSelection -PresetId $preset -PresetPorts $presetPorts
$openBrowser = Read-OpenBrowser

$commandArgs = @($ServerScript, "--host", "127.0.0.1", "--port", $port)
if (-not [string]::IsNullOrWhiteSpace($preset)) {
    $commandArgs += @("--preset", $preset)
}

Save-PresetPort -PresetId $preset -Port $port

Write-Host "[dm-start] Command: `"$PythonExe`" $($commandArgs -join ' ')"
if ($openBrowser) {
    Start-BrowserDelayed -Port $port
}

& $PythonExe @commandArgs
$exitCode = $LASTEXITCODE

Read-Host "Druk op Enter om dit venster te sluiten"
exit $exitCode
