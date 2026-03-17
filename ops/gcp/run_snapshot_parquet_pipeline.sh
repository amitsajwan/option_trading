#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"
PARQUET_BASE="${PARQUET_BASE:-${REPO_ROOT}/.data/ml_pipeline/parquet_data}"
RAW_DATA_ROOT="${RAW_DATA_ROOT:-${REPO_ROOT}/.cache/banknifty_data}"
SYNC_RAW_ARCHIVE_FROM_GCS="${SYNC_RAW_ARCHIVE_FROM_GCS:-0}"
PUBLISH_SNAPSHOT_PARQUET="${PUBLISH_SNAPSHOT_PARQUET:-1}"
PUBLISH_DERIVED_ML_FLAT="${PUBLISH_DERIVED_ML_FLAT:-1}"
PUBLISH_NORMALIZED_CACHE="${PUBLISH_NORMALIZED_CACHE:-0}"
NORMALIZE_JOBS="${NORMALIZE_JOBS:-24}"
SNAPSHOT_JOBS="${SNAPSHOT_JOBS:-8}"
VALIDATE_DAYS="${VALIDATE_DAYS:-5}"
WINDOW_MIN_TRADING_DAYS="${WINDOW_MIN_TRADING_DAYS:-150}"
WINDOW_MAX_GAP_DAYS="${WINDOW_MAX_GAP_DAYS:-7}"
BUILD_SOURCE="${BUILD_SOURCE:-historical_gcp}"
BUILD_RUN_ID="${BUILD_RUN_ID:-snapshot_parquet_$(date -u +%Y%m%dT%H%M%SZ)}"
MANIFEST_ROOT="${MANIFEST_ROOT:-${REPO_ROOT}/.run/snapshot_parquet/${BUILD_RUN_ID}}"
MIN_DAY="${MIN_DAY:-}"
MAX_DAY="${MAX_DAY:-}"
YEAR="${YEAR:-}"
NO_RESUME="${NO_RESUME:-0}"
VALIDATE_ONLY="${VALIDATE_ONLY:-0}"
NORMALIZE_ONLY="${NORMALIZE_ONLY:-0}"

if [ ! -f "${OPERATOR_ENV_FILE}" ]; then
  echo "Operator env file not found: ${OPERATOR_ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

mkdir -p "${MANIFEST_ROOT}"

if [ "${SYNC_RAW_ARCHIVE_FROM_GCS}" = "1" ]; then
  RAW_ARCHIVE_BUCKET_URL="${RAW_ARCHIVE_BUCKET_URL:?set RAW_ARCHIVE_BUCKET_URL in operator.env}"
  mkdir -p "${RAW_DATA_ROOT}"
  echo "Syncing raw archive ${RAW_ARCHIVE_BUCKET_URL%/} -> ${RAW_DATA_ROOT}"
  gcloud storage rsync "${RAW_ARCHIVE_BUCKET_URL%/}" "${RAW_DATA_ROOT}" --recursive
fi

if [ ! -d "${VENV_DIR}" ]; then
  python3 -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "${REPO_ROOT}/snapshot_app/requirements.txt"

CMD=(
  python -m snapshot_app.historical.snapshot_batch_runner
  --base "${PARQUET_BASE}"
  --normalize-jobs "${NORMALIZE_JOBS}"
  --snapshot-jobs "${SNAPSHOT_JOBS}"
  --build-source "${BUILD_SOURCE}"
  --build-run-id "${BUILD_RUN_ID}"
  --validate-ml-flat-contract
  --validate-days "${VALIDATE_DAYS}"
  --manifest-out "${MANIFEST_ROOT}/build_manifest.json"
  --validation-report-out "${MANIFEST_ROOT}/validation_report.json"
  --window-manifest-out "${MANIFEST_ROOT}/window_manifest_latest.json"
  --window-min-trading-days "${WINDOW_MIN_TRADING_DAYS}"
  --window-max-gap-days "${WINDOW_MAX_GAP_DAYS}"
)

if [ -d "${RAW_DATA_ROOT}" ]; then
  CMD+=(--raw-root "${RAW_DATA_ROOT}")
fi

if [ -n "${MIN_DAY}" ]; then
  CMD+=(--min-day "${MIN_DAY}")
fi

if [ -n "${MAX_DAY}" ]; then
  CMD+=(--max-day "${MAX_DAY}")
fi

if [ -n "${YEAR}" ]; then
  CMD+=(--year "${YEAR}")
fi

if [ "${NO_RESUME}" = "1" ]; then
  CMD+=(--no-resume)
fi

if [ "${VALIDATE_ONLY}" = "1" ]; then
  CMD+=(--validate-only)
fi

if [ "${NORMALIZE_ONLY}" = "1" ]; then
  CMD+=(--normalize-only)
fi

(
  cd "${REPO_ROOT}"
  "${CMD[@]}"
)

if [ "${PUBLISH_SNAPSHOT_PARQUET}" = "1" ] && [ "${VALIDATE_ONLY}" != "1" ] && [ "${NORMALIZE_ONLY}" != "1" ]; then
  export REPO_ROOT PARQUET_BASE
  export REPORT_ROOT="${MANIFEST_ROOT}"
  export SNAPSHOT_PARQUET_BUCKET_URL="${SNAPSHOT_PARQUET_BUCKET_URL:?set SNAPSHOT_PARQUET_BUCKET_URL in operator.env}"
  export PUBLISH_DERIVED_ML_FLAT
  export PUBLISH_NORMALIZED_CACHE
  "${REPO_ROOT}/ops/gcp/publish_snapshot_parquet.sh"
fi

echo
echo "Snapshot parquet pipeline complete."
echo "  build run id: ${BUILD_RUN_ID}"
echo "  parquet base: ${PARQUET_BASE}"
echo "  report root: ${MANIFEST_ROOT}"
if [ "${PUBLISH_SNAPSHOT_PARQUET}" = "1" ] && [ "${VALIDATE_ONLY}" != "1" ] && [ "${NORMALIZE_ONLY}" != "1" ]; then
  echo "  gcs target: ${SNAPSHOT_PARQUET_BUCKET_URL}"
fi
