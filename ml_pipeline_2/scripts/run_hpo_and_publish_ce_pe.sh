#!/usr/bin/env bash
# Run HPO for ATM_PE_9 and ATM_CE_9 in parallel, then publish both bundles.
# Usage (on GCP ML instance):
#   bash /tmp/run_hpo_and_publish_ce_pe.sh
set -euo pipefail

VENV_PYTHON=/opt/option_trading/.venv/bin/python3
REPO=/opt/option_trading
HPO_SCRIPT="${REPO}/ml_pipeline_2/scripts/hpo_option_pnl.py"
PUBLISH_SCRIPT="${REPO}/ml_pipeline_2/scripts/publish_option_pnl_model.py"
export PYTHONPATH="${REPO}"
cd "${REPO}"
DATA=/opt/option_trading/.data/ml_pipeline
LABELS_ROOT="${DATA}/parquet_data/option_pnl_labels_v1"
FLAT_ROOT="${DATA}/parquet_data/snapshots_ml_flat_v2"
OUT_BASE="${DATA}"
TRIALS=30

echo "=== HPO + Publish: ATM_PE_9 + ATM_CE_9 ==="
echo "Trials: ${TRIALS} per recipe"
echo "Started: $(date -u)"
echo ""

# --- HPO PE_9 (background) ---
PE9_OUT="${OUT_BASE}/option_pnl_hpo_PE9_$(date -u +%Y%m%d_%H%M)"
mkdir -p "${PE9_OUT}"
echo "[PE_9] HPO starting → ${PE9_OUT}"
"${VENV_PYTHON}" "${HPO_SCRIPT}" \
  --recipe ATM_PE_9 \
  --trials ${TRIALS} \
  --labels "${LABELS_ROOT}" \
  --flat "${FLAT_ROOT}" \
  --out "${PE9_OUT}" \
  > "${PE9_OUT}/hpo.log" 2>&1 &
PE9_PID=$!

# --- HPO CE_9 (background) ---
CE9_OUT="${OUT_BASE}/option_pnl_hpo_CE9_$(date -u +%Y%m%d_%H%M)"
mkdir -p "${CE9_OUT}"
echo "[CE_9] HPO starting → ${CE9_OUT}"
"${VENV_PYTHON}" "${HPO_SCRIPT}" \
  --recipe ATM_CE_9 \
  --trials ${TRIALS} \
  --labels "${LABELS_ROOT}" \
  --flat "${FLAT_ROOT}" \
  --out "${CE9_OUT}" \
  > "${CE9_OUT}/hpo.log" 2>&1 &
CE9_PID=$!

echo ""
echo "Both HPO jobs running in parallel:"
echo "  PE_9 PID=${PE9_PID}  log=${PE9_OUT}/hpo.log"
echo "  CE_9 PID=${CE9_PID}  log=${CE9_OUT}/hpo.log"
echo ""

# Wait for both
echo "Waiting for HPO jobs..."
wait ${PE9_PID} && echo "[PE_9] HPO done" || echo "[PE_9] HPO FAILED"
wait ${CE9_PID} && echo "[CE_9] HPO done" || echo "[CE_9] HPO FAILED"

echo ""
echo "=== HPO Results ==="

# Extract best threshold and net PnL from PE_9
PE9_THR=$(python3 -c "
import json, sys
d = json.load(open('${PE9_OUT}/hpo_results.json'))
best = d['trials'][0]
print(best['best_threshold'])
" 2>/dev/null || echo "0.60")

PE9_NET=$(python3 -c "
import json, sys
d = json.load(open('${PE9_OUT}/hpo_results.json'))
best = d['trials'][0]
print(round(best['best_net_pnl_sum'], 3))
" 2>/dev/null || echo "?")

echo "[PE_9] best threshold=${PE9_THR}  net=${PE9_NET}"

# Extract best threshold for CE_9
CE9_THR=$(python3 -c "
import json, sys
d = json.load(open('${CE9_OUT}/hpo_results.json'))
best = d['trials'][0]
print(best['best_threshold'])
" 2>/dev/null || echo "0.55")

CE9_NET=$(python3 -c "
import json, sys
d = json.load(open('${CE9_OUT}/hpo_results.json'))
best = d['trials'][0]
print(round(best['best_net_pnl_sum'], 3))
" 2>/dev/null || echo "?")

echo "[CE_9] best threshold=${CE9_THR}  net=${CE9_NET}"

echo ""
echo "=== Publishing bundles ==="

# Publish PE_9
PE9_BUNDLE_OUT="${DATA}/option_pnl_published_models/option_pnl_atm_pe_9_$(date -u +%Y%m%d_%H%M%S)"
echo "[PE_9] Publishing → ${PE9_BUNDLE_OUT}"
"${VENV_PYTHON}" "${PUBLISH_SCRIPT}" \
  --recipe ATM_PE_9 \
  --labels-root "${LABELS_ROOT}" \
  --flat-root "${FLAT_ROOT}" \
  --hpo-results-json "${PE9_OUT}/hpo_results.json" \
  --threshold "${PE9_THR}" \
  --recipe-params '{"option_type":"PE","strike_offset_steps":0,"max_hold_bars":9,"stop_pct_of_premium":0.25,"target_pct_of_premium":0.40}' \
  --out "${PE9_BUNDLE_OUT}" \
  2>&1 | tee "${PE9_OUT}/publish.log"

# Publish CE_9
CE9_BUNDLE_OUT="${DATA}/option_pnl_published_models/option_pnl_atm_ce_9_$(date -u +%Y%m%d_%H%M%S)"
echo ""
echo "[CE_9] Publishing → ${CE9_BUNDLE_OUT}"
"${VENV_PYTHON}" "${PUBLISH_SCRIPT}" \
  --recipe ATM_CE_9 \
  --labels-root "${LABELS_ROOT}" \
  --flat-root "${FLAT_ROOT}" \
  --hpo-results-json "${CE9_OUT}/hpo_results.json" \
  --threshold "${CE9_THR}" \
  --recipe-params '{"option_type":"CE","strike_offset_steps":0,"max_hold_bars":9,"stop_pct_of_premium":0.25,"target_pct_of_premium":0.40}' \
  --out "${CE9_BUNDLE_OUT}" \
  2>&1 | tee "${CE9_OUT}/publish.log"

echo ""
echo "=== ALL DONE ==="
echo "Finished: $(date -u)"
echo ""
echo "PE_9 bundle: ${PE9_BUNDLE_OUT}"
echo "CE_9 bundle: ${CE9_BUNDLE_OUT}"
echo ""
echo "To activate multi-bundle in docker-compose.yml, set:"
echo "  OPTION_PNL_MODEL_BUNDLE=${PE9_BUNDLE_OUT},${CE9_BUNDLE_OUT}"
