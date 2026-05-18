#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=${REPO_DIR:-/opt/option_trading}
CONFIG=${CONFIG:-ml_pipeline_2/configs/research/staged_grid.prod_v1.json}
MODEL_GROUP=${MODEL_GROUP:-banknifty_futures/h15_tp_auto}
PROFILE_ID=${PROFILE_ID:-openfe_v9_dual}
PARQUET=${PARQUET:-.data/ml_pipeline/parquet_data}

cd "$REPO_DIR"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
python -m pip install -e ./ml_pipeline_2 >/dev/null

if [[ ! -d "$PARQUET" ]]; then
  echo "[ERROR] Parquet base not found: $PARQUET" >&2
  exit 2
fi

echo "[1/3] Data preflight..."
python -m ml_pipeline_2.run_staged_data_preflight \
  --config "$CONFIG"

echo "[2/3] Manifest validate-only..."
python -m ml_pipeline_2.run_research \
  --config "$CONFIG" \
  --validate-only

echo "[3/3] Run staged grid..."
python -m ml_pipeline_2.run_staged_grid \
  --config "$CONFIG" \
  --model-group "$MODEL_GROUP" \
  --profile-id "$PROFILE_ID"

# Best-effort pointer to latest grid root
LATEST_GRID_DIR=$(ls -1dt ml_pipeline_2/artifacts/research/* 2>/dev/null | head -n1 || true)
if [[ -n "${LATEST_GRID_DIR}" && -f "${LATEST_GRID_DIR}/grid_summary.json" ]]; then
  echo "[DONE] Grid summary: ${LATEST_GRID_DIR}/grid_summary.json"
else
  echo "[DONE] Grid run completed. Inspect artifacts under ml_pipeline_2/artifacts/research/"
fi
