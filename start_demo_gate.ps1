param(
    [switch]$UseDefaults
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptPath = $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptPath
Set-Location $RepoRoot

$PythonExe = Join-Path $RepoRoot "py3_dialog_manager\venv\Scripts\python.exe"
$DemoGateScript = Join-Path $RepoRoot "py3_dialog_manager\scripts\run_demo_gate.py"
$RuntimePresetDir = Join-Path $RepoRoot "py3_dialog_manager\configs\runtime"
$SummaryPresetsPath = Join-Path $RepoRoot "py3_dialog_manager\configs\summary_presets.json"

$ScenarioOptions = @(
    [pscustomobject]@{
        Key = "all"
        Scenario = "all"
        Description = "Meest complete controle voor een demo: doorloopt de volledige demo en test ook herstel bij storingen."
    },
    [pscustomobject]@{
        Key = "chat"
        Scenario = "happy_path_dialog"
        Description = "Test het gewone gesprek: luisteren, antwoorden en spraakcommando's."
    },
    [pscustomobject]@{
        Key = "summary"
        Scenario = "summary_edit_flow"
        Description = "Test de samenvattingsflow: SR start, DM neemt op, transcript wordt bewerkt en de samenvatting wordt afgerond."
    },
    [pscustomobject]@{
        Key = "fallbacks"
        Scenario = "service_loss_recovery"
        Description = "Test uitval van STT, LLM of TTS en controleert of herstel en fallback goed werken."
    },
    [pscustomobject]@{
        Key = "rehearsal"
        Scenario = "full_demo_rehearsal"
        Description = "Test de volledige demo-doorloop: gesprek, samenvatting en workshopscript achter elkaar."
    }
)
$ScenarioChoices = @($ScenarioOptions | ForEach-Object { [string]$_.Key })
$ScenarioLookup = @{}
foreach ($item in $ScenarioOptions) {
    $ScenarioLookup[[string]$item.Key] = [string]$item.Scenario
    $ScenarioLookup[[string]$item.Scenario] = [string]$item.Scenario
}

$RuntimePresetNames = @()
if (Test-Path -LiteralPath $RuntimePresetDir) {
    $RuntimePresetNames = @(
        Get-ChildItem -LiteralPath $RuntimePresetDir -Filter "*.json" |
            Sort-Object Name |
            ForEach-Object { $_.BaseName }
    )
}

$SummaryPresetIds = @()
if (Test-Path -LiteralPath $SummaryPresetsPath) {
    $SummaryPresetPayload = Get-Content -LiteralPath $SummaryPresetsPath -Raw | ConvertFrom-Json
    if ($null -ne $SummaryPresetPayload.presets) {
        $SummaryPresetIds = @($SummaryPresetPayload.presets | ForEach-Object { [string]$_.id } | Where-Object { $_ })
    }
}

function Read-DefaultValue {
    param(
        [string]$Prompt,
        [string]$DefaultValue
    )

    if ($UseDefaults) {
        if ([string]::IsNullOrWhiteSpace($DefaultValue)) {
            Write-Host "[demo-gate-start] $Prompt -> <leeg>"
        } else {
            Write-Host "[demo-gate-start] $Prompt -> $DefaultValue"
        }
        return $DefaultValue
    }

    if ([string]::IsNullOrWhiteSpace($DefaultValue)) {
        $raw = Read-Host $Prompt
    } else {
        $raw = Read-Host "$Prompt [$DefaultValue]"
    }
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $DefaultValue
    }
    return $raw.Trim()
}

function Read-ChoiceValue {
    param(
        [string]$Prompt,
        [string[]]$Choices,
        [string]$DefaultValue
    )

    while ($true) {
        $value = [string](Read-DefaultValue -Prompt $Prompt -DefaultValue $DefaultValue)
        $normalized = $value.Trim().ToLowerInvariant()
        if ($Choices -contains $normalized) {
            return $normalized
        }
        Write-Host "[demo-gate-start] Ongeldige keuze. Kies uit: $($Choices -join ', ')"
    }
}

function Read-ScenarioValue {
    param(
        [string]$DefaultValue
    )

    Write-Host "[demo-gate-start] Scenario kiezen:"
    foreach ($item in $ScenarioOptions) {
        Write-Host ("  - {0,-10} {1}" -f ([string]$item.Key), ([string]$item.Description))
    }
    while ($true) {
        $value = [string](Read-DefaultValue -Prompt "Scenario" -DefaultValue $DefaultValue)
        $normalized = $value.Trim().ToLowerInvariant()
        if ($ScenarioLookup.ContainsKey($normalized)) {
            return [string]$ScenarioLookup[$normalized]
        }
        Write-Host "[demo-gate-start] Ongeldige keuze. Kies uit: $($ScenarioChoices -join ', ')"
    }
}

function Read-YesNoValue {
    param(
        [string]$Prompt,
        [bool]$DefaultValue
    )

    $defaultText = if ($DefaultValue) { "y" } else { "n" }
    while ($true) {
        $raw = [string](Read-DefaultValue -Prompt "$Prompt (y/n)" -DefaultValue $defaultText)
        $normalized = $raw.Trim().ToLowerInvariant()
        if ($normalized -in @("y", "yes", "j", "ja")) {
            return $true
        }
        if ($normalized -in @("n", "no", "nee")) {
            return $false
        }
        Write-Host "[demo-gate-start] Vul y of n in."
    }
}

function Resolve-RepoPath {
    param(
        [string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }

    $trimmed = $Value.Trim()
    if ([System.IO.Path]::IsPathRooted($trimmed)) {
        return [System.IO.Path]::GetFullPath($trimmed)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $trimmed))
}

function Test-RuntimePresetValue {
    param(
        [string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }

    $trimmed = $Value.Trim()
    $directCandidate = Resolve-RepoPath $trimmed
    if ($directCandidate -and (Test-Path -LiteralPath $directCandidate)) {
        return $true
    }

    $withExt = Join-Path $RuntimePresetDir ($trimmed + ".json")
    if (Test-Path -LiteralPath $withExt) {
        return $true
    }

    $named = Join-Path $RuntimePresetDir $trimmed
    if (Test-Path -LiteralPath $named) {
        return $true
    }

    return $false
}

function Read-RuntimePresetValue {
    param(
        [string]$DefaultValue
    )

    while ($true) {
        $value = [string](Read-DefaultValue -Prompt "Runtime preset (naam of pad)" -DefaultValue $DefaultValue)
        if (Test-RuntimePresetValue $value) {
            return $value.Trim()
        }
        Write-Host "[demo-gate-start] Runtime preset niet gevonden. Beschikbaar: $($RuntimePresetNames -join ', ')"
    }
}

function Read-SummaryPresetValue {
    param(
        [string]$DefaultValue
    )

    if ($SummaryPresetIds.Count -eq 0) {
        return ""
    }

    while ($true) {
        $value = [string](Read-DefaultValue -Prompt "Summary preset id" -DefaultValue $DefaultValue)
        $trimmed = $value.Trim()
        if ($SummaryPresetIds -contains $trimmed) {
            return $trimmed
        }
        Write-Host "[demo-gate-start] Summary preset niet gevonden. Beschikbaar: $($SummaryPresetIds -join ', ')"
    }
}

function Read-ExistingPathValue {
    param(
        [string]$Prompt,
        [string]$DefaultValue,
        [bool]$ExpectDirectory = $false
    )

    while ($true) {
        $raw = [string](Read-DefaultValue -Prompt $Prompt -DefaultValue $DefaultValue)
        $resolved = Resolve-RepoPath $raw
        if ([string]::IsNullOrWhiteSpace($resolved)) {
            Write-Host "[demo-gate-start] Pad mag niet leeg zijn."
            continue
        }
        if (-not (Test-Path -LiteralPath $resolved)) {
            Write-Host "[demo-gate-start] Pad niet gevonden: $resolved"
            continue
        }
        if ($ExpectDirectory -and -not (Test-Path -LiteralPath $resolved -PathType Container)) {
            Write-Host "[demo-gate-start] Verwacht een map: $resolved"
            continue
        }
        if (-not $ExpectDirectory -and -not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            Write-Host "[demo-gate-start] Verwacht een bestand: $resolved"
            continue
        }
        return $resolved
    }
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Host "[demo-gate-start] Python venv niet gevonden: $PythonExe"
    throw "py3_dialog_manager venv ontbreekt."
}

if (-not (Test-Path -LiteralPath $DemoGateScript)) {
    Write-Host "[demo-gate-start] Demo gate script niet gevonden: $DemoGateScript"
    throw "run_demo_gate.py ontbreekt."
}

Write-Host "[demo-gate-start] Repo: $RepoRoot"
Write-Host "[demo-gate-start] Scenario's:"
foreach ($item in $ScenarioOptions) {
    Write-Host ("  - {0,-10} {1}" -f ([string]$item.Key), ([string]$item.Description))
}
if ($RuntimePresetNames.Count -gt 0) {
    Write-Host "[demo-gate-start] Runtime presets : $($RuntimePresetNames -join ', ')"
}
if ($SummaryPresetIds.Count -gt 0) {
    Write-Host "[demo-gate-start] Summary presets : $($SummaryPresetIds -join ', ')"
}
Write-Host ""

$defaultSummaryPreset = if ($SummaryPresetIds.Count -gt 0) { $SummaryPresetIds[0] } else { "" }
$defaultSummaryScript = Resolve-RepoPath "py3_script_runner\scripts\demo_gate_summary_single_robot.json"
$defaultWorkshopScript = Resolve-RepoPath "py3_script_runner\scripts\demo_gate_workshop_single_robot.json"
$defaultAudioFixturesRoot = Resolve-RepoPath "py3_dialog_manager\demo_gate_audio"

$runDefault = Read-YesNoValue -Prompt "Run default (all scenarios, zonder services, zonder robot)" -DefaultValue $true

$profile = "offline"
$scenario = "all"
$runtimePreset = "runtime_virtuele_robot"
$summaryPresetId = $defaultSummaryPreset
$summaryScriptPath = $defaultSummaryScript
$workshopScriptPath = $defaultWorkshopScript
$audioFixturesRoot = $defaultAudioFixturesRoot
$naoIp = ""
$keepArtifacts = $false

if (-not $runDefault) {
    $scenario = Read-ScenarioValue -DefaultValue "all"
    $useLiveServices = Read-YesNoValue -Prompt "Echte services gebruiken" -DefaultValue $false
    $useLiveRobot = Read-YesNoValue -Prompt "Echte robot gebruiken" -DefaultValue $false

    if ($useLiveRobot) {
        if (-not $useLiveServices) {
            Write-Host "[demo-gate-start] Echte robot vereist het live_robot profiel; services gaan daarmee ook live."
        }
        $profile = "live_robot"
    } elseif ($useLiveServices) {
        $profile = "live_services"
    } else {
        $profile = "offline"
    }

    $defaultRuntimePreset = if ($profile -eq "live_robot") { "runtime_alex" } else { "runtime_virtuele_robot" }
    $runtimePreset = Read-RuntimePresetValue -DefaultValue $defaultRuntimePreset
    $summaryPresetId = Read-SummaryPresetValue -DefaultValue $defaultSummaryPreset

    $customPaths = Read-YesNoValue -Prompt "Custom script/audio paden instellen" -DefaultValue $false
    if ($customPaths) {
        $summaryScriptPath = Read-ExistingPathValue -Prompt "Summary script pad" -DefaultValue $defaultSummaryScript
        $workshopScriptPath = Read-ExistingPathValue -Prompt "Workshop script pad" -DefaultValue $defaultWorkshopScript
        $audioFixturesRoot = Read-ExistingPathValue -Prompt "Audio fixtures map" -DefaultValue $defaultAudioFixturesRoot -ExpectDirectory $true
    }

    if ($profile -eq "live_robot") {
        $naoIp = [string](Read-DefaultValue -Prompt "NAO IP override (leeg = preset)" -DefaultValue "")
    }

    $keepArtifacts = Read-YesNoValue -Prompt "Artifacts bewaren" -DefaultValue $false
    $runNow = Read-YesNoValue -Prompt "Demo gate nu starten" -DefaultValue $true
    if (-not $runNow) {
        Write-Host "[demo-gate-start] Geannuleerd."
        exit 0
    }
} else {
    Write-Host "[demo-gate-start] Default selectie: profile=offline, scenario=all"
}

$CommandArgs = @(
    "py3_dialog_manager\scripts\run_demo_gate.py",
    "--profile", $profile,
    "--scenario", $scenario,
    "--runtime-preset", $runtimePreset,
    "--summary-script", $summaryScriptPath,
    "--workshop-script", $workshopScriptPath,
    "--audio-fixtures-root", $audioFixturesRoot
)

if (-not [string]::IsNullOrWhiteSpace($summaryPresetId)) {
    $CommandArgs += @("--summary-preset-id", $summaryPresetId)
}
if (-not [string]::IsNullOrWhiteSpace($naoIp)) {
    $CommandArgs += @("--nao-ip", $naoIp.Trim())
}
if ($keepArtifacts) {
    $CommandArgs += "--keep-artifacts"
}

Write-Host ""
Write-Host "[demo-gate-start] Command:"
$renderedArgs = @(
    $CommandArgs | ForEach-Object {
        if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
    }
) -join ' '
Write-Host ('  "' + $PythonExe + '" ' + $renderedArgs)
Write-Host ""

& $PythonExe @CommandArgs
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    throw "Demo gate stopte met exit code $exitCode."
}

exit 0
