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

# Try the configured zone first, then the other zones in the same region on exhaustion.
REGION="${REGION:-${ZONE%-*}}"
FALLBACK_ZONES=()
for z in a b c; do
  candidate="${REGION}-${z}"
  [ "${candidate}" != "${ZONE}" ] && FALLBACK_ZONES+=("${candidate}")
done
ZONES_TO_TRY=("${ZONE}" "${FALLBACK_ZONES[@]}")

CREATED_ZONE=""
for try_zone in "${ZONES_TO_TRY[@]}"; do
  echo "Trying to create training VM ${TRAINING_VM_NAME} in ${try_zone}..."
  if gcloud compute instances create "${TRAINING_VM_NAME}" \
      --project "${PROJECT_ID}" \
      --zone "${try_zone}" \
      --source-instance-template "${TRAINING_INSTANCE_TEMPLATE}" 2>&1; then
    CREATED_ZONE="${try_zone}"
    break
  else
    echo "Zone ${try_zone} failed — trying next zone..."
  fi
done

if [ -z "${CREATED_ZONE}" ]; then
  echo "ERROR: Could not create training VM in any zone (${ZONES_TO_TRY[*]})." >&2
  echo "Try a different machine type by editing TRAINING_MACHINE_TYPE in operator.env and re-running Terraform." >&2
  exit 1
fi

echo "Training VM created: ${TRAINING_VM_NAME} in ${CREATED_ZONE}"
if [ "${CREATED_ZONE}" != "${ZONE}" ]; then
  echo "NOTE: VM was created in ${CREATED_ZONE} (not the configured ${ZONE})."
  echo "      Update ZONE in ops/gcp/operator.env if you want SSH commands to work without --zone."
fi
