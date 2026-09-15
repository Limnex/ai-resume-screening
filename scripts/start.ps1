param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$venvRoot = Join-Path $projectRoot ".venv"
$requirements = Join-Path $projectRoot "src\backend\requirements.txt"
$envFile = Join-Path $projectRoot ".env"
$envExample = Join-Path $projectRoot ".env.example"
$appUrl = "http://127.0.0.1:8000/frontend/index.html"

Set-Location -LiteralPath $projectRoot

function Stop-WithMessage {
    param(
        [string]$Message,
        [int]$Code = 1
    )

    Write-Host "[Failed] $Message" -ForegroundColor Red
    if (-not $Check) {
        Read-Host "Press Enter to exit"
    }
    exit $Code
}

function Test-VenvPython {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        return $false
    }

    try {
        & $venvPython --version *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if (-not (Test-VenvPython)) {
    if (Test-Path -LiteralPath $venvRoot) {
        $invalidVenv = Join-Path $projectRoot (".venv.invalid." + (Get-Date -Format "yyyyMMddHHmmss"))
        Write-Host "[Setup] Existing virtual environment is invalid; preserving it as $(Split-Path -Leaf $invalidVenv)." -ForegroundColor Yellow
        Move-Item -LiteralPath $venvRoot -Destination $invalidVenv
    }
    Write-Host "[Setup] Creating the Python virtual environment..." -ForegroundColor Cyan
    $created = $false
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            & py -3 -m venv .venv
            $created = $LASTEXITCODE -eq 0 -and (Test-VenvPython)
        } catch {
            $created = $false
        }
    }
    if (-not $created) {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python) {
            Stop-WithMessage "Python was not found. Install Python 3.11 or newer first."
        }
        try {
            & python -m venv --clear .venv
            $created = $LASTEXITCODE -eq 0 -and (Test-VenvPython)
        } catch {
            $created = $false
        }
    }
    if (-not $created) {
        Stop-WithMessage "The Python virtual environment could not be created."
    }
}

$dependenciesReady = $false
try {
    & $venvPython -c "import fastapi, uvicorn, httpx, pydantic, dotenv" *> $null
    $dependenciesReady = $LASTEXITCODE -eq 0
} catch {
    $dependenciesReady = $false
}
if (-not $dependenciesReady) {
    Write-Host "[Setup] Installing required packages..." -ForegroundColor Cyan
    & $venvPython -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) {
        Stop-WithMessage "Package installation failed. Check the network and try again."
    }
}

if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath $envExample -Destination $envFile
    Write-Host "[Demo] Created .env with offline demo mode enabled." -ForegroundColor Cyan
}

$modelConfigured = $false
try {
    & $venvPython -c "from src.backend.config import Settings; raise SystemExit(0 if Settings.from_env().llm_configured else 1)" *> $null
    $modelConfigured = $LASTEXITCODE -eq 0
} catch {
    $modelConfigured = $false
}
if (-not $modelConfigured) {
    Write-Host "[Configuration required] Enable DEMO_MODE or complete the real model settings." -ForegroundColor Yellow
    Start-Process -FilePath "notepad.exe" -ArgumentList $envFile
    if (-not $Check) {
        Read-Host "Save the configuration, then press Enter to exit"
    }
    exit 2
}

if ($Check) {
    Write-Host "[OK] Python, dependencies, and demo/model configuration are ready." -ForegroundColor Green
    Write-Host "[URL] $appUrl"
    exit 0
}

Write-Host "[Starting] $appUrl" -ForegroundColor Green
Write-Host "[Stopping] Return to this window and press Ctrl+C."
$browserCommand = "Start-Sleep -Seconds 2; Start-Process '$appUrl'"
Start-Process -FilePath "pwsh.exe" -ArgumentList @(
    "-NoProfile",
    "-WindowStyle",
    "Hidden",
    "-Command",
    $browserCommand
) -WindowStyle Hidden

& $venvPython -m uvicorn src.backend.main:app --host 127.0.0.1 --port 8000 --workers 1
$appExit = $LASTEXITCODE
if ($appExit -ne 0) {
    Write-Host "[Stopped] The service exited with code $appExit." -ForegroundColor Red
    Read-Host "Press Enter to exit"
}
exit $appExit
