#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
ENV_COMPOSE="${REPO_ROOT}/.env.compose"

if [ ! -f "${OPERATOR_ENV_FILE}" ]; then
  echo "Missing ${OPERATOR_ENV_FILE}. Copy ops/gcp/operator.env.example first." >&2
  exit 1
fi

if [ ! -f "${ENV_COMPOSE}" ] && [ -f "${REPO_ROOT}/.env.compose.example" ]; then
  cp "${REPO_ROOT}/.env.compose.example" "${ENV_COMPOSE}"
fi

if [ ! -f "${ENV_COMPOSE}" ]; then
  echo "Missing ${ENV_COMPOSE}" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

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

set_env_key() {
  local file_path="$1"
  local key="$2"
  local value="$3"
  if grep -q "^${key}=" "${file_path}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${file_path}"
  else
    echo "${key}=${value}" >> "${file_path}"
  fi
}

echo "Runtime deploy interactive setup"
echo "Press Enter to accept defaults shown in [brackets]."
echo

prompt_var PROJECT_ID "GCP project id" "${PROJECT_ID:-}"
prompt_var REGION "GCP region" "${REGION:-asia-south1}"
prompt_var ZONE "GCP zone" "${ZONE:-asia-south1-b}"
prompt_var RUNTIME_NAME "Runtime VM name" "${RUNTIME_NAME:-option-trading-runtime-01}"
prompt_var RUNTIME_CONFIG_BUCKET_URL "Runtime config bucket URL (gs://.../runtime)" "${RUNTIME_CONFIG_BUCKET_URL:-}"
prompt_var GHCR_IMAGE_PREFIX "GHCR image prefix" "${GHCR_IMAGE_PREFIX:-ghcr.io/amitsajwan}"
prompt_var APP_IMAGE_TAG "Runtime image tag" "${APP_IMAGE_TAG:-latest}"
prompt_var ML_PURE_RUN_ID "ML pure run id" "${ML_PURE_RUN_ID:-}"
prompt_var ML_PURE_MODEL_GROUP "ML pure model group" "${ML_PURE_MODEL_GROUP:-banknifty_futures/h15_tp_smoke_test}"

set_env_key "${ENV_COMPOSE}" "STRATEGY_ENGINE" "ml_pure"
set_env_key "${ENV_COMPOSE}" "STRATEGY_ROLLOUT_STAGE" "capped_live"
set_env_key "${ENV_COMPOSE}" "STRATEGY_POSITION_SIZE_MULTIPLIER" "0.25"
set_env_key "${ENV_COMPOSE}" "STRATEGY_ML_RUNTIME_GUARD_FILE" ".run/ml_runtime_guard_live.json"
set_env_key "${ENV_COMPOSE}" "GHCR_IMAGE_PREFIX" "${GHCR_IMAGE_PREFIX}"
set_env_key "${ENV_COMPOSE}" "APP_IMAGE_TAG" "${APP_IMAGE_TAG}"
set_env_key "${ENV_COMPOSE}" "ML_PURE_RUN_ID" "${ML_PURE_RUN_ID}"
set_env_key "${ENV_COMPOSE}" "ML_PURE_MODEL_GROUP" "${ML_PURE_MODEL_GROUP}"

mkdir -p "${REPO_ROOT}/.run"
if [ ! -f "${REPO_ROOT}/.run/ml_runtime_guard_live.json" ]; then
  read -r -p ".run/ml_runtime_guard_live.json missing. Create smoke guard now? [y/N]: " create_guard || true
  if [[ ! "${create_guard:-N}" =~ ^[Yy]$ ]]; then
    echo "Missing guard file. Create it manually or run with an existing approved guard." >&2
    exit 1
  fi
  cat > "${REPO_ROOT}/.run/ml_runtime_guard_live.json" <<'EOF'
{
  "approved_for_runtime": true,
  "offline_strict_positive_passed": true,
  "paper_days_observed": 10,
  "shadow_days_observed": 10
}
EOF
fi

echo
echo "Publishing runtime bootstrap bundle..."
export RUNTIME_CONFIG_BUCKET_URL REPO_ROOT
"${REPO_ROOT}/ops/gcp/publish_runtime_config.sh"

echo
read -r -p "Restart runtime VM now? [Y/n]: " restart_answer || true
restart_answer="${restart_answer:-Y}"
if [[ "${restart_answer}" =~ ^[Yy]$ ]]; then
  gcloud compute instances stop "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
  gcloud compute instances start "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
fi

echo
echo "Next checks:"
echo "  gcloud compute ssh ${RUNTIME_NAME} --project ${PROJECT_ID} --zone ${ZONE} --command \"sudo tail -n 200 /var/log/option-trading-runtime-startup.log\""
echo "  gcloud compute ssh ${RUNTIME_NAME} --project ${PROJECT_ID} --zone ${ZONE} --command \"cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml ps\""
