#!/usr/bin/env bash
set -euo pipefail

RAW_ARCHIVE_BUCKET_URL="${RAW_ARCHIVE_BUCKET_URL:?set RAW_ARCHIVE_BUCKET_URL, for example gs://my-snapshot-bucket/banknifty_data}"
RAW_DATA_ROOT="${RAW_DATA_ROOT:-${1:-}}"

if [ -z "${RAW_DATA_ROOT}" ]; then
  echo "Set RAW_DATA_ROOT or pass the raw archive path as the first argument." >&2
  exit 1
fi

if [ ! -d "${RAW_DATA_ROOT}" ]; then
  echo "Raw market data directory not found: ${RAW_DATA_ROOT}" >&2
  exit 1
fi

echo "Syncing ${RAW_DATA_ROOT} -> ${RAW_ARCHIVE_BUCKET_URL%/}"
gcloud storage rsync "${RAW_DATA_ROOT}" "${RAW_ARCHIVE_BUCKET_URL%/}" --recursive
echo "Raw market data sync complete."
