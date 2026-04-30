#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
TF_DIR="${TF_DIR:-${REPO_ROOT}/infra/gcp}"

if [ ! -f "${OPERATOR_ENV_FILE}" ]; then
  echo "Operator env file not found: ${OPERATOR_ENV_FILE}" >&2
  echo "Copy ops/gcp/operator.env.example to ops/gcp/operator.env and fill it in." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID in operator.env}"
ZONE="${ZONE:?set ZONE in operator.env}"
TRAINING_VM_NAME="${TRAINING_VM_NAME:?set TRAINING_VM_NAME in operator.env}"

TRAINING_INSTANCE_TEMPLATE="${TRAINING_INSTANCE_TEMPLATE:-}"
if [ -z "${TRAINING_INSTANCE_TEMPLATE}" ]; then
  TRAINING_INSTANCE_TEMPLATE="$(
    cd "${TF_DIR}" &&
    terraform output -raw training_instance_template
  )"
fi

echo "Creating training VM ${TRAINING_VM_NAME} in ${ZONE}"
gcloud compute instances create "${TRAINING_VM_NAME}" \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --source-instance-template "${TRAINING_INSTANCE_TEMPLATE}"

echo "Training VM created: ${TRAINING_VM_NAME}"
