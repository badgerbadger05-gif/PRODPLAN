[CmdletBinding()]
param(
    [ValidateSet("start", "verify", "start-verify", "stop")]
    [string]$Action = "start-verify",
    [string]$Distro = "Ubuntu"
)

$ErrorActionPreference = "Stop"
$database = "prodplan_r2"
$user = "r2_user"
$password = "r2_local_only"
$port = 55441
$pgVersion = 16
$runtimeBase = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { [Environment]::GetFolderPath("LocalApplicationData") }
$runtimeRoot = Join-Path $runtimeBase "PRODPLAN\r2-runtime"
$statePath = Join-Path $runtimeRoot "wsl-keeper.json"

function Invoke-R2Wsl {
    param([Parameter(Mandatory = $true)][string]$Command)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Command))
    & wsl.exe -d $Distro -u root -- bash -lc "echo $encoded | base64 -d | bash -s"
    if ($LASTEXITCODE -ne 0) {
        throw "R2 WSL command failed with exit code $LASTEXITCODE. Check the named Ubuntu dependency and local cluster."
    }
}

function Expand-R2Command {
    param([Parameter(Mandatory = $true)][string]$Command)
    return $Command.Replace("__VERSION__", "$pgVersion").Replace("__PORT__", "$port").Replace("__DATABASE__", $database).Replace("__USER__", $user).Replace("__PASSWORD__", $password)
}

$dependencyCheck = @'
set -eu
for command_name in psql pg_lsclusters pg_createcluster pg_ctlcluster runuser; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "R2 WSL dependency missing: $command_name; install it outside the normal start command." >&2
    exit 64
  }
done
'@

$clusterStart = @'
set -eu
cluster_line=$(pg_lsclusters --no-header | awk '$1 == "__VERSION__" && $3 == "__PORT__" {print}')
if [ -z "$cluster_line" ]; then
  pg_createcluster __VERSION__ r2 --port __PORT__ --start >/dev/null
else
  cluster_name=$(printf '%s\n' "$cluster_line" | awk '{print $2}')
  cluster_status=$(printf '%s\n' "$cluster_line" | awk '{print $4}')
  if [ "$cluster_status" != "online" ]; then
    pg_ctlcluster __VERSION__ "$cluster_name" start
  fi
fi
'@

$provisionIdentity = @'
set -eu
runuser -u postgres -- psql -p __PORT__ -d postgres -v ON_ERROR_STOP=1 -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '__USER__') THEN CREATE ROLE __USER__ LOGIN PASSWORD '__PASSWORD__'; ELSE ALTER ROLE __USER__ LOGIN PASSWORD '__PASSWORD__'; END IF; END \$\$;" >/dev/null
if ! runuser -u postgres -- psql -p __PORT__ -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname = '__DATABASE__'" | grep -qx 1; then
  runuser -u postgres -- createdb -p __PORT__ -O __USER__ __DATABASE__
fi
'@

$verifyIdentity = @'
set -eu
export PGPASSWORD='__PASSWORD__'
identity=$(psql -h 127.0.0.1 -p __PORT__ -U __USER__ -d __DATABASE__ -Atqc "SELECT current_database() || '|' || current_user || '|' || COALESCE(host(inet_server_addr()), '')")
test "$identity" = '__DATABASE__|__USER__|127.0.0.1' || {
  echo "R2 identity mismatch: $identity" >&2
  exit 65
}
echo "$identity"
'@

$stopCluster = @'
set -eu
cluster_name=$(pg_lsclusters --no-header | awk '$1 == "__VERSION__" && $3 == "__PORT__" {print $2; exit}')
test -n "$cluster_name" || { echo "R2 cluster on port __PORT__ is not present" >&2; exit 66; }
pg_ctlcluster __VERSION__ "$cluster_name" stop
'@

function Read-R2KeeperState {
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    } catch {
        throw "R2 keeper state is unreadable at $statePath; refusing to guess a PID."
    }
}

function Get-CheckedR2Keeper {
    $state = Read-R2KeeperState
    if ($null -eq $state) {
        throw "R2 WSL keeper is not registered at $statePath."
    }
    if ([string]$state.distro -ne $Distro -or [string]$state.command -ne "tail -f /dev/null") {
        throw "R2 keeper identity mismatch; refusing to manage an unknown process."
    }
    try {
        $process = Get-Process -Id ([int]$state.pid) -ErrorAction Stop
    } catch {
        throw "R2 keeper PID $($state.pid) is not running; refusing to infer a replacement."
    }
    if ($process.ProcessName -notin @("wsl", "wslhost")) {
        throw "R2 keeper PID $($state.pid) is not a WSL process; refusing to stop it."
    }
    $actualStartUtc = $process.StartTime.ToUniversalTime()
    $savedStartUtc = ([datetime]$state.start_utc).ToUniversalTime()
    if ($savedStartUtc.Ticks -ne $actualStartUtc.Ticks) {
        throw "R2 keeper PID $($state.pid) start identity differs; refusing to stop a reused PID."
    }
    return $process
}

function Start-R2Keeper {
    New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
    $existing = Read-R2KeeperState
    if ($null -ne $existing) {
        try {
            return Get-CheckedR2Keeper
        } catch {
            try { Get-Process -Id ([int]$existing.pid) -ErrorAction Stop | Out-Null; throw } catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
                Remove-Item -LiteralPath $statePath -Force
            }
        }
    }
    $keeper = Start-Process -FilePath "wsl.exe" -ArgumentList @("-d", $Distro, "-u", "root", "--exec", "tail", "-f", "/dev/null") -WindowStyle Hidden -PassThru
    Start-Sleep -Milliseconds 500
    $keeper.Refresh()
    if ($keeper.HasExited) {
        throw "R2 WSL keeper exited immediately with code $($keeper.ExitCode)."
    }
    $state = [ordered]@{
        pid = [int]$keeper.Id
        start_utc = $keeper.StartTime.ToUniversalTime().ToString("o")
        distro = $Distro
        command = "tail -f /dev/null"
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8NoBOM
    return Get-CheckedR2Keeper
}

function Stop-R2Keeper {
    $keeper = Get-CheckedR2Keeper
    Stop-Process -Id $keeper.Id -Force -ErrorAction Stop
    Remove-Item -LiteralPath $statePath -Force
}

function Test-R2WslDependencies {
    Invoke-R2Wsl $dependencyCheck
}

function Verify-R2Wsl {
    $null = Get-CheckedR2Keeper
    Test-R2WslDependencies
    Invoke-R2Wsl (Expand-R2Command $verifyIdentity)
    Write-Output "R2 WSL PostgreSQL identity verified: 127.0.0.1:$port/$database."
}

function Start-R2Wsl {
    $null = Start-R2Keeper
    Test-R2WslDependencies
    try {
        Invoke-R2Wsl (Expand-R2Command $verifyIdentity)
        Write-Output "R2 WSL PostgreSQL identity verified: 127.0.0.1:$port/$database."
        return
    } catch {
        # Existing cluster is not ready; provision/start only the named local contour.
    }
    Invoke-R2Wsl (Expand-R2Command $clusterStart)
    Invoke-R2Wsl (Expand-R2Command $provisionIdentity)
    Invoke-R2Wsl (Expand-R2Command $verifyIdentity)
    Write-Output "R2 WSL PostgreSQL is local-only: 127.0.0.1:$port/$database (PostgreSQL $pgVersion, distro $Distro)."
}

function Stop-R2Wsl {
    $null = Get-CheckedR2Keeper
    Test-R2WslDependencies
    Invoke-R2Wsl (Expand-R2Command $stopCluster)
    Stop-R2Keeper
}

switch ($Action) {
    "start" { Start-R2Wsl }
    "verify" { Verify-R2Wsl }
    "start-verify" { Start-R2Wsl }
    "stop" { Stop-R2Wsl }
}
