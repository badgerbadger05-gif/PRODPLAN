[CmdletBinding()]
param(
    [ValidateSet("start", "verify", "start-verify", "stop")]
    [string]$Action = "start-verify"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
$composeFile = Join-Path $repoRoot "docker-compose.r2.yml"
$project = "prodplan-r2-local"

function Invoke-R2Compose {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & docker compose --project-name $project --file $composeFile @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "R2 Docker compose command failed with exit code $LASTEXITCODE. Docker daemon must be available; no production fallback is allowed."
    }
}

function Start-R2 {
    Invoke-R2Compose @("up", "--detach", "--wait", "r2-postgres")
}

function Verify-R2 {
    Invoke-R2Compose @("exec", "--no-TTY", "r2-postgres", "pg_isready", "-U", "r2_user", "-d", "prodplan_r2")
    Write-Output "R2 PostgreSQL is local-only: 127.0.0.1:55441/prodplan_r2 (project $project)."
}

switch ($Action) {
    "start" { Start-R2 }
    "verify" { Verify-R2 }
    "start-verify" { Start-R2; Verify-R2 }
    "stop" { Invoke-R2Compose @("stop", "r2-postgres") }
}
