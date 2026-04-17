#!/usr/bin/env bash
# run_direction_fix_rebuild_and_grid.sh
#
# Complete direction-fix pipeline: pull latest code, rebuild v3_candidate parquet
# across all years, run preflight gate, then launch the direction_fix grid.
#
# Run as savitasajwan03 on the GCP training VM:
#   bash /home/savitasajwan03/option_trading/ops/gcp/run_direction_fix_rebuild_and_grid.sh
#
# Or from local via gcloud:
#   gcloud compute ssh option-trading-ml-01 --zone=asia-south1-b \
#     --project=gen-lang-client-0909109011 \
#     --command="sudo -u savitasajwan03 bash /home/savitasajwan03/option_trading/ops/gcp/run_direction_fix_rebuild_and_grid.sh"

set -euo pipefail

REPO_ROOT="/home/savitasajwan03/option_trading"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python3"
PARQUET_ROOT="/home/savitasajwan03/.data/ml_pipeline/parquet_data"
BRANCH="chore/ml-pipeline-ubuntu-gcp-runbook"

MODEL_GROUP="banknifty_futures/h15_tp_auto"
PROFILE_ID="openfe_v9_dual"
GRID_CONFIG="ml_pipeline_2/configs/research/staged_grid.direction_fix_v1.json"
MANIFEST_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.direction_fix_v1.json"

LOG_DIR="${REPO_ROOT}/logs/direction_fix_$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "${LOG_DIR}"

echo "============================================================"
echo " Direction-fix end-to-end run"
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

# ── Step 2: rebuild v3_candidate parquet (all years, no-resume) ───────────────
echo
echo "── Step 2: rebuild v3_candidate (full 2020-2024, this takes ~20-40 min) ──"
REBUILD_LOG="${LOG_DIR}/rebuild.log"
"${VENV_PYTHON}" -m snapshot_app.historical.rebuild_stage_views_from_flat \
    --parquet-root "${PARQUET_ROOT}" \
    --output-stage1-dataset stage1_entry_view_v3_candidate \
    --output-stage2-dataset stage2_direction_view_v3_candidate \
    --output-stage3-dataset stage3_recipe_view_v3_candidate \
    --no-resume \
    2>&1 | tee "${REBUILD_LOG}"

# quick sanity: confirm we got more than 2021 data
YEAR_COUNT=$("${VENV_PYTHON}" - "${PARQUET_ROOT}" <<'PY'
import sys, pathlib
root = pathlib.Path(sys.argv[1]) / "stage2_direction_view_v3_candidate"
years = sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.startswith("year="))
print(f"years rebuilt: {years}")
PY
)
echo "${YEAR_COUNT}"
if ! echo "${YEAR_COUNT}" | grep -q "year=2024"; then
    echo "ERROR: rebuild appears incomplete — year=2024 missing from stage2_direction_view_v3_candidate" >&2
    exit 1
fi
echo "Rebuild OK — year=2024 confirmed present."

# ── Step 3: preflight gate ────────────────────────────────────────────────────
echo
echo "── Step 3: preflight gate ──"
PREFLIGHT_LOG="${LOG_DIR}/preflight.json"
"${VENV_PYTHON}" -m ml_pipeline_2.run_staged_data_preflight \
    --config "${MANIFEST_CONFIG}" \
    --output "${PREFLIGHT_LOG}" \
    2>&1 | tee "${LOG_DIR}/preflight_stdout.log"

PREFLIGHT_STATUS=$("${VENV_PYTHON}" -c "import json,sys; d=json.load(open(sys.argv[1])); print(d['status'])" "${PREFLIGHT_LOG}")
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
echo "── Step 4: direction_fix grid run ──"
GRID_LOG="${LOG_DIR}/grid.log"
RUN_OUTPUT_ROOT="${REPO_ROOT}/ml_pipeline_2/artifacts/training_launches/direction_fix_$(date -u +%Y%m%d_%H%M%S)/run"
mkdir -p "$(dirname "${RUN_OUTPUT_ROOT}")"

"${VENV_PYTHON}" -m ml_pipeline_2.run_staged_grid \
    --config "${GRID_CONFIG}" \
    --run-output-root "${RUN_OUTPUT_ROOT}" \
    --model-group "${MODEL_GROUP}" \
    --profile-id "${PROFILE_ID}" \
    2>&1 | tee "${GRID_LOG}"

echo
echo "============================================================"
echo " direction_fix run COMPLETE"
echo " logs:   ${LOG_DIR}"
echo " output: ${RUN_OUTPUT_ROOT}"
echo " $(date -u)"
echo "============================================================"
