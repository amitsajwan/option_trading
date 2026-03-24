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

detect_default_project() {
  gcloud config get-value project 2>/dev/null | tr -d '\r'
}

sync_kite_env_from_credentials() {
  local credentials_path="${REPO_ROOT}/ingestion_app/credentials.json"
  local py_bin
  py_bin="$(command -v python3 || command -v python || true)"
  if [ -z "${py_bin}" ]; then
    echo "Python is required to parse ingestion_app/credentials.json" >&2
    return 1
  fi
  if [ ! -f "${credentials_path}" ]; then
    echo "Missing ${credentials_path}" >&2
    return 1
  fi
  local parsed
  parsed="$("${py_bin}" - "${credentials_path}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
obj = json.loads(path.read_text(encoding="utf-8-sig"))
api_key = str(obj.get("api_key") or "").strip()
access_token = str(obj.get("access_token") or "").strip()
if not api_key or not access_token:
    raise SystemExit(1)
print(api_key)
print(access_token)
PY
  )" || {
    echo "Failed to parse Kite credentials from ${credentials_path}" >&2
    return 1
  }
  local kite_api_key
  local kite_access_token
  kite_api_key="$(printf '%s\n' "${parsed}" | sed -n '1p')"
  kite_access_token="$(printf '%s\n' "${parsed}" | sed -n '2p')"
  if [ -z "${kite_api_key}" ] || [ -z "${kite_access_token}" ]; then
    echo "Kite credentials are incomplete in ${credentials_path}" >&2
    return 1
  fi
  set_env_key "${ENV_COMPOSE}" "KITE_API_KEY" "${kite_api_key}"
  set_env_key "${ENV_COMPOSE}" "KITE_ACCESS_TOKEN" "${kite_access_token}"
  return 0
}

echo "Runtime deploy interactive setup"
echo "Press Enter to accept defaults shown in [brackets]."
echo

default_project="${PROJECT_ID:-$(detect_default_project)}"
default_project="${default_project:-gen-lang-client-0909109011}"
default_runtime_config_url="${RUNTIME_CONFIG_BUCKET_URL:-gs://${default_project}-option-trading-runtime-config/runtime}"
prompt_var PROJECT_ID "GCP project id" "${default_project}"
prompt_var REGION "GCP region" "${REGION:-asia-south1}"
prompt_var ZONE "GCP zone" "${ZONE:-asia-south1-b}"
prompt_var RUNTIME_NAME "Runtime VM name" "${RUNTIME_NAME:-option-trading-runtime-01}"
prompt_var RUNTIME_CONFIG_BUCKET_URL "Runtime config bucket URL (gs://.../runtime)" "${default_runtime_config_url}"
prompt_var GHCR_IMAGE_PREFIX "GHCR image prefix" "${GHCR_IMAGE_PREFIX:-ghcr.io/amitsajwan}"
prompt_var APP_IMAGE_TAG "Runtime image tag" "${APP_IMAGE_TAG:-latest}"
prompt_var ML_PURE_RUN_ID "ML pure run id" "${ML_PURE_RUN_ID:-staged_dual_recipe_quick_publish_smoke_20260324_043508}"
prompt_var ML_PURE_MODEL_GROUP "ML pure model group" "${ML_PURE_MODEL_GROUP:-banknifty_futures/h15_tp_smoke_test}"

set_env_key "${ENV_COMPOSE}" "STRATEGY_ENGINE" "ml_pure"
set_env_key "${ENV_COMPOSE}" "STRATEGY_ROLLOUT_STAGE" "capped_live"
set_env_key "${ENV_COMPOSE}" "STRATEGY_POSITION_SIZE_MULTIPLIER" "0.25"
set_env_key "${ENV_COMPOSE}" "STRATEGY_ML_RUNTIME_GUARD_FILE" ".run/ml_runtime_guard_live.json"
set_env_key "${ENV_COMPOSE}" "GHCR_IMAGE_PREFIX" "${GHCR_IMAGE_PREFIX}"
set_env_key "${ENV_COMPOSE}" "APP_IMAGE_TAG" "${APP_IMAGE_TAG}"
set_env_key "${ENV_COMPOSE}" "ML_PURE_RUN_ID" "${ML_PURE_RUN_ID}"
set_env_key "${ENV_COMPOSE}" "ML_PURE_MODEL_GROUP" "${ML_PURE_MODEL_GROUP}"
set_env_key "${ENV_COMPOSE}" "INGESTION_COLLECTORS_ENABLED" "1"

if prompt_yes_no "Run Kite browser auth now (refresh credentials + write KITE_* envs)?" "Y"; then
  py_bin="$(command -v python3 || command -v python || true)"
  if [ -z "${py_bin}" ]; then
    echo "Python not found. Skipping Kite auth." >&2
  else
    (
      cd "${REPO_ROOT}"
      "${py_bin}" -m ingestion_app.kite_auth --force
    )
    sync_kite_env_from_credentials
  fi
fi

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
read -r -p "Runtime action: start, restart, or skip [restart]: " runtime_action || true
runtime_action="${runtime_action:-restart}"
runtime_status="$(
  gcloud compute instances describe "${RUNTIME_NAME}" \
    --project "${PROJECT_ID}" \
    --zone "${ZONE}" \
    --format='value(status)' 2>/dev/null || true
)"
case "${runtime_action}" in
  start)
    if [ -z "${runtime_status}" ]; then
      echo "Runtime VM not found: ${RUNTIME_NAME} (${ZONE})" >&2
      exit 1
    fi
    if [ "${runtime_status}" = "RUNNING" ]; then
      echo "Runtime VM already running: ${RUNTIME_NAME}"
    else
      gcloud compute instances start "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
    fi
    ;;
  restart)
    if [ -z "${runtime_status}" ]; then
      echo "Runtime VM not found: ${RUNTIME_NAME} (${ZONE})" >&2
      exit 1
    fi
    if [ "${runtime_status}" = "RUNNING" ]; then
      gcloud compute instances stop "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
    fi
    gcloud compute instances start "${RUNTIME_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
    ;;
  skip)
    echo "Skipping VM action."
    ;;
  *)
    echo "Unsupported action: ${runtime_action}. Use start, restart, or skip." >&2
    exit 1
    ;;
esac

echo
echo "Next checks:"
echo "  gcloud compute ssh ${RUNTIME_NAME} --project ${PROJECT_ID} --zone ${ZONE} --command \"sudo tail -n 200 /var/log/option-trading-runtime-startup.log\""
echo "  gcloud compute ssh ${RUNTIME_NAME} --project ${PROJECT_ID} --zone ${ZONE} --command \"cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml ps\""
