#!/usr/bin/env bash
set -euo pipefail

RUNTIME_CONFIG_BUCKET_URL="${RUNTIME_CONFIG_BUCKET_URL:?set RUNTIME_CONFIG_BUCKET_URL, for example gs://my-runtime-config/runtime}"
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGE_DIR}"' EXIT

if [ ! -f "${REPO_ROOT}/.env.compose" ]; then
  echo "Missing ${REPO_ROOT}/.env.compose" >&2
  exit 1
fi

mkdir -p "${STAGE_DIR}/ingestion_app"
cp "${REPO_ROOT}/.env.compose" "${STAGE_DIR}/.env.compose"

if [ -f "${REPO_ROOT}/ingestion_app/credentials.json" ]; then
  cp "${REPO_ROOT}/ingestion_app/credentials.json" "${STAGE_DIR}/ingestion_app/credentials.json"
fi

echo "Syncing runtime bootstrap bundle to ${RUNTIME_CONFIG_BUCKET_URL}"
gcloud storage rsync "${STAGE_DIR}" "${RUNTIME_CONFIG_BUCKET_URL%/}" --recursive
echo "Runtime config sync complete."
