param(
    [ValidateSet('live', 'historical', 'mock')]
    [string]$Mode = 'live',
    [switch]$StartCollectors = $true,
    [string]$ApiBase = 'http://127.0.0.1:8004',
    [double]$HealthTimeoutSeconds = 30.0
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

Push-Location $repoRoot
try {
    $args = @(
        '-m', 'ingestion_app.main_live',
        '--mode', $Mode,
        '--api-base', $ApiBase,
        '--health-timeout-seconds', [string]$HealthTimeoutSeconds
    )
    if ($StartCollectors) {
        $args += '--start-collectors'
    }
    python @args
} finally {
    Pop-Location
}
