#!/usr/bin/env bash
# Wait for any in-flight exit/risk experiment, then run E2 and E2+E3.
set -euo pipefail
REPO="${REPO_ROOT:-/opt/option_trading}"
LOG_DIR="${REPO}/.run/exit_risk_experiments"
mkdir -p "${LOG_DIR}"

while pgrep -f "run_exit_risk_experiments.sh" >/dev/null 2>&1; do
  echo "[$(date -Is)] waiting for prior run_exit_risk_experiments to finish"
  sleep 30
done

cd "${REPO}"
git pull --ff-only origin main 2>/dev/null || true
bash "${REPO}/ops/gcp/run_exit_risk_experiments.sh" E2 >> "${LOG_DIR}/queue_E2.log" 2>&1
bash "${REPO}/ops/gcp/run_exit_risk_experiments.sh" E2E3 >> "${LOG_DIR}/queue_E2E3.log" 2>&1
echo "[$(date -Is)] E2 and E2E3 finished"
