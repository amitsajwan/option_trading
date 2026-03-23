#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
TF_DIR="${TF_DIR:-${REPO_ROOT}/infra/gcp}"
AUTO_APPROVE="${AUTO_APPROVE:-0}"
DELETE_TRAINING_VM_FIRST="${DELETE_TRAINING_VM_FIRST:-1}"

if [ ! -f "${OPERATOR_ENV_FILE}" ]; then
  echo "Operator env file not found: ${OPERATOR_ENV_FILE}" >&2
  echo "Copy ops/gcp/operator.env.example to ops/gcp/operator.env and fill it in." >&2
  exit 1
fi

if [ ! -d "${TF_DIR}" ]; then
  echo "Terraform directory not found: ${TF_DIR}" >&2
  exit 1
fi

if ! command -v terraform >/dev/null 2>&1 && [ -x "${HOME}/bin/terraform" ]; then
  export PATH="${HOME}/bin:${PATH}"
fi

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID in operator.env}"
ZONE="${ZONE:?set ZONE in operator.env}"

if [ "${DELETE_TRAINING_VM_FIRST}" = "1" ] && [ -n "${TRAINING_VM_NAME:-}" ]; then
  training_status="$(
    gcloud compute instances describe "${TRAINING_VM_NAME}" \
      --project "${PROJECT_ID}" \
      --zone "${ZONE}" \
      --format='value(status)' 2>/dev/null || true
  )"
  if [ -n "${training_status}" ]; then
    echo "Deleting disposable training VM before Terraform destroy: ${TRAINING_VM_NAME}"
    gcloud compute instances delete "${TRAINING_VM_NAME}" \
      --project "${PROJECT_ID}" \
      --zone "${ZONE}" \
      --quiet
  fi
fi

echo "Destroying Terraform-managed compute/network/IAM while preserving:"
echo "  - Artifact Registry repository"
echo "  - published model bucket"
echo "  - runtime config bucket"

targets=(
  google_compute_instance.runtime
  google_compute_instance_template.training
  google_compute_firewall.runtime_ssh
  google_compute_firewall.runtime_dashboard
  google_compute_address.runtime
  google_project_iam_member.runtime_roles
  google_project_iam_member.training_roles
  google_service_account.runtime
  google_service_account.training
)

cmd=(terraform destroy)
if [ "${AUTO_APPROVE}" = "1" ]; then
  cmd+=(-auto-approve)
fi
for target in "${targets[@]}"; do
  cmd+=("-target=${target}")
done

(
  cd "${TF_DIR}"
  terraform init -input=false >/dev/null
  "${cmd[@]}"
)

echo "Preserve-data destroy complete."
echo "Buckets and Artifact Registry repo were left intact."
