#!/usr/bin/env bash
# =============================================================================
# run_grids_auto.sh — Full automated experiment chain: Grid A → B → C
#
# Usage (run inside tmux so it survives SSH disconnect):
#   tmux new-session -d -s auto_grids
#   tmux send-keys -t auto_grids "bash ml_pipeline_2/tools/run_grids_auto.sh 2>&1 | tee ml_pipeline_2/tools/auto_grids.log" Enter
#   tmux attach -t auto_grids   # to monitor progress
#
# Each grid runs to completion before the next is started.
# After Grid A, the best run's windows + labeler + S1 path are automatically
# written into the Grid B base manifest. Similarly A+B → C.
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

# ---- colour helpers ---------------------------------------------------------
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"; }
ok()   { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')] ✓ $*${NC}" | tee -a "${LOG_FILE}"; }
warn() { echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ $*${NC}" | tee -a "${LOG_FILE}"; }
fail() { echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ✗ $*${NC}" | tee -a "${LOG_FILE}"; exit 1; }

# ---- sanity checks ----------------------------------------------------------
log "=== Automated Grid Chain Started ==="
log "Repo   : ${REPO_ROOT}"
log "Python : ${PYTHON}"

[ -f "${PYTHON}" ] || fail "Python not found at ${PYTHON}"
[ -d "${CONFIGS}" ] || fail "Configs directory not found: ${CONFIGS}"

cd "${REPO_ROOT}"

log "Pulling latest code..."
git pull --ff-only || warn "git pull had a non-fatal issue — continuing with current code"

# =============================================================================
# GRID A — Label Fix
# =============================================================================
GRID_A_CONFIG="${CONFIGS}/staged_grid.label_fix_v1.json"
GRID_A_MODEL_GROUP="research/label_fix_v1"
GRID_A_SUMMARY_GLOB="${ARTIFACTS}/staged_grid_label_fix_v1_*/grid_summary.json"

log ""
log "================================================================="
log "GRID A — Label Fix (A1 window shift / A2 market direction / A3 combined)"
log "Config  : ${GRID_A_CONFIG}"
log "================================================================="

GRID_A_START=$(date '+%Y-%m-%d %H:%M:%S')
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_staged_grid \
    --config "${GRID_A_CONFIG}" \
    --model-group "${GRID_A_MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}" \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "Grid A failed"

ok "Grid A completed. Started: ${GRID_A_START}  Finished: $(date '+%Y-%m-%d %H:%M:%S')"

# Find the most recently created grid_summary.json for Grid A
GRID_A_SUMMARY=$(ls -t ${GRID_A_SUMMARY_GLOB} 2>/dev/null | head -1)
[ -n "${GRID_A_SUMMARY}" ] || fail "Could not find grid_summary.json after Grid A"
log "Grid A summary: ${GRID_A_SUMMARY}"

# Update Grid B base manifest with Grid A winner
GRID_B_BASE="${CONFIGS}/staged_dual_recipe.label_fix_b_base.json"
log "Updating Grid B manifest: ${GRID_B_BASE}"
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u "${TOOLS}/update_grid_manifest.py" \
    --grid-summary "${GRID_A_SUMMARY}" \
    --base-manifest "${GRID_B_BASE}" \
    --grid-kind label_fix \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "Failed to update Grid B manifest from Grid A winner"

ok "Grid B manifest updated."

# =============================================================================
# GRID B — Feature Set
# =============================================================================
GRID_B_CONFIG="${CONFIGS}/staged_grid.feature_s2_v1.json"
GRID_B_MODEL_GROUP="research/feature_s2_v1"
GRID_B_SUMMARY_GLOB="${ARTIFACTS}/staged_grid_feature_s2_v1_*/grid_summary.json"

log ""
log "================================================================="
log "GRID B — Feature Set Grid (B1 expiry_v3 / B2 full / B3 asymmetry / B4 oi_iv / B5 interactions)"
log "Config  : ${GRID_B_CONFIG}"
log "================================================================="

GRID_B_START=$(date '+%Y-%m-%d %H:%M:%S')
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_staged_grid \
    --config "${GRID_B_CONFIG}" \
    --model-group "${GRID_B_MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}" \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "Grid B failed"

ok "Grid B completed. Started: ${GRID_B_START}  Finished: $(date '+%Y-%m-%d %H:%M:%S')"

GRID_B_SUMMARY=$(ls -t ${GRID_B_SUMMARY_GLOB} 2>/dev/null | head -1)
[ -n "${GRID_B_SUMMARY}" ] || fail "Could not find grid_summary.json after Grid B"
log "Grid B summary: ${GRID_B_SUMMARY}"

# Update Grid C base manifest with Grid B winner
GRID_C_BASE="${CONFIGS}/staged_dual_recipe.deep_hpo_base.json"
log "Updating Grid C manifest: ${GRID_C_BASE}"
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u "${TOOLS}/update_grid_manifest.py" \
    --grid-summary "${GRID_B_SUMMARY}" \
    --base-manifest "${GRID_C_BASE}" \
    --grid-kind feature_s2 \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "Failed to update Grid C manifest from Grid B winner"

ok "Grid C manifest updated."

# =============================================================================
# GRID C — Deep HPO
# =============================================================================
GRID_C_CONFIG="${CONFIGS}/staged_grid.deep_hpo_v1.json"
GRID_C_MODEL_GROUP="research/deep_hpo_v1"
GRID_C_SUMMARY_GLOB="${ARTIFACTS}/staged_grid_deep_hpo_v1_*/grid_summary.json"

log ""
log "================================================================="
log "GRID C — Deep HPO + CV Config (C1 base / C2 long-train / C3 long-valid)"
log "Config  : ${GRID_C_CONFIG}"
log "================================================================="

GRID_C_START=$(date '+%Y-%m-%d %H:%M:%S')
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" -u -m ml_pipeline_2.run_staged_grid \
    --config "${GRID_C_CONFIG}" \
    --model-group "${GRID_C_MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}" \
    2>&1 | tee -a "${LOG_FILE}" \
    || fail "Grid C failed"

ok "Grid C completed. Started: ${GRID_C_START}  Finished: $(date '+%Y-%m-%d %H:%M:%S')"

GRID_C_SUMMARY=$(ls -t ${GRID_C_SUMMARY_GLOB} 2>/dev/null | head -1)
[ -n "${GRID_C_SUMMARY}" ] || fail "Could not find grid_summary.json after Grid C"

# =============================================================================
# Final summary
# =============================================================================
log ""
log "================================================================="
log "ALL GRIDS COMPLETE"
log "================================================================="
log "Grid A summary : ${GRID_A_SUMMARY}"
log "Grid B summary : ${GRID_B_SUMMARY}"
log "Grid C summary : ${GRID_C_SUMMARY}"
log ""
log "Quick result check:"
"${PYTHON}" -c "
import json, sys
from pathlib import Path

def show(label, path):
    try:
        data = json.loads(Path(path).read_text())
        winner = data.get('winner') or {}
        run_id = winner.get('grid_run_id', 'none')
        sp = winner.get('summary_path')
        if sp and Path(sp).exists():
            s = json.loads(Path(sp).read_text())
            ch = s.get('combined_holdout') or {}
            sq = (s.get('stage_quality') or {}).get('stage2') or {}
            print(f'  {label}: winner={run_id}  S2_ROC={sq.get(\"roc_auc\",\"?\"):.3f}  trades={ch.get(\"trades\",\"?\")}  PF={ch.get(\"profit_factor\",\"?\"):.3f}  long_share={ch.get(\"long_share\",\"?\"):.1%}')
        else:
            print(f'  {label}: winner={run_id}  (no summary)')
    except Exception as e:
        print(f'  {label}: could not parse ({e})')

show('Grid A', '${GRID_A_SUMMARY}')
show('Grid B', '${GRID_B_SUMMARY}')
show('Grid C', '${GRID_C_SUMMARY}')
" 2>&1 | tee -a "${LOG_FILE}"

log ""
ok "=== Automation complete. Check ${LOG_FILE} for full log. ==="
