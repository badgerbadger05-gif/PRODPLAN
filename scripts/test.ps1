param(
    [switch]$BackendOnly,
    [switch]$FrontendOnly,
    [switch]$Smoke
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not $FrontendOnly) {
    if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
        $python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
        if (-not (Test-Path $python)) {
            throw "Python 3.11 not found. Install it with: winget install -e --id Python.Python.3.11 --scope user"
        }
        & $python -m venv .venv
    }

    & .\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
    & .\.venv\Scripts\pytest.exe -q
}

if (-not $BackendOnly) {
    Push-Location frontend-erp-shell
    try {
        npm run lint
        npm run build
        if ($Smoke) {
            npm run smoke
        }
    }
    finally {
        Pop-Location
    }
}
