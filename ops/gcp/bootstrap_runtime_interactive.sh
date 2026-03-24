#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
TEMPLATE_ENV_FILE="${REPO_ROOT}/ops/gcp/operator.env.example"

if [ ! -f "${TEMPLATE_ENV_FILE}" ]; then
  echo "Missing template env file: ${TEMPLATE_ENV_FILE}" >&2
  exit 1
fi

mkdir -p "$(dirname "${OPERATOR_ENV_FILE}")"

if [ -f "${OPERATOR_ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  source "${OPERATOR_ENV_FILE}"
fi

prompt_var() {
  local var_name="$1"
  local prompt_text="$2"
  local default_value="${3:-}"
  local entered=""
  if [ -n "${default_value}" ]; then
    read -r -p "${prompt_text} [${default_value}]: " entered || true
    entered="${entered:-${default_value}}"
  else
    read -r -p "${prompt_text}: " entered || true
  fi
  if [ -z "${entered}" ]; then
    echo "Value required: ${var_name}" >&2
    exit 1
  fi
  printf -v "${var_name}" '%s' "${entered}"
}

prompt_yes_no() {
  local prompt_text="$1"
  local default_answer="${2:-Y}"
  local answer=""
  read -r -p "${prompt_text} [${default_answer}/n]: " answer || true
  answer="${answer:-${default_answer}}"
  if [[ "${answer}" =~ ^[Yy]$ ]]; then
    return 0
  fi
  return 1
}

prompt_optional() {
  local var_name="$1"
  local prompt_text="$2"
  local default_value="${3:-}"
  local entered=""
  if [ -n "${default_value}" ]; then
    read -r -p "${prompt_text} [${default_value}]: " entered || true
    entered="${entered:-${default_value}}"
  else
    read -r -p "${prompt_text} (optional): " entered || true
  fi
  printf -v "${var_name}" '%s' "${entered}"
}

detect_default_project() {
  gcloud config get-value project 2>/dev/null | tr -d '\r'
}

detect_default_repo_url() {
  git -C "${REPO_ROOT}" config --get remote.origin.url 2>/dev/null || true
}

detect_default_repo_ref() {
  git -C "${REPO_ROOT}" branch --show-current 2>/dev/null || true
}

echo "GCP runtime bootstrap (interactive)"
echo "This will ask for changing values and write ${OPERATOR_ENV_FILE}."
echo

detected_project_id="$(detect_default_project)"
detected_repo_url="$(detect_default_repo_url)"
detected_repo_ref="$(detect_default_repo_ref)"

existing_project_id="${PROJECT_ID:-${detected_project_id:-gen-lang-client-0909109011}}"
existing_region="${REGION:-asia-south1}"
existing_zone="${ZONE:-asia-south1-b}"
existing_runtime_name="${RUNTIME_NAME:-option-trading-runtime-01}"
existing_runtime_machine_type="${RUNTIME_MACHINE_TYPE:-e2-standard-4}"
existing_training_machine_type="${TRAINING_MACHINE_TYPE:-n2-highcpu-16}"
existing_repo_clone_url="${REPO_CLONE_URL:-${detected_repo_url:-https://github.com/amitsajwan/option_trading.git}}"
existing_repo_ref="${REPO_REF:-${detected_repo_ref:-main}}"
existing_ghcr_prefix="${GHCR_IMAGE_PREFIX:-ghcr.io/amitsajwan}"
existing_tag="${TAG:-latest}"
existing_repository="${REPOSITORY:-option-trading-runtime}"
existing_model_bucket_name="${MODEL_BUCKET_NAME:-${existing_project_id}-option-trading-models}"
existing_runtime_config_bucket_name="${RUNTIME_CONFIG_BUCKET_NAME:-${existing_project_id}-option-trading-runtime-config}"
existing_data_sync_source="${DATA_SYNC_SOURCE:-}"
existing_model_group="${MODEL_GROUP:-banknifty_futures/h15_tp_auto}"
existing_profile_id="${PROFILE_ID:-openfe_v9_dual}"
existing_staged_config="${STAGED_CONFIG:-ml_pipeline_2/configs/research/staged_dual_recipe.default.json}"
existing_dashboard_port="${DASHBOARD_PORT:-8008}"
existing_enable_dashboard_profile="${ENABLE_DASHBOARD_PROFILE:-true}"
existing_ssh_source_ranges="${SSH_SOURCE_RANGES:-0.0.0.0/0}"
existing_dashboard_source_ranges="${DASHBOARD_SOURCE_RANGES:-0.0.0.0/0}"

prompt_var PROJECT_ID "Project ID" "${existing_project_id}"
prompt_var REGION "Region" "${existing_region}"
prompt_var ZONE "Zone" "${existing_zone}"
prompt_var REPO_CLONE_URL "Repository clone URL" "${existing_repo_clone_url}"
prompt_var REPO_REF "Repository ref/branch" "${existing_repo_ref}"
prompt_var RUNTIME_NAME "Runtime VM name" "${existing_runtime_name}"
prompt_var RUNTIME_MACHINE_TYPE "Runtime machine type" "${existing_runtime_machine_type}"
prompt_var TRAINING_MACHINE_TYPE "Training machine type" "${existing_training_machine_type}"
prompt_var GHCR_IMAGE_PREFIX "GHCR image prefix" "${existing_ghcr_prefix}"
prompt_var TAG "Image tag" "${existing_tag}"
prompt_var REPOSITORY "Artifact Registry repository id (infra compatibility)" "${existing_repository}"
prompt_var MODEL_BUCKET_NAME "Model bucket name (without gs://)" "${existing_model_bucket_name}"
prompt_var RUNTIME_CONFIG_BUCKET_NAME "Runtime config bucket name (without gs://)" "${existing_runtime_config_bucket_name}"
prompt_optional DATA_SYNC_SOURCE "Data sync source (gs://.../ml_pipeline)" "${existing_data_sync_source}"
prompt_var DASHBOARD_PORT "Dashboard port" "${existing_dashboard_port}"
prompt_var ENABLE_DASHBOARD_PROFILE "Enable dashboard profile (true/false)" "${existing_enable_dashboard_profile}"
prompt_var SSH_SOURCE_RANGES "SSH source ranges (CIDR)" "${existing_ssh_source_ranges}"
prompt_var DASHBOARD_SOURCE_RANGES "Dashboard source ranges (CIDR)" "${existing_dashboard_source_ranges}"
prompt_var MODEL_GROUP "Training default model group" "${existing_model_group}"
prompt_var PROFILE_ID "Training default profile id" "${existing_profile_id}"
prompt_var STAGED_CONFIG "Training default staged config path" "${existing_staged_config}"

MODEL_BUCKET_URL="gs://${MODEL_BUCKET_NAME}/published_models"
RUNTIME_CONFIG_BUCKET_URL="gs://${RUNTIME_CONFIG_BUCKET_NAME}/runtime"

cat > "${OPERATOR_ENV_FILE}" <<EOF
# Generated by ops/gcp/bootstrap_runtime_interactive.sh
PROJECT_ID="${PROJECT_ID}"
REGION="${REGION}"
ZONE="${ZONE}"

REPO_CLONE_URL="${REPO_CLONE_URL}"
REPO_REF="${REPO_REF}"

RUNTIME_NAME="${RUNTIME_NAME}"
RUNTIME_MACHINE_TYPE="${RUNTIME_MACHINE_TYPE}"
TRAINING_MACHINE_TYPE="${TRAINING_MACHINE_TYPE}"

GHCR_IMAGE_PREFIX="${GHCR_IMAGE_PREFIX}"
TAG="${TAG}"
REPOSITORY="${REPOSITORY}"

MODEL_BUCKET_NAME="${MODEL_BUCKET_NAME}"
RUNTIME_CONFIG_BUCKET_NAME="${RUNTIME_CONFIG_BUCKET_NAME}"
SNAPSHOT_DATA_BUCKET_NAME=""
DATA_SYNC_SOURCE="${DATA_SYNC_SOURCE}"

MODEL_BUCKET_URL="${MODEL_BUCKET_URL}"
RUNTIME_CONFIG_BUCKET_URL="${RUNTIME_CONFIG_BUCKET_URL}"
RAW_ARCHIVE_BUCKET_URL=""
SNAPSHOT_PARQUET_BUCKET_URL=""

ENABLE_DASHBOARD_PROFILE=${ENABLE_DASHBOARD_PROFILE}
DASHBOARD_PORT=${DASHBOARD_PORT}

SSH_SOURCE_RANGES="${SSH_SOURCE_RANGES}"
DASHBOARD_SOURCE_RANGES="${DASHBOARD_SOURCE_RANGES}"

MODEL_GROUP="${MODEL_GROUP}"
PROFILE_ID="${PROFILE_ID}"
STAGED_CONFIG="${STAGED_CONFIG}"

TRAINING_VM_NAME="option-trading-training-01"
EOF

echo
echo "Wrote ${OPERATOR_ENV_FILE}"
echo "Derived:"
echo "  MODEL_BUCKET_URL=${MODEL_BUCKET_URL}"
echo "  RUNTIME_CONFIG_BUCKET_URL=${RUNTIME_CONFIG_BUCKET_URL}"

if prompt_yes_no "Run full bootstrap now (terraform + runtime VM provisioning)?"; then
  read -r -p "Build runtime images now? [y/N]: " run_image_build || true
  run_image_build="${run_image_build:-N}"
  run_image_build_flag="0"
  if [[ "${run_image_build}" =~ ^[Yy]$ ]]; then
    run_image_build_flag="1"
  fi
  echo
  echo "Running bootstrap..."
  source "${OPERATOR_ENV_FILE}"
  RUN_IMAGE_BUILD="${run_image_build_flag}" "${REPO_ROOT}/ops/gcp/from_scratch_bootstrap.sh"
fi

echo
echo "Next step:"
echo "  ./ops/gcp/start_runtime_interactive.sh"
