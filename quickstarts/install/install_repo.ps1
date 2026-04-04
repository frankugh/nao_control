Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptPath = $MyInvocation.MyCommand.Path
$ScriptDir = Split-Path -Parent $ScriptPath
$RepoRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$PathCachePath = Join-Path $ScriptDir "install_paths.local.json"

$DmDir = Join-Path $RepoRoot "py3_dialog_manager"
$SrDir = Join-Path $RepoRoot "py3_script_runner"
$BmDir = Join-Path $RepoRoot "py3_nao_behavior_manager"
$BaseDir = Join-Path $RepoRoot "py2_nao_base_controller"
$StoryDir = Join-Path $RepoRoot "py3_story_engine"
$CmdRecDir = Join-Path $RepoRoot "py3_command_recognition_train"

$PiperModelsDir = Join-Path $RepoRoot "piper_tts_models"
$PiperDownloader = Join-Path $PiperModelsDir "download_piper_voices.py"
$PiperDefaultVoice = Join-Path $PiperModelsDir "nl\nl_BE\nathalie\medium\nl_BE-nathalie-medium.onnx"
$VoskModelsDir = Join-Path $RepoRoot "models"
$VoskDownloader = Join-Path $RepoRoot "scripts\download_vosk_models.ps1"
$VoskDefaultModel = Join-Path $VoskModelsDir "vosk-model-small-nl-0.22"
$OllamaModels = @("gemma:2b", "granite3.2:8b")
$AzureEnvVarNames = @("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION")
$OllamaCloudRequiredEnvVarNames = @("OLLAMA_API_KEY")
$OllamaCloudOptionalEnvVarNames = @("OLLAMA_HOST")
$EnvVarNames = @($AzureEnvVarNames + $OllamaCloudRequiredEnvVarNames + $OllamaCloudOptionalEnvVarNames)

function Read-MenuChoice {
    param(
        [string]$Prompt,
        [string]$DefaultValue,
        [hashtable]$Aliases
    )

    while ($true) {
        $raw = Read-Host "$Prompt [$DefaultValue]"
        $value = $raw.Trim()
        if ([string]::IsNullOrWhiteSpace($value)) {
            return $DefaultValue
        }
        $normalized = $value.ToLowerInvariant()
        if ($Aliases.ContainsKey($normalized)) {
            return [string]$Aliases[$normalized]
        }
        Write-Host "[install] Ongeldige keuze."
    }
}

function Read-YesNo {
    param(
        [string]$Prompt,
        [bool]$DefaultYes = $false
    )

    $suffix = "[Y/n]"
    if (-not $DefaultYes) {
        $suffix = "[N/y]"
    }
    while ($true) {
        $raw = Read-Host "$Prompt $suffix"
        $value = $raw.Trim().ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($value)) {
            return $DefaultYes
        }
        if ($value -in @("y", "yes", "j", "ja")) {
            return $true
        }
        if ($value -in @("n", "no", "nee")) {
            return $false
        }
        Write-Host "[install] Vul y of n in."
    }
}

function Get-PathCache {
    if (-not (Test-Path -LiteralPath $PathCachePath)) {
        return @{}
    }
    try {
        $payload = Get-Content -LiteralPath $PathCachePath -Raw | ConvertFrom-Json
    } catch {
        Write-Host "[install] Kon install_paths.local.json niet lezen; cache wordt genegeerd."
        return @{}
    }
    $result = @{}
    foreach ($entry in $payload.PSObject.Properties) {
        $result[[string]$entry.Name] = [string]$entry.Value
    }
    return $result
}

function Save-PathCache {
    param([hashtable]$Cache)

    $Cache |
        ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath $PathCachePath -Encoding UTF8
}

function Test-PythonInterpreter {
    param(
        [string]$PythonPath,
        [int]$ExpectedMajor,
        [int]$ExpectedMinor
    )

    if ([string]::IsNullOrWhiteSpace($PythonPath) -or -not (Test-Path -LiteralPath $PythonPath)) {
        return $false
    }

    try {
        $code = "import sys; raise SystemExit(0 if ((sys.version_info[0] == $ExpectedMajor) and (sys.version_info[1] >= $ExpectedMinor)) else 1)"
        & $PythonPath -c $code | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Get-PythonFromLauncher {
    param(
        [string]$PyArg,
        [int]$ExpectedMajor,
        [int]$ExpectedMinor
    )

    try {
        $candidate = (& py $PyArg -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1).Trim()
    } catch {
        return ""
    }
    if (Test-PythonInterpreter -PythonPath $candidate -ExpectedMajor $ExpectedMajor -ExpectedMinor $ExpectedMinor) {
        return $candidate
    }
    return ""
}

function Get-PythonFromCommand {
    param(
        [string]$CommandName,
        [int]$ExpectedMajor,
        [int]$ExpectedMinor
    )

    $cmd = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        return ""
    }
    $candidate = [string]$cmd.Source
    if (Test-PythonInterpreter -PythonPath $candidate -ExpectedMajor $ExpectedMajor -ExpectedMinor $ExpectedMinor) {
        return $candidate
    }
    return ""
}

function Resolve-PythonInterpreter {
    param(
        [string]$CacheKey,
        [int]$ExpectedMajor,
        [int]$ExpectedMinor,
        [string]$LauncherArg,
        [string[]]$CommandNames,
        [string[]]$KnownPaths,
        [string]$MissingMessage
    )

    $cache = Get-PathCache
    if ($cache.ContainsKey($CacheKey)) {
        $cached = [string]$cache[$CacheKey]
        if (Test-PythonInterpreter -PythonPath $cached -ExpectedMajor $ExpectedMajor -ExpectedMinor $ExpectedMinor) {
            Write-Host "[install] Gebruik cached ${CacheKey}: $cached"
            return $cached
        }
    }

    $candidate = Get-PythonFromLauncher -PyArg $LauncherArg -ExpectedMajor $ExpectedMajor -ExpectedMinor $ExpectedMinor
    if (-not [string]::IsNullOrWhiteSpace($candidate)) {
        return $candidate
    }

    foreach ($commandName in $CommandNames) {
        $candidate = Get-PythonFromCommand -CommandName $commandName -ExpectedMajor $ExpectedMajor -ExpectedMinor $ExpectedMinor
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            return $candidate
        }
    }

    foreach ($path in $KnownPaths) {
        if (Test-PythonInterpreter -PythonPath $path -ExpectedMajor $ExpectedMajor -ExpectedMinor $ExpectedMinor) {
            return $path
        }
    }

    Write-Host $MissingMessage
    $manual = Read-Host "Geef een pad naar Python $ExpectedMajor.$ExpectedMinor+ of laat leeg om over te slaan"
    $manualPath = $manual.Trim()
    if ([string]::IsNullOrWhiteSpace($manualPath)) {
        return ""
    }
    if (-not (Test-PythonInterpreter -PythonPath $manualPath -ExpectedMajor $ExpectedMajor -ExpectedMinor $ExpectedMinor)) {
        throw "Ongeldig Python-pad voor $CacheKey`: $manualPath"
    }

    $cache[$CacheKey] = $manualPath
    Save-PathCache -Cache $cache
    return $manualPath
}

function Ensure-Py3Venv {
    param(
        [string]$ProjectDir,
        [string]$Python3Path
    )

    $venvDir = Join-Path $ProjectDir "venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        Write-Host "[install] Gebruik bestaande venv: $venvDir"
        return $venvPython
    }
    Write-Host "[install] Maak Py3 venv: $venvDir"
    & $Python3Path -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Py3 venv aanmaken faalde: $venvDir"
    }
    return $venvPython
}

function Ensure-Py2Venv {
    param(
        [string]$ProjectDir,
        [string]$Python2Path
    )

    if ([string]::IsNullOrWhiteSpace($Python2Path)) {
        throw "Python 2.7 is verplicht voor de base controller runtime."
    }

    $venvDir = Join-Path $ProjectDir "venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        Write-Host "[install] Gebruik bestaande Py2 venv: $venvDir"
        return $venvPython
    }
    Write-Host "[install] Maak Py2 venv: $venvDir"
    try {
        & $Python2Path -m virtualenv $venvDir
    } catch {
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        Write-Host "[install] virtualenv ontbreekt mogelijk in Python2; probeer installatie..."
        & $Python2Path -m pip install virtualenv
        if ($LASTEXITCODE -ne 0) {
            throw "Kon virtualenv niet installeren met $Python2Path"
        }
        & $Python2Path -m virtualenv $venvDir
        if ($LASTEXITCODE -ne 0) {
            throw "Py2 venv aanmaken faalde: $venvDir"
        }
    }
    return $venvPython
}

function Invoke-PipInstall {
    param(
        [string]$PythonPath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )

    Push-Location $WorkingDirectory
    try {
        & $PythonPath -m pip @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "pip faalde in $WorkingDirectory met args: $($Arguments -join ' ')"
        }
    } finally {
        Pop-Location
    }
}

function Install-Py3ProjectFromRequirements {
    param(
        [string]$ProjectDir,
        [string]$Python3Path,
        [bool]$IncludeTests
    )

    $venvPython = Ensure-Py3Venv -ProjectDir $ProjectDir -Python3Path $Python3Path
    Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "--upgrade", "pip") -WorkingDirectory $ProjectDir
    Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "-r", "requirements.txt") -WorkingDirectory $ProjectDir
    if ($IncludeTests) {
        Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "pytest") -WorkingDirectory $ProjectDir
    }
    return $venvPython
}

function Install-StoryEngine {
    param(
        [string]$Python3Path,
        [bool]$IncludeTests
    )

    $venvPython = Ensure-Py3Venv -ProjectDir $StoryDir -Python3Path $Python3Path
    Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "--upgrade", "pip") -WorkingDirectory $StoryDir
    Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "-e", ".") -WorkingDirectory $StoryDir
    if ($IncludeTests -and (Test-Path -LiteralPath (Join-Path $StoryDir "requirements.txt"))) {
        Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "-r", "requirements.txt") -WorkingDirectory $StoryDir
    }
}

function Install-CmdRecPackage {
    param(
        [string]$Python3Path,
        [bool]$IncludeTests
    )

    $venvPython = Ensure-Py3Venv -ProjectDir $CmdRecDir -Python3Path $Python3Path
    Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "--upgrade", "pip") -WorkingDirectory $CmdRecDir
    if ($IncludeTests) {
        Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "-e", ".[test]") -WorkingDirectory $CmdRecDir
    } else {
        Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "-e", ".") -WorkingDirectory $CmdRecDir
    }
}

function Install-BaseController {
    param([string]$Python2Path)

    $venvPython = Ensure-Py2Venv -ProjectDir $BaseDir -Python2Path $Python2Path
    Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "--upgrade", "pip") -WorkingDirectory $BaseDir
    Invoke-PipInstall -PythonPath $venvPython -Arguments @("install", "-r", "requirements.txt") -WorkingDirectory $BaseDir
}

function Set-UserEnvValue {
    param(
        [string]$Name,
        [string]$Value
    )

    $targetValue = $Value
    if ([string]::IsNullOrWhiteSpace($targetValue)) {
        $targetValue = $null
    }
    [Environment]::SetEnvironmentVariable($Name, $targetValue, "User")
    if ($null -eq $targetValue) {
        Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
    } else {
        Set-Item "Env:$Name" $targetValue
    }
}

function Read-PlainSecret {
    param([string]$Prompt)

    $secure = Read-Host $Prompt -AsSecureString
    return [System.Net.NetworkCredential]::new("", $secure).Password
}

function Get-EffectiveEnvValue {
    param([string]$Name)

    $userValue = [Environment]::GetEnvironmentVariable($Name, "User")
    if (-not [string]::IsNullOrWhiteSpace($userValue)) {
        return $userValue
    }
    $machineValue = [Environment]::GetEnvironmentVariable($Name, "Machine")
    if (-not [string]::IsNullOrWhiteSpace($machineValue)) {
        return $machineValue
    }
    return ""
}

function Test-EnvGroupComplete {
    param([string[]]$Names)

    foreach ($name in $Names) {
        if ([string]::IsNullOrWhiteSpace((Get-EffectiveEnvValue -Name $name))) {
            return $false
        }
    }
    return $true
}

function Show-InstallWelcome {
    Write-Host "Ugh!"
    Write-Host "Welkom bij de installer voor NAO Studio. Een set tools om interactie te hebben met een fysieke NAO v5 robot of de virtuele avatar daarvan."
    Write-Host ""
    Write-Host "Om te weten hoe we alles goed installeren eerst even wat vragen."
    Write-Host ""
}

function Show-ProfileMessage {
    param([string]$Profile)

    if ($Profile -eq "gebruiker") {
        Write-Host "Mooi! Dan installeren we alleen de modules die nodig zijn om de applicatie te draaien."
    } else {
        Write-Host "OK! Dan installeren we alles. Zowel de runtime als de aanvullende tooling om te testen en verder te ontwikkelen."
    }
    Write-Host ""
}

function Show-OllamaExplainer {
    Write-Host ""
    Write-Host "Ollama is tooling om lokale AI modellen op je eigen computer te draaien."
    Write-Host "Als je alleen via cloud werkt is Ollama optioneel."
    Write-Host "Voor lokale modellen en voor lokale modelkeuze in de UI is de Ollama CLI wel nodig."
    Write-Host "Handmatig installeren kan via: https://ollama.com/download"
    Write-Host ""
}

function Show-AzureExplainer {
    Write-Host ""
    Write-Host "Azure cloud services worden hier gebruikt voor cloud speech."
    Write-Host "Daarvoor heb je minimaal een Speech key en region nodig."
    Write-Host "Maak of open een Speech resource in de Azure portal: https://portal.azure.com/"
    Write-Host "Open daar je Speech resource en ga naar 'Keys and Endpoint'."
    Write-Host "Daar vind je je key; je region is de region van die resource."
    Write-Host "Officiele uitleg: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-recognize-speech"
    Write-Host "Deze installer zet die waarden alleen in je Windows User env vars."
    Write-Host ""
}

function Show-OllamaCloudExplainer {
    Write-Host ""
    Write-Host "Ollama cloud gebruikt OLLAMA_API_KEY om een remote Ollama endpoint te bereiken."
    Write-Host "OLLAMA_HOST is optioneel en alleen nodig als je niet de standaard host gebruikt."
    Write-Host "Maak een API key aan in je Ollama account: https://ollama.com/settings/keys"
    Write-Host "Officiele authenticatie-uitleg: https://docs.ollama.com/api/authentication"
    Write-Host "Officiele API/base-url uitleg: https://docs.ollama.com/api/introduction"
    Write-Host "Dat staat los van de lokale Ollama CLI."
    Write-Host "Deze installer zet die waarden alleen in je Windows User env vars."
    Write-Host ""
}

function Show-CredentialsIntro {
    Write-Host ""
    Write-Host "Ik zie dat nog niet alle omgevingsvariabelen op de computer staan ingesteld."
    Write-Host "Zonder die variabelen werken de cloud services niet."
    Write-Host "Als je deze tooling hebt ontvangen van mij dan heb ik je hier instructies over gegeven."
    Write-Host "Als je deze software van git hebt dan moet je zelf bij ollama en azure de juiste accounts aanmaken."
    Write-Host "Je kunt de keys zelf in je OS variabelen zetten of via dit script, dan wordt het automatisch erin gezet."
    Write-Host "Geen zorgen het wordt nergens anders gezet, die keys blijven van jou en jou alleen."
    Write-Host ""
}

function Resolve-OllamaCommand {
    $ollamaCmd = Get-Command "ollama" -ErrorAction SilentlyContinue
    if ($null -eq $ollamaCmd) {
        return ""
    }
    return [string]$ollamaCmd.Source
}

function Ensure-PiperVoice {
    param([string]$DmVenvPython)

    if (Test-Path -LiteralPath $PiperDefaultVoice) {
        Write-Host "[install] Piper voice al aanwezig: $PiperDefaultVoice"
        return
    }
    if (-not (Test-Path -LiteralPath $PiperDownloader)) {
        Write-Host "[install] Piper downloader ontbreekt: $PiperDownloader"
        return
    }

    try {
        Invoke-PipInstall -PythonPath $DmVenvPython -Arguments @("install", "huggingface_hub") -WorkingDirectory $DmDir
        Push-Location $PiperModelsDir
        try {
            & $DmVenvPython $PiperDownloader
            if ($LASTEXITCODE -ne 0) {
                throw "download_piper_voices.py faalde"
            }
        } finally {
            Pop-Location
        }
    } catch {
        Write-Host "[install] Piper voice download faalde: $($_.Exception.Message)"
        return
    }

    if (Test-Path -LiteralPath $PiperDefaultVoice) {
        Write-Host "[install] Piper voice download klaar."
    } else {
        Write-Host "[install] Piper voice is na download nog niet gevonden: $PiperDefaultVoice"
    }
}

function Ensure-VoskModels {
    if (Test-Path -LiteralPath $VoskDefaultModel) {
        Write-Host "[install] Vosk NL model al aanwezig: $VoskDefaultModel"
        return
    }
    if (-not (Test-Path -LiteralPath $VoskDownloader)) {
        Write-Host "[install] Vosk downloader ontbreekt: $VoskDownloader"
        return
    }

    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $VoskDownloader -ModelsDir $VoskModelsDir
        if ($LASTEXITCODE -ne 0) {
            throw "download_vosk_models.ps1 faalde met exitcode $LASTEXITCODE"
        }
    } catch {
        Write-Host "[install] Vosk model download faalde: $($_.Exception.Message)"
        return
    }

    if (Test-Path -LiteralPath $VoskDefaultModel) {
        Write-Host "[install] Vosk model download klaar."
    } else {
        Write-Host "[install] Vosk model is na download nog niet gevonden: $VoskDefaultModel"
    }
}

function Ensure-OllamaCli {
    $ollamaSource = Resolve-OllamaCommand
    if (-not [string]::IsNullOrWhiteSpace($ollamaSource)) {
        Write-Host "[install] Ollama CLI gevonden: $ollamaSource"
        return $ollamaSource
    }

    $wingetCmd = Get-Command "winget" -ErrorAction SilentlyContinue
    if ($null -eq $wingetCmd) {
        Write-Host "[install] winget niet gevonden; automatische Ollama install wordt overgeslagen."
    } else {
        Write-Host "[install] Probeer Ollama te installeren via winget..."
        & $wingetCmd.Source install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[install] winget install voor Ollama faalde."
        }
    }

    Start-Sleep -Seconds 2
    $ollamaSource = Resolve-OllamaCommand
    if (-not [string]::IsNullOrWhiteSpace($ollamaSource)) {
        Write-Host "[install] Ollama CLI gevonden na install: $ollamaSource"
        return $ollamaSource
    }

    Write-Host ""
    Write-Host "Ollama is nog niet gevonden."
    Write-Host "Installeer Ollama handmatig via https://ollama.com/download."
    Write-Host "Als de installer Ollama daarna nog niet ziet, sluit dit venster en start de installer opnieuw."
    [void](Read-Host "Druk op ENTER om door te gaan")

    $ollamaSource = Resolve-OllamaCommand
    if (-not [string]::IsNullOrWhiteSpace($ollamaSource)) {
        Write-Host "[install] Ollama CLI gevonden na handmatige stap: $ollamaSource"
    } else {
        Write-Host "[install] Ollama CLI nog steeds niet gevonden."
    }
    return $ollamaSource
}

function Ensure-OllamaModels {
    param(
        [string]$OllamaCommand,
        [string[]]$Models
    )

    if ([string]::IsNullOrWhiteSpace($OllamaCommand)) {
        Write-Host "[install] Ollama CLI niet gevonden; lokale modellen worden overgeslagen."
        return
    }
    if ($null -eq $Models -or $Models.Count -eq 0) {
        Write-Host "[install] Geen ontbrekende Ollama modellen te installeren."
        return
    }

    foreach ($model in $Models) {
        Write-Host "[install] ollama pull $model"
        & $OllamaCommand pull $model
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[install] ollama pull faalde voor model: $model"
        }
    }
}

function Test-OllamaModelInstalled {
    param(
        [string]$OllamaCommand,
        [string]$Model
    )

    if ([string]::IsNullOrWhiteSpace($OllamaCommand)) {
        return $false
    }

    try {
        $lines = @(& $OllamaCommand list 2>$null)
    } catch {
        return $false
    }
    foreach ($line in $lines) {
        $trimmed = ($line | Out-String).Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed)) {
            continue
        }
        $parts = $trimmed -split "\s+"
        if ($parts.Length -gt 0 -and $parts[0] -eq $Model) {
            return $true
        }
    }
    return $false
}

function Get-MissingOllamaModels {
    param([string]$OllamaCommand)

    if ([string]::IsNullOrWhiteSpace($OllamaCommand)) {
        return @($OllamaModels)
    }

    $missing = @()
    foreach ($model in $OllamaModels) {
        if (-not (Test-OllamaModelInstalled -OllamaCommand $OllamaCommand -Model $model)) {
            $missing += $model
        }
    }
    return @($missing)
}

function Verify-Install {
    param(
        [bool]$ShowMissingPaths = $false
    )

    $missingPackages = @()
    $missingDetails = @()
    $checks = @(
        @{Label = "DM venv"; Name = "py3_dialog_manager"; Path = (Join-Path $DmDir "venv\Scripts\python.exe")},
        @{Label = "SR venv"; Name = "py3_script_runner"; Path = (Join-Path $SrDir "venv\Scripts\python.exe")},
        @{Label = "Behavior manager venv"; Name = "py3_nao_behavior_manager"; Path = (Join-Path $BmDir "venv\Scripts\python.exe")},
        @{Label = "Story engine venv"; Name = "py3_story_engine"; Path = (Join-Path $StoryDir "venv\Scripts\python.exe")},
        @{Label = "Base controller venv"; Name = "py2_nao_base_controller"; Path = (Join-Path $BaseDir "venv\Scripts\python.exe")},
        @{Label = "CmdRec venv"; Name = "py3_command_recognition_train"; Path = (Join-Path $CmdRecDir "venv\Scripts\python.exe")},
        @{Label = "Piper NL voice"; Name = "Piper NL voice"; Path = $PiperDefaultVoice},
        @{Label = "Vosk NL model"; Name = "Vosk NL model"; Path = $VoskDefaultModel}
    )
    foreach ($check in $checks) {
        $path = [string]$check.Path
        if (-not (Test-Path -LiteralPath $path)) {
            $missingPackages += [string]$check.Name
            $missingDetails += "$($check.Label): $path"
        }
    }

    $missingEnv = @()
    foreach ($name in @($AzureEnvVarNames + $OllamaCloudRequiredEnvVarNames)) {
        $userValue = [Environment]::GetEnvironmentVariable($name, "User")
        $machineValue = [Environment]::GetEnvironmentVariable($name, "Machine")
        if ([string]::IsNullOrWhiteSpace($userValue) -and [string]::IsNullOrWhiteSpace($machineValue)) {
            $missingEnv += $name
        }
    }

    $missingOllama = @()
    $ollamaCommand = Resolve-OllamaCommand
    if ([string]::IsNullOrWhiteSpace($ollamaCommand)) {
        $missingOllama += "Ollama CLI"
    } else {
        $missingModels = @(Get-MissingOllamaModels -OllamaCommand $ollamaCommand)
        $missingOllama += @($missingModels)
    }

    if ($missingPackages.Count -eq 0 -and $missingEnv.Count -eq 0 -and $missingOllama.Count -eq 0) {
        Write-Host "[verify] Alles lijkt geinstalleerd en geconfigureerd."
        return
    }

    if ($missingPackages.Count -eq 0) {
        Write-Host "[verify] Libraries en lokale modellen: compleet."
    } else {
        Write-Host "[verify] Er missen nog libraries of lokale modellen: $($missingPackages -join ', ')"
    }

    if ($missingEnv.Count -eq 0) {
        Write-Host "[verify] Cloud variabelen: compleet."
    } else {
        Write-Host "[verify] Er missen nog cloud variabelen: $($missingEnv -join ', ')"
    }

    if ([string]::IsNullOrWhiteSpace((Get-EffectiveEnvValue -Name "OLLAMA_HOST"))) {
        Write-Host "[verify] OLLAMA_HOST niet gezet; standaard host wordt gebruikt."
    }

    if ($missingOllama.Count -eq 0) {
        Write-Host "[verify] Ollama: compleet."
    } else {
        Write-Host "[verify] Er missen nog Ollama onderdelen: $($missingOllama -join ', ')"
    }

    if ($missingDetails.Count -gt 0 -and $ShowMissingPaths) {
        Write-Host "[verify] Missende paden:"
        foreach ($detail in $missingDetails) {
            Write-Host "  - $detail"
        }
    }
}

function Install-Runtime {
    param(
        [bool]$IncludeTests,
        [string]$Python3Path,
        [string]$Python2Path
    )

    Write-Host "[install] Runtime setup start."
    $dmPython = Install-Py3ProjectFromRequirements -ProjectDir $DmDir -Python3Path $Python3Path -IncludeTests:$IncludeTests
    [void](Install-Py3ProjectFromRequirements -ProjectDir $SrDir -Python3Path $Python3Path -IncludeTests:$IncludeTests)
    [void](Install-Py3ProjectFromRequirements -ProjectDir $BmDir -Python3Path $Python3Path -IncludeTests:$IncludeTests)
    Install-StoryEngine -Python3Path $Python3Path -IncludeTests:$IncludeTests
    Install-CmdRecPackage -Python3Path $Python3Path -IncludeTests:$IncludeTests
    Install-BaseController -Python2Path $Python2Path
    Ensure-VoskModels
    Ensure-PiperVoice -DmVenvPython $dmPython
    Write-Host "[install] Runtime setup klaar."
}

function Get-InstallerMode {
    $modeAliases = @{
        "installeren" = "installeren"
        "install" = "installeren"
        "i" = "installeren"
        "alleen verifieren" = "verifieren"
        "verifieren" = "verifieren"
        "verify" = "verifieren"
        "v" = "verifieren"
        "alleen credentials bijwerken" = "credentials"
        "credentials" = "credentials"
        "c" = "credentials"
    }

    return Read-MenuChoice `
        -Prompt "Wat wil je doen? [installeren / alleen verifieren / alleen credentials bijwerken]" `
        -DefaultValue "installeren" `
        -Aliases $modeAliases
}

function Get-InstallProfile {
    $profileAliases = @{
        "gebruiker" = "gebruiker"
        "g" = "gebruiker"
        "ontwikkelaar" = "ontwikkelaar"
        "o" = "ontwikkelaar"
        "developer" = "ontwikkelaar"
        "dev" = "ontwikkelaar"
    }

    return Read-MenuChoice `
        -Prompt "Welk profiel wil je installeren? [gebruiker / ontwikkelaar]" `
        -DefaultValue "gebruiker" `
        -Aliases $profileAliases
}

function Configure-Credentials {
    param([bool]$ForcePrompt = $false)

    $azureComplete = Test-EnvGroupComplete -Names $AzureEnvVarNames
    $ollamaCloudComplete = Test-EnvGroupComplete -Names $OllamaCloudRequiredEnvVarNames
    $showIntro = $ForcePrompt -or (-not $azureComplete) -or (-not $ollamaCloudComplete)
    if ($showIntro) {
        Show-CredentialsIntro
    }

    $configureAzure = $false
    if (-not $azureComplete) {
        if (Read-YesNo -Prompt "Wil je uitleg over Azure cloud services?" -DefaultYes:$false) {
            Show-AzureExplainer
        }
        $configureAzure = Read-YesNo -Prompt "Wil je Azure keys aangeven via dit script?" -DefaultYes:$true
    } elseif ($ForcePrompt -or (Read-YesNo -Prompt "Azure cloud variabelen zijn al aanwezig. Wil je ze aanpassen?" -DefaultYes:$false)) {
        if (Read-YesNo -Prompt "Wil je uitleg over Azure cloud services?" -DefaultYes:$false) {
            Show-AzureExplainer
        }
        $configureAzure = Read-YesNo -Prompt "Wil je Azure keys aangeven via dit script?" -DefaultYes:$true
    }
    if ($configureAzure) {
        $azureSpeechKey = Read-PlainSecret -Prompt "AZURE_SPEECH_KEY"
        $azureSpeechRegion = Read-Host "AZURE_SPEECH_REGION"
        Set-UserEnvValue -Name "AZURE_SPEECH_KEY" -Value $azureSpeechKey
        Set-UserEnvValue -Name "AZURE_SPEECH_REGION" -Value $azureSpeechRegion
        Write-Host "[install] Azure variabelen zijn direct gezet in Windows User env."
    }

    $configureOllamaCloud = $false
    if (-not $ollamaCloudComplete) {
        if (Read-YesNo -Prompt "Wil je uitleg over Ollama cloud?" -DefaultYes:$false) {
            Show-OllamaCloudExplainer
        }
        $configureOllamaCloud = Read-YesNo -Prompt "Wil je Ollama keys aangeven via dit script?" -DefaultYes:$true
    } elseif ($ForcePrompt -or (Read-YesNo -Prompt "Ollama cloud variabelen zijn al aanwezig. Wil je ze aanpassen?" -DefaultYes:$false)) {
        if (Read-YesNo -Prompt "Wil je uitleg over Ollama cloud?" -DefaultYes:$false) {
            Show-OllamaCloudExplainer
        }
        $configureOllamaCloud = Read-YesNo -Prompt "Wil je Ollama keys aangeven via dit script?" -DefaultYes:$true
    }
    if ($configureOllamaCloud) {
        $ollamaApiKey = Read-PlainSecret -Prompt "OLLAMA_API_KEY"
        $ollamaHost = Read-Host "OLLAMA_HOST (optioneel; leeg = standaard host gebruiken)"
        Set-UserEnvValue -Name "OLLAMA_API_KEY" -Value $ollamaApiKey
        Set-UserEnvValue -Name "OLLAMA_HOST" -Value $ollamaHost
        Write-Host "[install] Ollama cloud variabelen zijn direct gezet in Windows User env."
    }
}

function Collect-InstallPlan {
    Write-Host "Om te weten hoe we alles goed installeren eerst even wat vragen."
    Write-Host ""

    $profile = Get-InstallProfile
    Show-ProfileMessage -Profile $profile

    $python3 = Resolve-PythonInterpreter `
        -CacheKey "python3" `
        -ExpectedMajor 3 `
        -ExpectedMinor 10 `
        -LauncherArg "-3" `
        -CommandNames @("python") `
        -KnownPaths @("C:\Python312\python.exe", "C:\Python311\python.exe", "C:\Python310\python.exe") `
        -MissingMessage "[install] Python 3.10+ niet automatisch gevonden. Installeer Python 3 of geef een custom pad op."
    if ([string]::IsNullOrWhiteSpace($python3)) {
        throw "Python 3.10+ is verplicht voor NAO Studio."
    }
    Write-Host "[install] Python3: $python3"

    $python2 = Resolve-PythonInterpreter `
        -CacheKey "python2" `
        -ExpectedMajor 2 `
        -ExpectedMinor 7 `
        -LauncherArg "-2" `
        -CommandNames @("python2") `
        -KnownPaths @("C:\Python27\python.exe") `
        -MissingMessage "[install] Python 2.7 niet automatisch gevonden. py2_nao_base_controller hoort bij de runtime; installeer Python 2.7 of geef een custom pad op."
    if ([string]::IsNullOrWhiteSpace($python2)) {
        throw "Python 2.7 is verplicht voor deze runtime-install."
    }
    Write-Host "[install] Python2: $python2"
    Write-Host ""

    $ollamaCommand = Resolve-OllamaCommand
    $installOllama = $false
    $installOllamaModels = $false
    $ollamaModelsToInstall = @()
    if ([string]::IsNullOrWhiteSpace($ollamaCommand)) {
        Write-Host "Ik zie dat je Ollama nog niet hebt geinstalleerd."
        if (Read-YesNo -Prompt "Wil je uitleg over Ollama?" -DefaultYes:$false) {
            Show-OllamaExplainer
        }
        $installOllama = Read-YesNo -Prompt "Wil je Ollama installeren?" -DefaultYes:$true
        if ($installOllama) {
            $ollamaModelsToInstall = @($OllamaModels)
            $installOllamaModels = Read-YesNo -Prompt "Wil je de lokale modellen installeren? ($($ollamaModelsToInstall -join ', '))" -DefaultYes:$true
        }
    } else {
        Write-Host "[install] Ollama CLI gevonden: $ollamaCommand"
        $ollamaModelsToInstall = @(Get-MissingOllamaModels -OllamaCommand $ollamaCommand)
        if ($ollamaModelsToInstall.Count -eq 0) {
            Write-Host "[install] Lokale Ollama modellen zijn al aanwezig: $($OllamaModels -join ', ')"
        } else {
            $installOllamaModels = Read-YesNo -Prompt "Wil je de ontbrekende lokale modellen installeren? ($($ollamaModelsToInstall -join ', '))" -DefaultYes:$true
        }
    }
    Write-Host ""

    return [ordered]@{
        Profile = $profile
        IncludeTests = ($profile -eq "ontwikkelaar")
        Python3 = $python3
        Python2 = $python2
        InstallOllama = $installOllama
        InstallOllamaModels = $installOllamaModels
        OllamaModelsToInstall = @($ollamaModelsToInstall)
    }
}

function Execute-InstallPlan {
    param([hashtable]$Plan)

    Configure-Credentials -ForcePrompt:$false

    Write-Host ""
    Write-Host "We gaan nu alle dependencies installeren, een moment geduld. Rome is ook niet in een dag gebouwd."
    Write-Host ""

    Install-Runtime -IncludeTests:([bool]$Plan.IncludeTests) -Python3Path ([string]$Plan.Python3) -Python2Path ([string]$Plan.Python2)

    $ollamaCommand = Resolve-OllamaCommand
    if ([bool]$Plan.InstallOllama) {
        $ollamaCommand = Ensure-OllamaCli
        if ([bool]$Plan.InstallOllamaModels) {
            $Plan.OllamaModelsToInstall = @(Get-MissingOllamaModels -OllamaCommand $ollamaCommand)
        }
    }
    if ([bool]$Plan.InstallOllamaModels) {
        Ensure-OllamaModels -OllamaCommand $ollamaCommand -Models @($Plan.OllamaModelsToInstall)
    }

    Write-Host ""
    Verify-Install
}

function Run-CredentialsUtility {
    Configure-Credentials -ForcePrompt:$true
}

function Main {
    Write-Host "Ugh!"
    Write-Host "Welkom bij de installer voor NAO Studio. Een set tools om interactie te hebben met een fysieke NAO v5 robot of de virtuele avatar daarvan."
    Write-Host ""

    $mode = Get-InstallerMode
    if ($mode -eq "verifieren") {
        Verify-Install
        if (Read-YesNo -Prompt "Wil je ook de missende paden zien?" -DefaultYes:$false) {
            Verify-Install -ShowMissingPaths $true
        }
        return
    }
    if ($mode -eq "credentials") {
        Run-CredentialsUtility
        return
    }

    $installPlan = Collect-InstallPlan
    Execute-InstallPlan -Plan $installPlan
}

try {
    Main
    Write-Host "[install] Klaar."
    exit 0
} catch {
    Write-Host "[install] FAILED: $($_.Exception.Message)"
    exit 1
}
