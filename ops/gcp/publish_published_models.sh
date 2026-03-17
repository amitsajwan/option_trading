#!/usr/bin/env bash
set -euo pipefail

MODEL_BUCKET_URL="${MODEL_BUCKET_URL:?set MODEL_BUCKET_URL, for example gs://my-model-bucket/published_models}"
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
MODEL_GROUP="${MODEL_GROUP:-}"

SOURCE_ROOT="${REPO_ROOT}/ml_pipeline_2/artifacts/published_models"
if [ ! -d "${SOURCE_ROOT}" ]; then
  echo "Published models directory not found: ${SOURCE_ROOT}" >&2
  exit 1
fi

if [ -n "${MODEL_GROUP}" ]; then
  SOURCE_PATH="${SOURCE_ROOT}/${MODEL_GROUP}"
  TARGET_PATH="${MODEL_BUCKET_URL%/}/${MODEL_GROUP}"
else
  SOURCE_PATH="${SOURCE_ROOT}"
  TARGET_PATH="${MODEL_BUCKET_URL%/}"
fi

if [ ! -d "${SOURCE_PATH}" ]; then
  echo "Requested model group not found: ${SOURCE_PATH}" >&2
  exit 1
fi

echo "Syncing ${SOURCE_PATH} -> ${TARGET_PATH}"
gcloud storage rsync "${SOURCE_PATH}" "${TARGET_PATH}" --recursive
echo "Published model sync complete."
