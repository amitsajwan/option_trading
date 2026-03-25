#!/usr/bin/env bash
set -euo pipefail

RUNTIME_CONFIG_BUCKET_URL="${RUNTIME_CONFIG_BUCKET_URL:?set RUNTIME_CONFIG_BUCKET_URL, for example gs://my-runtime-config/runtime}"
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGE_DIR}"' EXIT
PYTHON_BIN="${PYTHON_BIN:-}"

if [ ! -f "${REPO_ROOT}/.env.compose" ]; then
  echo "Missing ${REPO_ROOT}/.env.compose" >&2
  exit 1
fi

if [ -z "${PYTHON_BIN}" ]; then
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi

if [ -z "${PYTHON_BIN}" ]; then
  echo "Python 3 is required to run runtime preflight validation." >&2
  exit 1
fi

"${PYTHON_BIN}" "${REPO_ROOT}/ops/gcp/validate_runtime_bundle.py" \
  --mode runtime \
  --repo-root "${REPO_ROOT}" \
  --env-file "${REPO_ROOT}/.env.compose"

# shellcheck disable=SC1090
source "${REPO_ROOT}/.env.compose"

guard_file="${STRATEGY_ML_RUNTIME_GUARD_FILE:-}"
historical_guard_file="${STRATEGY_ML_RUNTIME_GUARD_FILE_HISTORICAL:-}"

mkdir -p "${STAGE_DIR}/ingestion_app"
cp "${REPO_ROOT}/.env.compose" "${STAGE_DIR}/.env.compose"

if [ -f "${REPO_ROOT}/ingestion_app/credentials.json" ]; then
  cp "${REPO_ROOT}/ingestion_app/credentials.json" "${STAGE_DIR}/ingestion_app/credentials.json"
fi

if [ -n "${guard_file}" ]; then
  mkdir -p "${STAGE_DIR}/$(dirname "${guard_file}")"
  cp "${REPO_ROOT}/${guard_file}" "${STAGE_DIR}/${guard_file}"
fi

if [ -n "${historical_guard_file}" ] && [ "${historical_guard_file}" != "${guard_file}" ]; then
  mkdir -p "${STAGE_DIR}/$(dirname "${historical_guard_file}")"
  cp "${REPO_ROOT}/${historical_guard_file}" "${STAGE_DIR}/${historical_guard_file}"
fi

echo "Syncing runtime bootstrap bundle to ${RUNTIME_CONFIG_BUCKET_URL}"
gcloud storage rsync "${STAGE_DIR}" "${RUNTIME_CONFIG_BUCKET_URL%/}" --recursive
echo "Runtime config sync complete."
