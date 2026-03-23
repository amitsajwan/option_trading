#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"

if [ ! -f "${OPERATOR_ENV_FILE}" ]; then
  echo "Operator env file not found: ${OPERATOR_ENV_FILE}" >&2
  echo "Copy ops/gcp/operator.env.example to ops/gcp/operator.env and fill it in." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID in operator.env}"
ZONE="${ZONE:?set ZONE in operator.env}"
DEFAULT_TRAINING_VM_NAME="${TRAINING_VM_NAME:-}"
TRAINING_VM_NAME="${1:-${DEFAULT_TRAINING_VM_NAME}}"

if [ -z "${TRAINING_VM_NAME}" ]; then
  echo "Set TRAINING_VM_NAME in operator.env or pass it as the first argument." >&2
  exit 1
fi

status="$(
  gcloud compute instances describe "${TRAINING_VM_NAME}" \
    --project "${PROJECT_ID}" \
    --zone "${ZONE}" \
    --format='value(status)' 2>/dev/null || true
)"

if [ -z "${status}" ]; then
  echo "Training VM not found: ${TRAINING_VM_NAME} (${ZONE})"
  exit 0
fi

echo "Deleting training VM ${TRAINING_VM_NAME} in ${ZONE}"
gcloud compute instances delete "${TRAINING_VM_NAME}" \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --quiet

echo "Training VM deleted: ${TRAINING_VM_NAME}"
