#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
TF_DIR="${TF_DIR:-${REPO_ROOT}/infra/gcp}"
RUN_TERRAFORM="${RUN_TERRAFORM:-1}"
RUN_IMAGE_BUILD="${RUN_IMAGE_BUILD:-1}"
RUN_RUNTIME_CONFIG_SYNC="${RUN_RUNTIME_CONFIG_SYNC:-1}"
TERRAFORM_AUTO_APPROVE="${TERRAFORM_AUTO_APPROVE:-0}"

if [ ! -f "${OPERATOR_ENV_FILE}" ]; then
  echo "Operator env file not found: ${OPERATOR_ENV_FILE}" >&2
  echo "Copy ops/gcp/operator.env.example to ops/gcp/operator.env and fill it in." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

require_var() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "Missing required variable: ${name}" >&2
    exit 1
  fi
}

require_var PROJECT_ID
require_var REGION
require_var ZONE
require_var REPO_CLONE_URL
require_var REPO_REF
require_var RUNTIME_NAME
require_var RUNTIME_MACHINE_TYPE
require_var TRAINING_MACHINE_TYPE
require_var REPOSITORY
require_var TAG
require_var MODEL_BUCKET_NAME
require_var RUNTIME_CONFIG_BUCKET_NAME
require_var DASHBOARD_PORT
require_var SSH_SOURCE_RANGES
require_var DASHBOARD_SOURCE_RANGES

if [ -z "${MODEL_BUCKET_URL:-}" ]; then
  MODEL_BUCKET_URL="gs://${MODEL_BUCKET_NAME}/published_models"
fi

if [ -z "${RUNTIME_CONFIG_BUCKET_URL:-}" ]; then
  RUNTIME_CONFIG_BUCKET_URL="gs://${RUNTIME_CONFIG_BUCKET_NAME}/runtime"
fi

mkdir -p "${TF_DIR}"

if [ ! -f "${REPO_ROOT}/.env.compose" ] && [ -f "${REPO_ROOT}/.env.compose.example" ]; then
  cp "${REPO_ROOT}/.env.compose.example" "${REPO_ROOT}/.env.compose"
  echo "Created ${REPO_ROOT}/.env.compose from .env.compose.example"
fi

cat > "${TF_DIR}/terraform.tfvars" <<EOF
project_id                   = "${PROJECT_ID}"
region                       = "${REGION}"
zone                         = "${ZONE}"
repo_clone_url               = "${REPO_CLONE_URL}"
repo_ref                     = "${REPO_REF}"

runtime_name                 = "${RUNTIME_NAME}"
runtime_machine_type         = "${RUNTIME_MACHINE_TYPE}"
training_machine_type        = "${TRAINING_MACHINE_TYPE}"

artifact_registry_repository_id = "${REPOSITORY}"
artifact_registry_location      = "${REGION}"
app_image_tag                   = "${TAG}"

model_bucket_name            = "${MODEL_BUCKET_NAME}"
runtime_config_bucket_name   = "${RUNTIME_CONFIG_BUCKET_NAME}"

runtime_config_sync_source   = "${RUNTIME_CONFIG_BUCKET_URL}"
published_models_sync_source = "${MODEL_BUCKET_URL}"
data_sync_source             = "${DATA_SYNC_SOURCE}"

dashboard_port               = ${DASHBOARD_PORT}
enable_dashboard_profile     = ${ENABLE_DASHBOARD_PROFILE:-true}

ssh_source_ranges            = ["${SSH_SOURCE_RANGES}"]
dashboard_source_ranges      = ["${DASHBOARD_SOURCE_RANGES}"]
EOF

if [ -n "${SNAPSHOT_DATA_BUCKET_NAME:-}" ]; then
  cat >> "${TF_DIR}/terraform.tfvars" <<EOF
create_snapshot_data_bucket  = true
snapshot_data_bucket_name    = "${SNAPSHOT_DATA_BUCKET_NAME}"
EOF
fi

echo "Wrote ${TF_DIR}/terraform.tfvars"

if [ "${RUN_TERRAFORM}" = "1" ]; then
  (
    cd "${TF_DIR}"
    terraform init
    terraform plan
    if [ "${TERRAFORM_AUTO_APPROVE}" = "1" ]; then
      terraform apply -auto-approve
    else
      terraform apply
    fi
  )
fi

if [ "${RUN_IMAGE_BUILD}" = "1" ]; then
  export PROJECT_ID REGION REPOSITORY TAG
  "${REPO_ROOT}/ops/gcp/build_runtime_images.sh"
fi

if [ "${RUN_RUNTIME_CONFIG_SYNC}" = "1" ]; then
  export RUNTIME_CONFIG_BUCKET_URL
  export REPO_ROOT
  "${REPO_ROOT}/ops/gcp/publish_runtime_config.sh"
fi

echo
echo "Bootstrap summary"
echo "  terraform vars: ${TF_DIR}/terraform.tfvars"
echo "  runtime config bucket: ${RUNTIME_CONFIG_BUCKET_URL}"
echo "  model bucket: ${MODEL_BUCKET_URL}"
echo "  image tag: ${TAG}"
echo
echo "Next step: create a disposable training VM with ops/gcp/create_training_vm.sh"
