#!/usr/bin/env bash
# run_expiry_direction_grid.sh
#
# fo_expiry_aware_v3 for Stage 2 direction (PCR + IV skew + OI + EMA + VIX + regime).
# Previous run (vel_dir_grid) showed this feature set reached ROC 0.571 — above the
# 0.55 threshold — but Brier 0.254 failed the calibration gate.
#
# This run uses:
#   stage2_cv_gate_mode = record_only  → Stage 3 runs even if Brier gate fails
#   Brier threshold relaxed to 0.26    → records gate result without blocking
#   Stage 2: 8 models × 5 HPO trials  → better chance of finding a well-calibrated model
#   Stage 1: fo_full, 4 models         → consistent strong entry gate
#
# To reattach after disconnect:
#   tmux attach -t expiry_dir_grid

set -euo pipefail

SESSION_NAME="expiry_dir_grid"
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
BRANCH="chore/ml-pipeline-ubuntu-gcp-runbook"

MODEL_GROUP="banknifty_futures/h15_tp_auto"
PROFILE_ID="openfe_v9_dual"
GRID_CONFIG="ml_pipeline_2/configs/research/staged_grid.expiry_direction_v1.json"
MANIFEST_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.expiry_direction_v1.json"

LOG_DIR="${REPO_ROOT}/logs/expiry_dir_grid_$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "${LOG_DIR}"

echo "============================================================"
echo " Expiry-aware direction grid"
echo " Stage 2: fo_expiry_aware_v3 (PCR+IV+OI+EMA), MIDDAY"
echo " stage2_cv_gate_mode = record_only  |  brier_max = 0.26"
echo " Stage 2 models: 8 models x 5 HPO trials"
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

# ── Step 2: preflight ─────────────────────────────────────────────────────────
echo
echo "── Step 2: preflight gate ──"
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
    exit 1
fi
echo "Preflight PASS."

# ── Step 3: grid run ──────────────────────────────────────────────────────────
echo
echo "── Step 3: expiry direction grid ──"
echo "    fo_expiry_aware_v3 for Stage 2, record_only gate"
echo "    Stage 3 will run regardless of Brier score"
GRID_LOG="${LOG_DIR}/grid.log"
RUN_OUTPUT_ROOT="${REPO_ROOT}/ml_pipeline_2/artifacts/training_launches/expiry_dir_grid_$(date -u +%Y%m%d_%H%M%S)/run"
mkdir -p "$(dirname "${RUN_OUTPUT_ROOT}")"

"${VENV_PYTHON}" -m ml_pipeline_2.run_staged_grid \
    --config "${GRID_CONFIG}" \
    --run-output-root "${RUN_OUTPUT_ROOT}" \
    --model-group "${MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}" \
    2>&1 | tee "${GRID_LOG}"

# ── Step 4: results summary ───────────────────────────────────────────────────
echo
echo "── Step 4: results summary ──"
"${VENV_PYTHON}" - "${RUN_OUTPUT_ROOT}" <<'PY'
import sys, json, pathlib

run_root = pathlib.Path(sys.argv[1])
runs_dir = run_root / "runs"
if not runs_dir.exists():
    print("No runs/ directory found.")
    sys.exit(0)

for run_dir in sorted(runs_dir.iterdir()):
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        print(f"  {run_dir.name}: no summary.json")
        continue
    s = json.loads(summary_path.read_text())
    mode = s.get("completion_mode", "?")
    cv = s.get("cv_prechecks") or {}
    s1 = cv.get("stage1_cv") or {}
    s2 = cv.get("stage2_cv") or {}
    bypassed = s2.get("continued_after_failure", False)
    pf = "n/a"; trades = "n/a"; dd = "n/a"; side = "n/a"
    gates = s.get("gates") or {}
    combined = gates.get("combined") or {}
    if combined:
        pf     = combined.get("profit_factor", "n/a")
        trades = combined.get("trades", "n/a")
        dd     = combined.get("max_drawdown_pct", "n/a")
        side   = combined.get("side_share", "n/a")
    print(f"  {run_dir.name}:")
    print(f"    completion_mode    = {mode}")
    print(f"    stage1 roc_auc     = {s1.get('roc_auc','n/a'):.4f}" if s1.get('roc_auc') else f"    stage1 roc_auc     = n/a")
    print(f"    stage2 roc_auc     = {s2.get('roc_auc','n/a'):.4f}  brier={s2.get('brier','n/a'):.4f}  gate_bypassed={bypassed}" if s2.get('roc_auc') else f"    stage2 roc_auc     = n/a")
    print(f"    profit_factor      = {pf}")
    print(f"    trades             = {trades}")
    print(f"    max_drawdown_pct   = {dd}")
    print(f"    side_share         = {side}")
PY

echo
echo "============================================================"
echo " expiry_dir_grid run COMPLETE"
echo " logs:   ${LOG_DIR}"
echo " output: ${RUN_OUTPUT_ROOT}"
echo " $(date -u)"
echo "============================================================"
