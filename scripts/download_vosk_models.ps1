param(
    [string]$ModelsDir = "models"
)

$ErrorActionPreference = "Stop"

$models = @(
    @{
        Name = "vosk-model-small-nl-0.22"
        Url  = "https://alphacephei.com/vosk/models/vosk-model-small-nl-0.22.zip"
    },
    @{
        Name = "vosk-model-nl-spraakherkenning-0.6"
        Url  = "https://alphacephei.com/vosk/models/vosk-model-nl-spraakherkenning-0.6.zip"
    },
    @{
        Name = "vosk-model-nl-spraakherkenning-0.6-lgraph"
        Url  = "https://alphacephei.com/vosk/models/vosk-model-nl-spraakherkenning-0.6-lgraph.zip"
    }
)

if (-not (Test-Path -LiteralPath $ModelsDir)) {
    New-Item -ItemType Directory -Path $ModelsDir | Out-Null
}

foreach ($m in $models) {
    $destDir = Join-Path $ModelsDir $m.Name
    if (Test-Path -LiteralPath $destDir) {
        Write-Host "Skip: $($m.Name) bestaat al in $ModelsDir"
        continue
    }

    $zipPath = Join-Path $ModelsDir ($m.Name + ".zip")
    Write-Host "Download: $($m.Name)"
    Invoke-WebRequest -Uri $m.Url -OutFile $zipPath

    Write-Host "Uitpakken: $($m.Name)"
    Expand-Archive -Path $zipPath -DestinationPath $ModelsDir -Force
    Remove-Item -LiteralPath $zipPath -Force
}

Write-Host "Klaar."
