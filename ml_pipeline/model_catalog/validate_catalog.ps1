$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$catalogRoot = Join-Path $PSScriptRoot "models"

if (-not (Test-Path $catalogRoot)) {
    throw "Catalog folder not found: $catalogRoot"
}

$required = @("instance_key", "model_package", "threshold_report", "training_report_path")
$manifests = Get-ChildItem -Path $catalogRoot -Recurse -Filter "model.json"
if ($manifests.Count -eq 0) {
    throw "No model.json files found under $catalogRoot"
}

$errors = New-Object System.Collections.Generic.List[string]

foreach ($manifest in $manifests) {
    try {
        $json = Get-Content -Path $manifest.FullName -Raw | ConvertFrom-Json
    } catch {
        $errors.Add("Invalid JSON: $($manifest.FullName) :: $($_.Exception.Message)")
        continue
    }

    foreach ($field in $required) {
        if (-not ($json.PSObject.Properties.Name -contains $field) -or [string]::IsNullOrWhiteSpace([string]$json.$field)) {
            $errors.Add("Missing required field '$field' in $($manifest.FullName)")
        }
    }

    $pathFields = @("model_package", "threshold_report", "training_report_path")
    if ($json.PSObject.Properties.Name -contains "eval_summary_path" -and -not [string]::IsNullOrWhiteSpace([string]$json.eval_summary_path)) {
        $pathFields += "eval_summary_path"
    }

    foreach ($field in $pathFields) {
        $rel = [string]$json.$field
        if ([string]::IsNullOrWhiteSpace($rel)) {
            continue
        }
        $full = Join-Path $repoRoot $rel
        if (-not (Test-Path $full)) {
            $errors.Add("Missing path for '$field' in $($manifest.FullName): $rel")
            continue
        }
    }

    # Enforce standardized feature-first layout targets.
    $packagePath = [string]$json.model_package
    if ($packagePath -notmatch "^ml_pipeline/artifacts/models/by_features/.+/model/model\.joblib$") {
        $errors.Add("model_package must match by_features/*/model/model.joblib in $($manifest.FullName)")
    }
    $thrPath = [string]$json.threshold_report
    if ($thrPath -notmatch "^ml_pipeline/artifacts/models/by_features/.+/config/profiles/.+/threshold_report\.json$") {
        $errors.Add("threshold_report must match by_features/*/config/profiles/*/threshold_report.json in $($manifest.FullName)")
    }
    $trainPath = [string]$json.training_report_path
    if ($trainPath -notmatch "^ml_pipeline/artifacts/models/by_features/.+/config/profiles/.+/training_report\.json$") {
        $errors.Add("training_report_path must match by_features/*/config/profiles/*/training_report.json in $($manifest.FullName)")
    }

    # Require model contract at model-group root (beside model/config/reports/data folders).
    $modelPackageRel = [string]$json.model_package
    if (-not [string]::IsNullOrWhiteSpace($modelPackageRel)) {
        $modelPackageFull = Join-Path $repoRoot $modelPackageRel
        if (Test-Path $modelPackageFull) {
            $groupRoot = Split-Path -Parent (Split-Path -Parent $modelPackageFull)
            $contractPath = Join-Path $groupRoot "model_contract.json"
            if (-not (Test-Path $contractPath)) {
                $errors.Add("missing model contract: $contractPath (referenced by $($manifest.FullName))")
            }
        }
    }
}

if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Host "ERROR: $_" -ForegroundColor Red }
    throw "Catalog validation failed with $($errors.Count) error(s)."
}

Write-Host "Catalog validation passed for $($manifests.Count) model manifest(s)." -ForegroundColor Green
