#!/usr/bin/env bash
# =============================================================================
# run_grids_from_b.sh — Restart from Grid B using A2 as the confirmed winner.
#
# Grid A winner: A2 (direction_market_up_v1, original windows, S2_ROC=0.544,
# 168 holdout trades, long_share≈39% — bias corrected from 93.8%).
# A1 failed signal check; A3 had too few holdout trades.
#
# Launch inside tmux:
#   tmux new-session -d -s grids_bc
#   tmux send-keys -t grids_bc \
#     "bash /home/savitasajwan03/option_trading/ml_pipeline_2/tools/run_grids_from_b.sh \
#      2>&1 | tee /home/savitasajwan03/option_trading/ml_pipeline_2/tools/auto_grids_bc.log" Enter
#   tmux attach -t grids_bc
# =============================================================================

set -euo pipefail

REPO_ROOT="/home/savitasajwan03/option_trading"
PYTHON="${REPO_ROOT}/.venv/bin/python"
ML_ROOT="${REPO_ROOT}/ml_pipeline_2"
CONFIGS="${ML_ROOT}/configs/research"
TOOLS="${ML_ROOT}/tools"
ARTIFACTS="${ML_ROOT}/artifacts/research"
PROFILE_ID="ml_pure_staged_v1"
LOG_FILE="${TOOLS}/auto_grids_bc.log"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"; }
ok()   { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')] ✓ $*${NC}" | tee -a "${LOG_FILE}"; }
warn() { echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ $*${NC}" | tee -a "${LOG_FILE}"; }
fail() { echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ✗ $*${NC}" | tee -a "${LOG_FILE}"; exit 1; }

log "=== Grid B+C Restart (Grid A winner = A2 market_direction) ==="

[ -f "${PYTHON}" ] || fail "Python not found at ${PYTHON}"
cd "${REPO_ROOT}"

# =============================================================================
# Patch Grid B base manifest with A2 winner
# A2 run: staged_label_fix_a2_market_direction
# A2 settings: direction_market_up_v1, original windows, S1 reuse from staged_proper_full_v1_20260426_051531
# =============================================================================
A2_RUN_ID="staged_label_fix_a2_market_direction"
A2_RUN_DIR="${ARTIFACTS}/staged_label_fix_a2_market_direction"
GRID_B_BASE="${CONFIGS}/staged_dual_recipe.label_fix_b_base.json"

log "Patching Grid B base manifest with confirmed A2 winner..."
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u "${TOOLS}/update_grid_manifest.py" \
    --run-summaries "${A2_RUN_DIR}/summary.json" \
    --base-manifest "${GRID_B_BASE}" \
    --grid-kind label_fix \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "Failed to patch Grid B manifest with A2"

ok "Grid B manifest patched with A2 winner."

# =============================================================================
# GRID B — Feature Set Grid
# =============================================================================
GRID_B_CONFIG="${CONFIGS}/staged_grid.feature_s2_v1.json"
GRID_B_MODEL_GROUP="research/feature_s2_v1"
GRID_B_SUMMARY_GLOB="${ARTIFACTS}/staged_grid_feature_s2_v1_*/grid_summary.json"

log ""
log "================================================================="
log "GRID B — Feature Set Grid"
log "B1 expiry_v3 | B2 full | B3 asymmetry | B4 oi_iv | B5 interactions"
log "Base: direction_market_up_v1 label + original windows + S1 reuse from A2"
log "================================================================="

GRID_B_START=$(date '+%Y-%m-%d %H:%M:%S')
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_staged_grid \
    --config "${GRID_B_CONFIG}" \
    --model-group "${GRID_B_MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}" \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "Grid B failed"

ok "Grid B complete. Started: ${GRID_B_START}  Finished: $(date '+%Y-%m-%d %H:%M:%S')"

GRID_B_SUMMARY=$(ls -t ${GRID_B_SUMMARY_GLOB} 2>/dev/null | head -1)
[ -n "${GRID_B_SUMMARY}" ] || fail "Could not find grid_summary.json after Grid B"
log "Grid B summary: ${GRID_B_SUMMARY}"

# Update Grid C manifest with Grid B winner
GRID_C_BASE="${CONFIGS}/staged_dual_recipe.deep_hpo_base.json"
log "Selecting Grid B winner and updating Grid C manifest..."
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u "${TOOLS}/update_grid_manifest.py" \
    --grid-summary "${GRID_B_SUMMARY}" \
    --base-manifest "${GRID_C_BASE}" \
    --grid-kind feature_s2 \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "Failed to update Grid C manifest from Grid B winner"

ok "Grid C manifest updated."

# =============================================================================
# GRID C — Deep HPO + CV Config
# =============================================================================
GRID_C_CONFIG="${CONFIGS}/staged_grid.deep_hpo_v1.json"
GRID_C_MODEL_GROUP="research/deep_hpo_v1"
GRID_C_SUMMARY_GLOB="${ARTIFACTS}/staged_grid_deep_hpo_v1_*/grid_summary.json"

log ""
log "================================================================="
log "GRID C — Deep HPO + CV Config"
log "C1 deep-HPO baseline | C2 long train window | C3 long valid window"
log "================================================================="

GRID_C_START=$(date '+%Y-%m-%d %H:%M:%S')
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_staged_grid \
    --config "${GRID_C_CONFIG}" \
    --model-group "${GRID_C_MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}" \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "Grid C failed"

ok "Grid C complete. Started: ${GRID_C_START}  Finished: $(date '+%Y-%m-%d %H:%M:%S')"

GRID_C_SUMMARY=$(ls -t ${GRID_C_SUMMARY_GLOB} 2>/dev/null | head -1)
[ -n "${GRID_C_SUMMARY}" ] || fail "Could not find grid_summary.json after Grid C"

# =============================================================================
# Final summary
# =============================================================================
log ""
log "================================================================="
log "ALL COMPLETE"
log "================================================================="
log "Grid A winner: A2 (direction_market_up_v1, S2_ROC=0.544, 168 trades, long_share≈39%)"
log "Grid B summary: ${GRID_B_SUMMARY}"
log "Grid C summary: ${GRID_C_SUMMARY}"
log ""
log "Quick results:"
"${PYTHON}" /tmp/check_results2.py 2>&1 | tee -a "${LOG_FILE}" || true
log ""
ok "=== Done. Log: ${LOG_FILE} ==="
