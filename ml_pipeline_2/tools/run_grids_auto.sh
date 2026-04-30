#!/usr/bin/env bash
# =============================================================================
# run_grids_auto.sh — Full automated experiment chain: Grid A → B → C
#
# Grid A: 3 standalone run_research calls (A1/A2/A3 differ in windows+labels
#         which the grid override validator does not permit).
# Grid B: run_staged_grid (only catalog.feature_sets_by_stage differs — allowed).
# Grid C: run_staged_grid (only training.cv_config/HPO differs — allowed).
#
# Launch inside tmux so the session survives SSH disconnect:
#   tmux new-session -d -s auto_grids
#   tmux send-keys -t auto_grids \
#     "bash /home/savitasajwan03/option_trading/ml_pipeline_2/tools/run_grids_auto.sh \
#      2>&1 | tee /home/savitasajwan03/option_trading/ml_pipeline_2/tools/auto_grids.log" Enter
#   tmux attach -t auto_grids     # to monitor
#   tmux detach                   # Ctrl-b d to detach
# =============================================================================

set -euo pipefail

REPO_ROOT="/home/savitasajwan03/option_trading"
PYTHON="${REPO_ROOT}/.venv/bin/python"
ML_ROOT="${REPO_ROOT}/ml_pipeline_2"
CONFIGS="${ML_ROOT}/configs/research"
TOOLS="${ML_ROOT}/tools"
ARTIFACTS="${ML_ROOT}/artifacts/research"
PROFILE_ID="ml_pure_staged_v1"
LOG_FILE="${TOOLS}/auto_grids.log"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"; }
ok()   { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')] ✓ $*${NC}" | tee -a "${LOG_FILE}"; }
warn() { echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ $*${NC}" | tee -a "${LOG_FILE}"; }
fail() { echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ✗ $*${NC}" | tee -a "${LOG_FILE}"; exit 1; }

log "=== Automated Grid Chain Started ==="
log "Repo: ${REPO_ROOT}  Python: ${PYTHON}"
[ -f "${PYTHON}" ] || fail "Python not found at ${PYTHON}"
[ -d "${CONFIGS}" ] || fail "Configs dir not found: ${CONFIGS}"

cd "${REPO_ROOT}"
git pull --ff-only 2>&1 | tee -a "${LOG_FILE}" || warn "git pull non-fatal issue — continuing"

# =============================================================================
# GRID A — 3 standalone run_research calls (windows+labels differ, can't use grid)
# A1 and A3 retrain S1. A2 reuses proven S1 from staged_proper_full_v1_20260426_051531.
# Run A1 and A2 in parallel (independent). A3 runs after both finish.
# =============================================================================
log ""
log "================================================================="
log "GRID A — Label Fix (standalone run_research calls)"
log "A1: window shift only | A2: market direction label | A3: combined"
log "================================================================="

A1_DIR="${ARTIFACTS}/staged_label_fix_a1_window_shift"
A2_DIR="${ARTIFACTS}/staged_label_fix_a2_market_direction"
A3_DIR="${ARTIFACTS}/staged_label_fix_a3_combined"

GRID_A_START=$(date '+%Y-%m-%d %H:%M:%S')

log "Launching A1 and A2 in parallel..."

PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_research \
    --config "${CONFIGS}/staged_dual_recipe.label_fix_base.json" \
    --run-output-root "${A1_DIR}" \
    2>&1 | tee -a "${LOG_FILE}" &
PID_A1=$!

PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_research \
    --config "${CONFIGS}/staged_dual_recipe.label_fix_a2.json" \
    --run-output-root "${A2_DIR}" \
    2>&1 | tee -a "${LOG_FILE}" &
PID_A2=$!

log "A1 PID=${PID_A1}  A2 PID=${PID_A2} — waiting..."
wait ${PID_A1} || warn "A1 exited non-zero (will still try A3 and check winner)"
wait ${PID_A2} || warn "A2 exited non-zero"

ok "A1 + A2 done. Starting A3 (combined: shifted windows + market direction label)..."

PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_research \
    --config "${CONFIGS}/staged_dual_recipe.label_fix_a3.json" \
    --run-output-root "${A3_DIR}" \
    2>&1 | tee -a "${LOG_FILE}" \
    || warn "A3 exited non-zero"

ok "Grid A complete. Started: ${GRID_A_START}  Finished: $(date '+%Y-%m-%d %H:%M:%S')"

# Collect the 3 individual summary paths
A1_SUMMARY="${A1_DIR}/summary.json"
A2_SUMMARY="${A2_DIR}/summary.json"
A3_SUMMARY="${A3_DIR}/summary.json"

# Verify at least one summary exists
FOUND_A=0
[ -f "${A1_SUMMARY}" ] && FOUND_A=$((FOUND_A+1)) && log "A1 summary: ${A1_SUMMARY}"
[ -f "${A2_SUMMARY}" ] && FOUND_A=$((FOUND_A+1)) && log "A2 summary: ${A2_SUMMARY}"
[ -f "${A3_SUMMARY}" ] && FOUND_A=$((FOUND_A+1)) && log "A3 summary: ${A3_SUMMARY}"
[ "${FOUND_A}" -gt 0 ] || fail "No Grid A run produced a summary.json — all runs failed"

# Build the list of existing summary paths
A_SUMMARIES=""
[ -f "${A1_SUMMARY}" ] && A_SUMMARIES="${A_SUMMARIES} ${A1_SUMMARY}"
[ -f "${A2_SUMMARY}" ] && A_SUMMARIES="${A_SUMMARIES} ${A2_SUMMARY}"
[ -f "${A3_SUMMARY}" ] && A_SUMMARIES="${A_SUMMARIES} ${A3_SUMMARY}"

# Update Grid B base manifest with Grid A winner
GRID_B_BASE="${CONFIGS}/staged_dual_recipe.label_fix_b_base.json"
log "Selecting Grid A winner and updating Grid B manifest: ${GRID_B_BASE}"
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u "${TOOLS}/update_grid_manifest.py" \
    --run-summaries ${A_SUMMARIES} \
    --base-manifest "${GRID_B_BASE}" \
    --grid-kind label_fix \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "Failed to update Grid B manifest from Grid A winner"

ok "Grid B manifest updated."

# =============================================================================
# GRID B — Feature Set Grid (proper run_staged_grid — catalog override is allowed)
# =============================================================================
GRID_B_CONFIG="${CONFIGS}/staged_grid.feature_s2_v1.json"
GRID_B_MODEL_GROUP="research/feature_s2_v1"
GRID_B_SUMMARY_GLOB="${ARTIFACTS}/staged_grid_feature_s2_v1_*/grid_summary.json"

log ""
log "================================================================="
log "GRID B — Feature Set Grid"
log "B1 expiry_v3 | B2 full | B3 asymmetry | B4 oi_iv | B5 interactions"
log "Config: ${GRID_B_CONFIG}"
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

# Update Grid C base manifest
GRID_C_BASE="${CONFIGS}/staged_dual_recipe.deep_hpo_base.json"
log "Selecting Grid B winner and updating Grid C manifest: ${GRID_C_BASE}"
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u "${TOOLS}/update_grid_manifest.py" \
    --grid-summary "${GRID_B_SUMMARY}" \
    --base-manifest "${GRID_C_BASE}" \
    --grid-kind feature_s2 \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "Failed to update Grid C manifest from Grid B winner"

ok "Grid C manifest updated."

# =============================================================================
# GRID C — Deep HPO + CV Config (proper run_staged_grid — training override allowed)
# =============================================================================
GRID_C_CONFIG="${CONFIGS}/staged_grid.deep_hpo_v1.json"
GRID_C_MODEL_GROUP="research/deep_hpo_v1"
GRID_C_SUMMARY_GLOB="${ARTIFACTS}/staged_grid_deep_hpo_v1_*/grid_summary.json"

log ""
log "================================================================="
log "GRID C — Deep HPO + CV Config"
log "C1 deep-HPO baseline | C2 long train window | C3 long valid window"
log "Config: ${GRID_C_CONFIG}"
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
# Final summary print
# =============================================================================
log ""
log "================================================================="
log "ALL GRIDS COMPLETE"
log "================================================================="
log "Grid A runs  : ${A1_DIR}  ${A2_DIR}  ${A3_DIR}"
log "Grid B summary: ${GRID_B_SUMMARY}"
log "Grid C summary: ${GRID_C_SUMMARY}"
log ""
log "Quick result check:"
"${PYTHON}" -c "
import json
from pathlib import Path

def show_run(label, sp):
    p = Path(sp)
    if not p.exists():
        print(f'  {label}: no summary')
        return
    s = json.loads(p.read_text())
    ch = s.get('combined_holdout') or {}
    sq = (s.get('stage_quality') or {}).get('stage2') or {}
    roc = sq.get('roc_auc')
    pf  = ch.get('profit_factor')
    tr  = ch.get('trades')
    ls  = ch.get('long_share')
    print(f'  {label}: S2_ROC={roc:.3f}  trades={tr}  PF={pf:.3f}  long_share={ls:.1%}')

def show_grid(label, gs):
    p = Path(gs)
    if not p.exists():
        print(f'  {label}: no grid_summary')
        return
    data = json.loads(p.read_text())
    winner = data.get('winner') or {}
    sp = winner.get('summary_path')
    run_id = winner.get('grid_run_id', 'none')
    print(f'  {label} winner: {run_id}')
    if sp:
        show_run(f'    {label}', sp)

show_run('A1', '${A1_SUMMARY}')
show_run('A2', '${A2_SUMMARY}')
show_run('A3', '${A3_SUMMARY}')
show_grid('Grid B', '${GRID_B_SUMMARY}')
show_grid('Grid C', '${GRID_C_SUMMARY}')
" 2>&1 | tee -a "${LOG_FILE}"

log ""
ok "=== Automation complete. Full log: ${LOG_FILE} ==="
