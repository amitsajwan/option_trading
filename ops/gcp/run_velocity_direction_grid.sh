#!/usr/bin/env bash
# run_velocity_direction_grid.sh
#
# Final Stage 2 direction validation: velocity + momentum (fo_velocity_v1) and
# comprehensive PCR/IV/OI set (fo_expiry_aware_v3) against v3_candidate data.
#
# Stage 2 gate is HARD — if ROC < 0.55 the lane stops. This tells us definitively
# whether any feature set can produce a viable direction signal.
#
# Stage 1 is trained fresh (fo_full, all sessions). Lane 2 reuses Lane 1's Stage 1.
# Parquet rebuild is skipped (v3_candidate already built).
#
# Usage:
#   cd /home/savitasajwan03/option_trading
#   bash ops/gcp/run_velocity_direction_grid.sh
#
# To reattach after disconnect:
#   tmux attach -t vel_dir_grid

set -euo pipefail

SESSION_NAME="vel_dir_grid"
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
GRID_CONFIG="ml_pipeline_2/configs/research/staged_grid.velocity_direction_v1.json"
MANIFEST_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.velocity_direction_v1.json"

LOG_DIR="${REPO_ROOT}/logs/vel_dir_grid_$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "${LOG_DIR}"

echo "============================================================"
echo " Velocity direction grid — final Stage 2 validation"
echo " Lane 1: fo_velocity_v1 (velocity+momentum), MIDDAY"
echo " Lane 2: fo_expiry_aware_v3 (PCR+IV+OI+EMA), MIDDAY (reuse S1)"
echo " stage2_cv_gate_mode = hard"
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

# ── Step 2: data verification ─────────────────────────────────────────────────
echo
echo "── Step 2: verify velocity columns in v3_candidate data ──"
"${VENV_PYTHON}" - "${PARQUET_ROOT}/snapshots_ml_flat_v2" <<'PY'
import sys, duckdb, pathlib

dataset_root = sys.argv[1]
glob = str(pathlib.Path(dataset_root) / "**" / "*.parquet")
con = duckdb.connect()

# Column inventory
cols_df = con.execute(
    f"DESCRIBE SELECT * FROM read_parquet('{glob}', hive_partitioning=false, union_by_name=true) LIMIT 0"
).df()
all_cols = cols_df["column_name"].tolist() if "column_name" in cols_df.columns else []
vel_cols = sorted(c for c in all_cols if c.startswith("vel_") or c.startswith("ctx_am_")
                  or c in ("adx_14", "vol_spike_ratio", "ctx_gap_pct", "ctx_gap_up", "ctx_gap_down"))
print(f"Velocity/momentum columns ({len(vel_cols)}):")
for c in vel_cols:
    print(f"  {c}")

# Population check — post-computation rows (time_minute_of_day >= 690 = 11:30 AM IST)
# Market opens 9:15 AM = 555 min from midnight; 11:30 AM = 690 min from midnight.
row = con.execute(f"""
    SELECT
        COUNT(*) AS total,
        SUM(CASE WHEN ctx_am_vwap_side IS NOT NULL THEN 1 ELSE 0 END) AS vel_populated
    FROM read_parquet('{glob}', hive_partitioning=false, union_by_name=true)
    WHERE trade_date BETWEEN '2024-05-01' AND '2024-07-31'
      AND time_minute_of_day >= 690
""").df()
total = int(row["total"].iloc[0])
populated = int(row["vel_populated"].iloc[0])
pct = 100.0 * populated / total if total > 0 else 0
print(f"\nPost-11:30AM rows (valid window, time_minute_of_day >= 690): {total:,}")
print(f"  ctx_am_vwap_side populated: {populated:,} ({pct:.1f}%)")
if pct >= 95:
    print("  DATA CHECK PASS: velocity columns correctly populated for MIDDAY rows")
else:
    print("  DATA CHECK FAIL: velocity columns not populated — check forward-fill")
    sys.exit(1)
PY

echo "Data verification complete."

# ── Step 3: preflight gate ────────────────────────────────────────────────────
echo
echo "── Step 3: preflight gate (v3_candidate data check) ──"
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
echo "── Step 4: velocity direction grid (2 lanes) ──"
echo "    stage2_cv_gate_mode = hard"
echo "    Lane 1: fo_velocity_v1 (velocity+momentum+regime)"
echo "    Lane 2: fo_expiry_aware_v3 (PCR+IV+OI+EMA) — reuses Lane 1 Stage 1"
GRID_LOG="${LOG_DIR}/grid.log"
RUN_OUTPUT_ROOT="${REPO_ROOT}/ml_pipeline_2/artifacts/training_launches/vel_dir_grid_$(date -u +%Y%m%d_%H%M%S)/run"
mkdir -p "$(dirname "${RUN_OUTPUT_ROOT}")"

"${VENV_PYTHON}" -m ml_pipeline_2.run_staged_grid \
    --config "${GRID_CONFIG}" \
    --run-output-root "${RUN_OUTPUT_ROOT}" \
    --model-group "${MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}" \
    2>&1 | tee "${GRID_LOG}"

# ── Step 5: results summary ───────────────────────────────────────────────────
echo
echo "── Step 5: results summary ──"
"${VENV_PYTHON}" - "${RUN_OUTPUT_ROOT}" <<'PY'
import sys, json, pathlib

run_root = pathlib.Path(sys.argv[1])
runs_dir = run_root / "runs"
if not runs_dir.exists():
    print("No runs/ directory found — grid may have failed.")
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
    gate_passed = stage2_cv.get("gate_passed", "?")
    bypassed = stage2_cv.get("continued_after_failure", False)
    pf = "n/a"
    trades = "n/a"
    dd = "n/a"
    side = "n/a"
    gates = s.get("gates") or {}
    combined = gates.get("combined") or {}
    if combined:
        pf = combined.get("profit_factor", "n/a")
        trades = combined.get("trades", "n/a")
        dd = combined.get("max_drawdown_pct", "n/a")
        side = combined.get("side_share", "n/a")
    print(f"  {run_dir.name}:")
    print(f"    completion_mode      = {mode}")
    print(f"    stage2 roc_auc       = {stage2_roc}  (gate_passed={gate_passed}, bypassed={bypassed})")
    print(f"    profit_factor        = {pf}")
    print(f"    trades               = {trades}")
    print(f"    max_drawdown_pct     = {dd}")
    print(f"    side_share           = {side}")
PY

echo
echo "============================================================"
echo " vel_dir_grid run COMPLETE"
echo " logs:   ${LOG_DIR}"
echo " output: ${RUN_OUTPUT_ROOT}"
echo " $(date -u)"
echo "============================================================"
