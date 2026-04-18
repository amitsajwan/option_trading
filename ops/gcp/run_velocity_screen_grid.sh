#!/usr/bin/env bash
# run_velocity_screen_grid.sh
#
# Runs the velocity-feature screen grid (3 lanes: all_day, midday, midday_late).
# All three stages use fo_velocity_v1 features against the stage*_view_v2 parquet.
#
# Parquet rebuild is SKIPPED — stage*_view_v2 must already be built.
# Run snapshot_app.historical.rebuild_stage_views_from_flat with
# --source-flat-dataset snapshots_ml_flat_v2 and --output-stage*-dataset
# stage*_view_v2 first if the data is missing.
#
# Usage:
#   cd /home/savitasajwan03/option_trading
#   bash ops/gcp/run_velocity_screen_grid.sh
#
# To reattach after disconnect:
#   tmux attach -t velocity_screen

set -euo pipefail

SESSION_NAME="velocity_screen"
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
GRID_CONFIG="ml_pipeline_2/configs/research/staged_grid.velocity_screen_fast_v1.json"
MANIFEST_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.velocity_screen_fast_v1.json"

LOG_DIR="${REPO_ROOT}/logs/velocity_screen_$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "${LOG_DIR}"

echo "============================================================"
echo " Velocity screen grid run"
echo " stage*_view_v2  →  fo_velocity_v1 features"
echo " lanes: all_day | midday | midday_late"
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

# ── Step 2: verify v2 parquet is present ─────────────────────────────────────
echo
echo "── Step 2: verify stage*_view_v2 data ──"
"${VENV_PYTHON}" - "${PARQUET_ROOT}" <<'PY'
import sys, pathlib, textwrap

root = pathlib.Path(sys.argv[1])
ok = True
for ds in ("stage1_entry_view_v2", "stage2_direction_view_v2", "stage3_recipe_view_v2"):
    ds_path = root / ds
    if not ds_path.exists():
        print(f"  MISSING: {ds}")
        ok = False
        continue
    years = sorted(p.name for p in ds_path.iterdir() if p.is_dir() and p.name.startswith("year="))
    has_2024 = "year=2024" in years
    mark = "OK" if has_2024 else "WARN(no 2024)"
    print(f"  {mark}: {ds}  ({len(years)} years, {years[0] if years else '?'}–{years[-1] if years else '?'})")
    if not has_2024:
        ok = False

if not ok:
    print()
    print("ERROR: one or more v2 datasets are missing or incomplete.")
    print("Run rebuild_stage_views_from_flat with --source-flat-dataset snapshots_ml_flat_v2")
    print("and --output-stage*-dataset stage*_view_v2 before retrying.")
    sys.exit(1)

print()
print("All v2 datasets present and include year=2024.")
PY
echo "v2 data check OK."

# ── Step 3: preflight gate ────────────────────────────────────────────────────
echo
echo "── Step 3: preflight gate (velocity_screen_fast manifest) ──"
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

# ── Step 4: grid run ──────────────────────────────────────────────────────────
echo
echo "── Step 4: velocity screen grid run (3 lanes) ──"
GRID_LOG="${LOG_DIR}/grid.log"
RUN_OUTPUT_ROOT="${REPO_ROOT}/ml_pipeline_2/artifacts/training_launches/velocity_screen_$(date -u +%Y%m%d_%H%M%S)/run"
mkdir -p "$(dirname "${RUN_OUTPUT_ROOT}")"

"${VENV_PYTHON}" -m ml_pipeline_2.run_staged_grid \
    --config "${GRID_CONFIG}" \
    --run-output-root "${RUN_OUTPUT_ROOT}" \
    --model-group "${MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}" \
    2>&1 | tee "${GRID_LOG}"

# ── Step 5: quick results summary ────────────────────────────────────────────
echo
echo "── Step 5: results summary ──"
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
    pf = "n/a"
    trades = "n/a"
    dd = "n/a"
    gates = s.get("gates") or {}
    combined = gates.get("combined") or {}
    if combined:
        pf = combined.get("profit_factor", "n/a")
        trades = combined.get("trades", "n/a")
        dd = combined.get("max_drawdown_pct", "n/a")
    stage1 = (s.get("cv_prechecks") or {}).get("stage1_cv") or {}
    stage1_roc = stage1.get("roc_auc", "n/a")
    print(f"  {run_dir.name}:")
    print(f"    completion_mode  = {mode}")
    print(f"    stage1 roc_auc   = {stage1_roc}")
    print(f"    profit_factor    = {pf}")
    print(f"    trades           = {trades}")
    print(f"    max_drawdown_pct = {dd}")
PY

echo
echo "============================================================"
echo " velocity_screen run COMPLETE"
echo " logs:   ${LOG_DIR}"
echo " output: ${RUN_OUTPUT_ROOT}"
echo " $(date -u)"
echo "============================================================"
