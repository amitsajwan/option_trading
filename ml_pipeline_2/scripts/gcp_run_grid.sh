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

# Optional: override final_holdout window by generating temporary manifests
EFFECTIVE_CONFIG="$CONFIG"
if [[ -n "${HOLDOUT_START:-}" && -n "${HOLDOUT_END:-}" ]]; then
  echo "[info] Applying holdout override: ${HOLDOUT_START} → ${HOLDOUT_END}"
  python - "$REPO_DIR" "$CONFIG" "$HOLDOUT_START" "$HOLDOUT_END" << 'PY'
import json, sys
from pathlib import Path
repo = Path(sys.argv[1])
grid_path = (repo / sys.argv[2]).resolve()
h_start, h_end = sys.argv[3], sys.argv[4]
grid = json.loads(grid_path.read_text(encoding='utf-8'))
base_rel = grid["inputs"]["base_manifest_path"]
base_path = (grid_path.parent / base_rel).resolve()
base = json.loads(base_path.read_text(encoding='utf-8'))
base.setdefault("windows", {}).setdefault("final_holdout", {})
base["windows"]["final_holdout"]["start"] = h_start
base["windows"]["final_holdout"]["end"] = h_end
tmp_base = grid_path.parent / "_tmp.deep_search.holdout_override.json"
tmp_base.write_text(json.dumps(base, indent=2), encoding='utf-8')
grid["inputs"]["base_manifest_path"] = str(tmp_base.relative_to(grid_path.parent))
rn = str(grid.get("outputs", {}).get("run_name") or "staged_grid")
rn = f"{rn}_holdout_{h_start.replace('-', '')}_{h_end.replace('-', '')}"
grid.setdefault("outputs", {})["run_name"] = rn
tmp_grid = grid_path.parent / "_tmp.grid.holdout_override.json"
tmp_grid.write_text(json.dumps(grid, indent=2), encoding='utf-8')
print(str(tmp_grid))
PY
  EFFECTIVE_CONFIG=$(python - << 'PY'
from pathlib import Path
import sys
p=sys.stdin.read().strip()
print(p)
PY
  )
  echo "[info] Using temp grid config: ${EFFECTIVE_CONFIG}"
fi

echo "[1/4] Data preflight..."
python -m ml_pipeline_2.run_staged_data_preflight \
  --config "$EFFECTIVE_CONFIG"

echo "[2/4] Manifest validate-only..."
python -m ml_pipeline_2.run_research \
  --config "$EFFECTIVE_CONFIG" \
  --validate-only

echo "[3/4] Run staged grid..."
python -m ml_pipeline_2.run_staged_grid \
  --config "$EFFECTIVE_CONFIG" \
  --model-group "$MODEL_GROUP" \
  --profile-id "$PROFILE_ID"

# Best-effort pointer to latest grid root
LATEST_GRID_DIR=$(ls -1dt ml_pipeline_2/artifacts/research/* 2>/dev/null | head -n1 || true)
if [[ -n "${LATEST_GRID_DIR}" && -f "${LATEST_GRID_DIR}/grid_summary.json" ]]; then
  echo "[4/4] Auditing grid runs..."
  python ml_pipeline_2/scripts/audit_grid.py --grid-root "${LATEST_GRID_DIR}" || echo "[warn] audit script failed"
  echo "[DONE] Grid summary: ${LATEST_GRID_DIR}/grid_summary.json"
  echo "[DONE] Audit summary: ${LATEST_GRID_DIR}/audit_summary.json (if generated)"
else
  echo "[DONE] Grid run completed. Inspect artifacts under ml_pipeline_2/artifacts/research/"
fi
