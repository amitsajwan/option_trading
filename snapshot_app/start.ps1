param(
    [string]$Instrument = 'BANKNIFTY26MARFUT',
    [string]$DashboardApiBase = 'http://127.0.0.1:8002',
    [double]$TimeoutSeconds = 15.0,
    [int]$OhlcLimit = 300
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

Push-Location $repoRoot
try {
    python -m snapshot_app.main_live `
        --instrument $Instrument `
        --dashboard-api-base $DashboardApiBase `
        --timeout-seconds $TimeoutSeconds `
        --ohlc-limit $OhlcLimit
} finally {
    Pop-Location
}
