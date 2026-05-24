#!/usr/bin/env bash
# Overnight: Gate 1 E6 (May-Jul) then E6_aug_oct (Aug-Oct), then Gate 2 E2 cost overlay.
set -euo pipefail
REPO="${REPO_ROOT:-/opt/option_trading}"
LOG_DIR="${REPO}/.run/exit_risk_experiments"
mkdir -p "${LOG_DIR}"
cd "${REPO}"

echo "[$(date -Is)] START E6 May-Jul"
bash "${REPO}/ops/gcp/run_exit_risk_experiments.sh" E6

echo "[$(date -Is)] START E6_aug_oct"
bash "${REPO}/ops/gcp/run_exit_risk_experiments.sh" E6_aug_oct

echo "[$(date -Is)] Gate 2: E2 cost overlay (existing run 32b01989)"
sudo docker cp "${REPO}/ops/gcp/analyze_oos_validation_run.py" option_trading-dashboard-1:/tmp/analyze_oos_validation_run.py
E2_RID="32b01989-85f5-4285-9cb8-41c47bcfc8ce"
{
  echo "=== E2 default costs ==="
  sudo docker exec -e MONGO_URL=mongodb://mongo:27017 option_trading-dashboard-1 \
    python /tmp/analyze_oos_validation_run.py "${E2_RID}" oos_E2_gross
  echo ""
  echo "=== E2 slippage 50bps ==="
  sudo docker exec -e MONGO_URL=mongodb://mongo:27017 -e OOS_COST_SLIPPAGE_BPS=50 option_trading-dashboard-1 \
    python /tmp/analyze_oos_validation_run.py "${E2_RID}" oos_E2_cost50
} | tee "${LOG_DIR}/E2_cost_overlay.log"

echo "[$(date -Is)] ALL DONE"
