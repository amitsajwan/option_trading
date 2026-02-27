param(
    [double]$HealthTimeoutSeconds = 30.0,
    [double]$HealthMaxAgeSeconds = 900.0
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

Push-Location $repoRoot
try {
    python -m persistence_app.main_snapshot_consumer `
        --health-timeout-seconds $HealthTimeoutSeconds `
        --health-max-age-seconds $HealthMaxAgeSeconds
} finally {
    Pop-Location
}
