#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"
APPLY_RUNTIME_HANDOFF="${APPLY_RUNTIME_HANDOFF:-1}"
PUBLISH_RUNTIME_CONFIG="${PUBLISH_RUNTIME_CONFIG:-1}"
AUTO_INSTALL_SYSTEM_PACKAGES="${AUTO_INSTALL_SYSTEM_PACKAGES:-1}"
PARQUET_BASE="${PARQUET_BASE:-${REPO_ROOT}/.data/ml_pipeline/parquet_data}"
TRAINING_RELEASE_JSON="${TRAINING_RELEASE_JSON:-${REPO_ROOT}/training-release.json}"
STAGE2_REQUIRED_COLUMNS="${STAGE2_REQUIRED_COLUMNS:-pcr_change_5m,pcr_change_15m,atm_oi_ratio,near_atm_oi_ratio,atm_ce_oi,atm_pe_oi}"
MODEL_GROUP_OVERRIDE="${MODEL_GROUP:-}"
PROFILE_ID_OVERRIDE="${PROFILE_ID:-}"
STAGED_CONFIG_OVERRIDE="${STAGED_CONFIG:-}"
MODEL_BUCKET_URL_OVERRIDE="${MODEL_BUCKET_URL:-}"
RUNTIME_CONFIG_BUCKET_URL_OVERRIDE="${RUNTIME_CONFIG_BUCKET_URL:-}"

ensure_file() {
  local path="$1"
  if [ ! -f "${path}" ]; then
    echo "Required file not found: ${path}" >&2
    exit 1
  fi
}

ensure_dir() {
  local path="$1"
  if [ ! -d "${path}" ]; then
    echo "Required directory not found: ${path}" >&2
    exit 1
  fi
}

run_as_root() {
  if [ "$(id -u)" = "0" ]; then
    "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
    return
  fi
  echo "Root privileges are required to run: $*" >&2
  exit 1
}

ensure_lightgbm_runtime() {
  if python - <<'PY'
import importlib
import sys

for name in ("lightgbm", "xgboost"):
    try:
        importlib.import_module(name)
    except Exception as exc:  # pragma: no cover - shell preflight only
        text = str(exc)
        print(f"{name}: {text}", file=sys.stderr)
        if "libgomp.so.1" in text:
            raise SystemExit(42)
        raise SystemExit(1)
print("optional_ml_dependencies_ok")
PY
  then
    return
  fi

  local status="$?"
  if [ "${status}" = "42" ] && [ "${AUTO_INSTALL_SYSTEM_PACKAGES}" = "1" ] && command -v apt-get >/dev/null 2>&1; then
    echo "Installing missing system package libgomp1 for LightGBM..."
    run_as_root apt-get update
    run_as_root apt-get install -y libgomp1
    python - <<'PY'
import importlib
for name in ("lightgbm", "xgboost"):
    importlib.import_module(name)
print("optional_ml_dependencies_ok")
PY
    return
  fi

  echo "Optional ML dependencies are not runnable. Install python packages and system libraries, then retry." >&2
  exit "${status}"
}

verify_stage2_required_columns() {
  local stage2_root="$1"
  local required_csv="$2"
  python - <<'PY' "${stage2_root}" "${required_csv}"
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

root = Path(sys.argv[1])
required = [item.strip() for item in str(sys.argv[2]).split(",") if item.strip()]
if not root.exists():
    print(f"stage2 dataset path not found: {root}", file=sys.stderr)
    raise SystemExit(1)
files = sorted(root.rglob("*.parquet"))
if not files:
    print(f"no parquet files found under stage2 dataset path: {root}", file=sys.stderr)
    raise SystemExit(1)

missing = []
for path in files:
    names = set(pq.ParquetFile(path).schema_arrow.names)
    absent = [name for name in required if name not in names]
    if absent:
        missing.append({"path": str(path), "missing_columns": absent})
if missing:
    print(json.dumps({"status": "failed", "missing_files": missing[:10]}, indent=2), file=sys.stderr)
    raise SystemExit(1)
print(json.dumps({"status": "ok", "checked_files": len(files), "required_columns": required}, indent=2))
PY
}

ensure_file "${OPERATOR_ENV_FILE}"

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

if [ -n "${MODEL_GROUP_OVERRIDE}" ]; then
  MODEL_GROUP="${MODEL_GROUP_OVERRIDE}"
fi
if [ -n "${PROFILE_ID_OVERRIDE}" ]; then
  PROFILE_ID="${PROFILE_ID_OVERRIDE}"
fi
if [ -n "${STAGED_CONFIG_OVERRIDE}" ]; then
  STAGED_CONFIG="${STAGED_CONFIG_OVERRIDE}"
fi
if [ -n "${MODEL_BUCKET_URL_OVERRIDE}" ]; then
  MODEL_BUCKET_URL="${MODEL_BUCKET_URL_OVERRIDE}"
fi
if [ -n "${RUNTIME_CONFIG_BUCKET_URL_OVERRIDE}" ]; then
  RUNTIME_CONFIG_BUCKET_URL="${RUNTIME_CONFIG_BUCKET_URL_OVERRIDE}"
fi

MODEL_GROUP="${MODEL_GROUP:?set MODEL_GROUP in operator.env}"
PROFILE_ID="${PROFILE_ID:?set PROFILE_ID in operator.env}"
STAGED_CONFIG="${STAGED_CONFIG:?set STAGED_CONFIG in operator.env}"
MODEL_BUCKET_URL="${MODEL_BUCKET_URL:?set MODEL_BUCKET_URL in operator.env}"

if [ ! -f "${REPO_ROOT}/.env.compose" ] && [ -f "${REPO_ROOT}/.env.compose.example" ]; then
  cp "${REPO_ROOT}/.env.compose.example" "${REPO_ROOT}/.env.compose"
  echo "Created ${REPO_ROOT}/.env.compose from .env.compose.example"
fi

ensure_dir "${PARQUET_BASE}/snapshots_ml_flat"
ensure_dir "${PARQUET_BASE}/stage1_entry_view"
ensure_dir "${PARQUET_BASE}/stage2_direction_view"
ensure_dir "${PARQUET_BASE}/stage3_recipe_view"

if [ ! -d "${VENV_DIR}" ]; then
  python3 -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip
python -m pip install -e "${REPO_ROOT}/ml_pipeline_2"
python -m pip install pyarrow lightgbm xgboost
ensure_lightgbm_runtime

echo "== Preflight: Verify local Stage 2 schema =="
verify_stage2_required_columns "${PARQUET_BASE}/stage2_direction_view" "${STAGE2_REQUIRED_COLUMNS}"

python -m ml_pipeline_2.run_staged_release \
  --config "${STAGED_CONFIG}" \
  --model-group "${MODEL_GROUP}" \
  --profile-id "${PROFILE_ID}" \
  --model-bucket-url "${MODEL_BUCKET_URL}" > "${TRAINING_RELEASE_JSON}"

echo "Release payload written to ${TRAINING_RELEASE_JSON}"
cat "${TRAINING_RELEASE_JSON}"

RELEASE_STATUS="$(
  python - <<'PY' "${TRAINING_RELEASE_JSON}"
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload.get("release_status") or "")
PY
)"
ASSESSMENT_PATH="$(
  python - <<'PY' "${TRAINING_RELEASE_JSON}"
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["paths"]["assessment"])
PY
)"
RELEASE_SUMMARY_PATH="$(
  python - <<'PY' "${TRAINING_RELEASE_JSON}"
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["paths"]["release_summary"])
PY
)"
BLOCKING_REASONS="$(
  python - <<'PY' "${TRAINING_RELEASE_JSON}"
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(", ".join(str(item) for item in list((payload.get("assessment") or {}).get("blocking_reasons") or [])))
PY
)"

if [ "${RELEASE_STATUS}" != "published" ]; then
  echo
  echo "Staged release pipeline complete."
  echo "  release status: ${RELEASE_STATUS}"
  echo "  release payload: ${TRAINING_RELEASE_JSON}"
  echo "  assessment: ${ASSESSMENT_PATH}"
  echo "  release summary: ${RELEASE_SUMMARY_PATH}"
  echo "  blocking reasons: ${BLOCKING_REASONS:-none}"
  exit 0
fi

RELEASE_ENV_PATH="$(
  python - <<'PY' "${TRAINING_RELEASE_JSON}"
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["paths"]["runtime_env"])
PY
)"

if [ "${APPLY_RUNTIME_HANDOFF}" = "1" ]; then
  export RELEASE_ENV_PATH
  "${REPO_ROOT}/ops/gcp/apply_ml_pure_release.sh"
fi

if [ "${PUBLISH_RUNTIME_CONFIG}" = "1" ]; then
  export RUNTIME_CONFIG_BUCKET_URL="${RUNTIME_CONFIG_BUCKET_URL:?set RUNTIME_CONFIG_BUCKET_URL in operator.env}"
  export REPO_ROOT
  "${REPO_ROOT}/ops/gcp/publish_runtime_config.sh"
fi

echo
echo "Staged release pipeline complete."
echo "  runtime handoff: ${RELEASE_ENV_PATH}"
