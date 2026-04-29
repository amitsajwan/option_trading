#!/usr/bin/env bash
# run_d_grids.sh — Grid D runs (D1 + D2 in parallel)
#
# D1: cost=0.0, same as C1 — gross PF diagnostic (true edge w/o cost drag)
# D2: cost=0.0, min_ce_pe_edge=0.005 — high-edge filter (cleaner labels)
#
# Both reuse S1 from C1. Run in parallel. No dependency between D1 and D2.
#
# Launch:
#   tmux new-session -d -s grid_d
#   tmux send-keys -t grid_d \
#     "bash /home/savitasajwan03/option_trading/ml_pipeline_2/tools/run_d_grids.sh \
#      2>&1 | tee /home/savitasajwan03/option_trading/ml_pipeline_2/tools/auto_grid_d.log" Enter

set -euo pipefail

REPO_ROOT="/home/savitasajwan03/option_trading"
PYTHON="${REPO_ROOT}/.venv/bin/python"
ML_ROOT="${REPO_ROOT}/ml_pipeline_2"
CONFIGS="${ML_ROOT}/configs/research"
TOOLS="${ML_ROOT}/tools"
LOG_FILE="${TOOLS}/auto_grid_d.log"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"; }
ok()   { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')] ✓ $*${NC}" | tee -a "${LOG_FILE}"; }
fail() { echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ✗ $*${NC}" | tee -a "${LOG_FILE}"; exit 1; }

log "=== Grid D: Zero-cost + High-edge filter experiments ==="
log "Key insight: cost_per_trade affects policy selection and PF reporting only."
log "  Training labels (oracle columns) are pre-baked — unaffected by cost setting."
log "D1: cost=0.0, min_edge=0.002 (same labels as C1) — gross PF measurement"
log "D2: cost=0.0, min_edge=0.005 (stricter label filter) — higher-confidence trades"
log "Both reuse S1 from C1 (staged_deep_hpo_c1_base_20260429_040848)"

[ -f "${PYTHON}" ] || fail "Python not found at ${PYTHON}"

D1_MANIFEST="${CONFIGS}/staged_dual_recipe.deep_hpo_d1_zero_cost.json"
D2_MANIFEST="${CONFIGS}/staged_dual_recipe.deep_hpo_d2_high_edge.json"
D1_LOG="${TOOLS}/d1_run.log"
D2_LOG="${TOOLS}/d2_run.log"

log ""
log "================================================================="
log "Launching D1 + D2 in parallel"
log "================================================================="

PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_research \
    --config "${D1_MANIFEST}" \
    > "${D1_LOG}" 2>&1 &
D1_PID=$!
log "D1 launched (PID ${D1_PID}) — log: ${D1_LOG}"

PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_research \
    --config "${D2_MANIFEST}" \
    > "${D2_LOG}" 2>&1 &
D2_PID=$!
log "D2 launched (PID ${D2_PID}) — log: ${D2_LOG}"

log "Waiting for D1 (PID ${D1_PID}) and D2 (PID ${D2_PID})..."

D1_EXIT=0; D2_EXIT=0
wait ${D1_PID} || D1_EXIT=$?
wait ${D2_PID} || D2_EXIT=$?

[ ${D1_EXIT} -eq 0 ] && ok "D1 complete." || log "WARNING: D1 exited with code ${D1_EXIT} — check ${D1_LOG}"
[ ${D2_EXIT} -eq 0 ] && ok "D2 complete." || log "WARNING: D2 exited with code ${D2_EXIT} — check ${D2_LOG}"

log ""
log "================================================================="
log "GRID D COMPLETE — check results:"
log "  python3 /tmp/check_c_runs.py   (update BASE glob for d* dirs)"
log "  Or: python3 /tmp/check_d_runs.py"
log "================================================================="
ok "=== Done. Log: ${LOG_FILE} ==="
