#!/usr/bin/env bash
# run_soft_gate_grid.sh
#
# Runs the soft-gate direction-fix grid (stage2_cv_gate_mode=record_only).
# Stage 2 CV gate failure is recorded but no longer blocks Stage 3 — this run
# shows whether Stage 3 economics pass even when Stage 2 average ROC is low.
#
# Parquet rebuild is SKIPPED (v3_candidate is already built from prior run).
# Preflight is run to confirm data is still intact, then grid launches.
#
# Usage:
#   cd /home/savitasajwan03/option_trading
#   bash ops/gcp/run_soft_gate_grid.sh
#
# To reattach after disconnect:
#   tmux attach -t soft_gate_grid

set -euo pipefail

SESSION_NAME="soft_gate_grid"
if [ -z "${TMUX:-}" ]; then
    if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
        echo "Session '${SESSION_NAME}' already exists."
        echo "Attach with: tmux attach -t ${SESSION_NAME}"
        exit 0
    fi
    tmux new-session -d -s "${SESSION_NAME}" \
        "cd \"$(pwd)\" && _INSIDE_TMUX=1 bash \"${BASH_SOURCE[0]}\"; echo '--- press Enter to close ---'; read"
    echo "Started in tmux session '${SESSION_NAME}'."
    echo "Attach with: tmux attach -t ${SESSION_NAME}"
    exit 0
fi

REPO_ROOT="/home/savitasajwan03/option_trading"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python3"
PARQUET_ROOT="/home/savitasajwan03/.data/ml_pipeline/parquet_data"
BRANCH="chore/ml-pipeline-ubuntu-gcp-runbook"

MODEL_GROUP="banknifty_futures/h15_tp_auto"
PROFILE_ID="openfe_v9_dual"
GRID_CONFIG="ml_pipeline_2/configs/research/staged_grid.direction_fix_stage2_soft_gate_v1.json"
MANIFEST_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.direction_fix_stage2_soft_gate_v1.json"

LOG_DIR="${REPO_ROOT}/logs/soft_gate_grid_$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "${LOG_DIR}"

echo "============================================================"
echo " Soft-gate direction-fix grid run"
echo " stage2_cv_gate_mode = record_only"
echo " log dir: ${LOG_DIR}"
echo " $(date -u)"
echo "============================================================"

# ── Step 1: pull latest code ──────────────────────────────────────────────────
echo
echo "── Step 1: git pull ${BRANCH} ──"
cd "${REPO_ROOT}"
git fetch origin "${BRANCH}"
git checkout "${BRANCH}"
git pull origin "${BRANCH}"
echo "HEAD: $(git log --oneline -1)"

# ── Step 2: preflight gate (rebuild already done — just verify data) ──────────
echo
echo "── Step 2: preflight gate (v3_candidate data check) ──"
PREFLIGHT_LOG="${LOG_DIR}/preflight.json"
"${VENV_PYTHON}" -m ml_pipeline_2.run_staged_data_preflight \
    --config "${MANIFEST_CONFIG}" \
    --output "${PREFLIGHT_LOG}" \
    2>&1 | tee "${LOG_DIR}/preflight_stdout.log"

PREFLIGHT_STATUS=$("${VENV_PYTHON}" -c \
    "import json,sys; d=json.load(open(sys.argv[1])); print(d['status'])" \
    "${PREFLIGHT_LOG}")
echo "Preflight status: ${PREFLIGHT_STATUS}"
if [ "${PREFLIGHT_STATUS}" != "pass" ]; then
    echo "ERROR: preflight failed — see ${PREFLIGHT_LOG}" >&2
    "${VENV_PYTHON}" -c "
import json,sys
d=json.load(open(sys.argv[1]))
for e in d.get('errors', []):
    print(' FAIL:', e)
" "${PREFLIGHT_LOG}" >&2
    exit 1
fi
echo "Preflight PASS."

# ── Step 3: grid run ──────────────────────────────────────────────────────────
echo
echo "── Step 3: soft-gate grid run (3 lanes) ──"
echo "    Stage 2 CV gate failures will be recorded but will NOT block Stage 3."
GRID_LOG="${LOG_DIR}/grid.log"
RUN_OUTPUT_ROOT="${REPO_ROOT}/ml_pipeline_2/artifacts/training_launches/soft_gate_grid_$(date -u +%Y%m%d_%H%M%S)/run"
mkdir -p "$(dirname "${RUN_OUTPUT_ROOT}")"

"${VENV_PYTHON}" -m ml_pipeline_2.run_staged_grid \
    --config "${GRID_CONFIG}" \
    --run-output-root "${RUN_OUTPUT_ROOT}" \
    --model-group "${MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}" \
    2>&1 | tee "${GRID_LOG}"

# ── Step 4: quick results summary ────────────────────────────────────────────
echo
echo "── Step 4: results summary ──"
"${VENV_PYTHON}" - "${RUN_OUTPUT_ROOT}" <<'PY'
import sys, json, pathlib

run_root = pathlib.Path(sys.argv[1])
runs_dir = run_root / "runs"
if not runs_dir.exists():
    print("No runs/ directory found — grid may have failed to produce output.")
    sys.exit(0)

for run_dir in sorted(runs_dir.iterdir()):
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        print(f"  {run_dir.name}: no summary.json")
        continue
    s = json.loads(summary_path.read_text())
    mode = s.get("completion_mode", "?")
    stage2_cv = (s.get("cv_prechecks") or {}).get("stage2_cv") or {}
    stage2_roc = stage2_cv.get("roc_auc", "n/a")
    bypassed = stage2_cv.get("continued_after_failure", False)
    pf = "n/a"
    trades = "n/a"
    dd = "n/a"
    gates = s.get("gates") or {}
    combined = gates.get("combined") or {}
    if combined:
        pf = combined.get("profit_factor", "n/a")
        trades = combined.get("trades", "n/a")
        dd = combined.get("max_drawdown_pct", "n/a")
    print(f"  {run_dir.name}:")
    print(f"    completion_mode  = {mode}")
    print(f"    stage2 roc_auc   = {stage2_roc}  (gate_bypassed={bypassed})")
    print(f"    profit_factor    = {pf}")
    print(f"    trades           = {trades}")
    print(f"    max_drawdown_pct = {dd}")
PY

echo
echo "============================================================"
echo " soft_gate_grid run COMPLETE"
echo " logs:   ${LOG_DIR}"
echo " output: ${RUN_OUTPUT_ROOT}"
echo " $(date -u)"
echo "============================================================"
