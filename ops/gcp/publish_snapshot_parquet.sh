#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
PARQUET_BASE="${PARQUET_BASE:-${REPO_ROOT}/.data/ml_pipeline/parquet_data}"
REPORT_ROOT="${REPORT_ROOT:-${REPO_ROOT}/.run/snapshot_parquet}"
SNAPSHOT_PARQUET_BUCKET_URL="${SNAPSHOT_PARQUET_BUCKET_URL:?set SNAPSHOT_PARQUET_BUCKET_URL, for example gs://my-snapshot-bucket/parquet_data}"
PUBLISH_DERIVED_ML_FLAT="${PUBLISH_DERIVED_ML_FLAT:-1}"
PUBLISH_STAGE_VIEWS="${PUBLISH_STAGE_VIEWS:-1}"
PUBLISH_MARKET_BASE="${PUBLISH_MARKET_BASE:-1}"
PUBLISH_NORMALIZED_CACHE="${PUBLISH_NORMALIZED_CACHE:-0}"

TARGET_ROOT="${SNAPSHOT_PARQUET_BUCKET_URL%/}"
SNAPSHOT_ROOT="${PARQUET_BASE}/snapshots"

if [ ! -d "${SNAPSHOT_ROOT}" ]; then
  echo "Canonical snapshots directory not found: ${SNAPSHOT_ROOT}" >&2
  exit 1
fi

sync_dir() {
  local source_path="$1"
  local target_path="$2"
  if [ ! -d "${source_path}" ]; then
    return 0
  fi
  echo "Syncing ${source_path} -> ${target_path}"
  gcloud storage rsync "${source_path}" "${target_path}" --recursive
}

sync_dir "${SNAPSHOT_ROOT}" "${TARGET_ROOT}/snapshots"

if [ "${PUBLISH_MARKET_BASE}" = "1" ]; then
  sync_dir "${PARQUET_BASE}/market_base" "${TARGET_ROOT}/market_base"
fi

if [ "${PUBLISH_DERIVED_ML_FLAT}" = "1" ]; then
  sync_dir "${PARQUET_BASE}/snapshots_ml_flat" "${TARGET_ROOT}/snapshots_ml_flat"
fi

if [ "${PUBLISH_STAGE_VIEWS}" = "1" ]; then
  sync_dir "${PARQUET_BASE}/stage1_entry_view" "${TARGET_ROOT}/stage1_entry_view"
  sync_dir "${PARQUET_BASE}/stage2_direction_view" "${TARGET_ROOT}/stage2_direction_view"
  sync_dir "${PARQUET_BASE}/stage3_recipe_view" "${TARGET_ROOT}/stage3_recipe_view"
fi

if [ "${PUBLISH_NORMALIZED_CACHE}" = "1" ]; then
  sync_dir "${PARQUET_BASE}/futures" "${TARGET_ROOT}/normalized/futures"
  sync_dir "${PARQUET_BASE}/options" "${TARGET_ROOT}/normalized/options"
  sync_dir "${PARQUET_BASE}/spot" "${TARGET_ROOT}/normalized/spot"
  sync_dir "${PARQUET_BASE}/vix" "${TARGET_ROOT}/normalized/vix"
fi

if [ -d "${REPORT_ROOT}" ]; then
  sync_dir "${REPORT_ROOT}" "${TARGET_ROOT}/reports"
fi

echo "Snapshot parquet sync complete."
