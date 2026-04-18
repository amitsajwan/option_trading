Write-Host "This repo's GCP runtime deploy path is Linux VM + Docker Compose, not Cloud Run." -ForegroundColor Yellow
Write-Host "Use this on the GCP runtime VM:" -ForegroundColor Cyan
Write-Host "  cd /opt/option_trading"
Write-Host "  bash ./quick_deploy_gcp.sh"
Write-Host ""
Write-Host "Runbook:"
Write-Host "  ops/gcp/VELOCITY_RUNTIME_DEPLOY.md"
