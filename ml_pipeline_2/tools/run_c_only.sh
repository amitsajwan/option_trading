#!/usr/bin/env bash
# run_c_only.sh — Run Grid C standalone runs (C1 → C2+C3 parallel)
# cv_config is a disallowed grid override key, so C1/C2/C3 run as standalone run_research calls.
# C1 runs first (fresh S2 HPO). C2 and C3 both reuse S1 from C1 and run in parallel.
#
# Launch:
#   tmux new-session -d -s grid_c
#   tmux send-keys -t grid_c \
#     "bash /home/savitasajwan03/option_trading/ml_pipeline_2/tools/run_c_only.sh \
#      2>&1 | tee /home/savitasajwan03/option_trading/ml_pipeline_2/tools/auto_grid_c.log" Enter
#   tmux attach -t grid_c

set -euo pipefail

REPO_ROOT="/home/savitasajwan03/option_trading"
PYTHON="${REPO_ROOT}/.venv/bin/python"
ML_ROOT="${REPO_ROOT}/ml_pipeline_2"
CONFIGS="${ML_ROOT}/configs/research"
TOOLS="${ML_ROOT}/tools"
ARTIFACTS="${ML_ROOT}/artifacts/research"
LOG_FILE="${TOOLS}/auto_grid_c.log"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"; }
ok()   { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')] ✓ $*${NC}" | tee -a "${LOG_FILE}"; }
fail() { echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ✗ $*${NC}" | tee -a "${LOG_FILE}"; exit 1; }

log "=== Grid C: Deep HPO (C1 baseline → C2 long_train + C3 long_valid in parallel) ==="
log "Grid B winner: B4 (fo_midday_time_aware_plus_oi_iv, S2_ROC=0.5453, 329 trades, long_share=51%)"

[ -f "${PYTHON}" ] || fail "Python not found at ${PYTHON}"
cd "${REPO_ROOT}"

# =============================================================================
# C1 — Deep HPO baseline (standard cv_config 120/21/21)
# S1 reuse from B4. S2 HPO: 12 trials/model, 80 experiments, 4h budget.
# =============================================================================
C1_MANIFEST="${CONFIGS}/staged_dual_recipe.deep_hpo_c1.json"
C1_RUN_DIR="${ARTIFACTS}/staged_deep_hpo_c1_base"

log ""
log "================================================================="
log "C1 — Deep HPO baseline (cv: train=120d, valid=21d)"
log "================================================================="

C1_START=$(date '+%Y-%m-%d %H:%M:%S')
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_research \
    --config "${C1_MANIFEST}" \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "C1 failed"

ok "C1 complete. Started: ${C1_START}  Finished: $(date '+%Y-%m-%d %H:%M:%S')"

C1_RUN_DIR_ACTUAL=$(ls -dt "${ARTIFACTS}"/staged_deep_hpo_c1_base* 2>/dev/null | head -1)
[ -n "${C1_RUN_DIR_ACTUAL}" ] || fail "C1 artifact dir not found (glob staged_deep_hpo_c1_base*)"
log "C1 artifacts: ${C1_RUN_DIR_ACTUAL}"

# =============================================================================
# C2 and C3 — run in parallel, both reuse S1 from C1
# =============================================================================
C2_MANIFEST="${CONFIGS}/staged_dual_recipe.deep_hpo_c2.json"
C3_MANIFEST="${CONFIGS}/staged_dual_recipe.deep_hpo_c3.json"
C2_LOG="${TOOLS}/c2_run.log"
C3_LOG="${TOOLS}/c3_run.log"

log ""
log "================================================================="
log "C2 + C3 — launching in parallel (both reuse S1 from C1)"
log "C2: cv train=180d  |  C3: cv valid=42d"
log "================================================================="

PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_research \
    --config "${C2_MANIFEST}" \
    > "${C2_LOG}" 2>&1 &
C2_PID=$!
log "C2 launched (PID ${C2_PID}) — log: ${C2_LOG}"

PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_research \
    --config "${C3_MANIFEST}" \
    > "${C3_LOG}" 2>&1 &
C3_PID=$!
log "C3 launched (PID ${C3_PID}) — log: ${C3_LOG}"

# Wait for both
C2_START=$(date '+%Y-%m-%d %H:%M:%S')
log "Waiting for C2 (PID ${C2_PID}) and C3 (PID ${C3_PID})..."

C2_EXIT=0; C3_EXIT=0
wait ${C2_PID} || C2_EXIT=$?
wait ${C3_PID} || C3_EXIT=$?

[ ${C2_EXIT} -eq 0 ] && ok "C2 complete." || log "WARNING: C2 exited with code ${C2_EXIT}"
[ ${C3_EXIT} -eq 0 ] && ok "C3 complete." || log "WARNING: C3 exited with code ${C3_EXIT}"

# =============================================================================
# Final summary
# =============================================================================
log ""
log "================================================================="
log "GRID C COMPLETE"
log "================================================================="

for RUN in staged_deep_hpo_c1_base staged_deep_hpo_c2_long_train staged_deep_hpo_c3_long_valid; do
    SP="${ARTIFACTS}/${RUN}/summary.json"
    if [ -f "${SP}" ]; then
        MODE=$(python3 -c "import json; s=json.load(open('${SP}')); print(s.get('completion_mode','?'))")
        log "  ${RUN}: ${MODE}"
    else
        log "  ${RUN}: no summary.json"
    fi
done

log ""
log "Check detailed metrics:"
log "  python3 /tmp/check_b_runs.py   (update script for C dirs if needed)"
log ""
ok "=== Done. Log: ${LOG_FILE} ==="
