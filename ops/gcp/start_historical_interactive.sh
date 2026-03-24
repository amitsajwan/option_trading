#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
ENV_COMPOSE="${REPO_ROOT}/.env.compose"
CURRENT_RELEASE_DIR="${REPO_ROOT}/.run/gcp_release"
CURRENT_MANIFEST_PATH="${CURRENT_RELEASE_DIR}/current_runtime_release.json"

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

detect_default_project() {
  gcloud config get-value project 2>/dev/null | tr -d '\r'
}

require_command() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Missing required command: ${cmd}" >&2
    exit 1
  fi
}

find_python_bin() {
  if [ -x "${REPO_ROOT}/.venv/bin/python" ]; then
    printf '%s\n' "${REPO_ROOT}/.venv/bin/python"
    return 0
  fi
  if [ -x "${REPO_ROOT}/.venv/Scripts/python.exe" ]; then
    printf '%s\n' "${REPO_ROOT}/.venv/Scripts/python.exe"
    return 0
  fi
  command -v python3 || command -v python || true
}

verify_ghcr_images() {
  local prefix="$1"
  local tag="$2"
  local missing=0
  local svc=""
  for svc in snapshot_app persistence_app strategy_app market_data_dashboard; do
    if docker manifest inspect "${prefix}/${svc}:${tag}" >/dev/null 2>&1; then
      echo "ok: ${svc}:${tag}"
    else
      echo "missing: ${svc}:${tag}"
      missing=1
    fi
  done
  return "${missing}"
}

remote_gcloud() {
  local remote_command="$1"
  gcloud compute ssh "${TARGET_VM_NAME}" \
    --project "${PROJECT_ID}" \
    --zone "${ZONE}" \
    --command "${remote_command}"
}

download_current_release() {
  mkdir -p "${CURRENT_RELEASE_DIR}"
  gcloud storage cp "${RUNTIME_CONFIG_BUCKET_URL%/}/release/current_runtime_release.json" "${CURRENT_MANIFEST_PATH}" >/dev/null 2>&1 || return 1
  return 0
}

manifest_field() {
  local manifest_path="$1"
  local field_name="$2"
  "${PY_BIN}" - "${manifest_path}" "${field_name}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = payload
for part in sys.argv[2].split("."):
    value = value.get(part) if isinstance(value, dict) else None
print("" if value is None else str(value))
PY
}

require_command gcloud
PY_BIN="$(find_python_bin)"
if [ -z "${PY_BIN}" ]; then
  echo "Python is required for historical preflight and manifest parsing." >&2
  exit 1
fi

echo "Historical replay interactive setup"
echo "Press Enter to accept defaults shown in [brackets]."
echo

default_project="${PROJECT_ID:-$(detect_default_project)}"
default_project="${default_project:-gen-lang-client-0909109011}"
default_snapshot_bucket="${SNAPSHOT_PARQUET_BUCKET_URL:-}"
default_vm_name="${RUNTIME_NAME:-option-trading-runtime-01}"
default_app_image_tag="${APP_IMAGE_TAG:-${TAG:-latest}}"

prompt_var PROJECT_ID "GCP project id" "${default_project}"
prompt_var REGION "GCP region" "${REGION:-asia-south1}"
prompt_var ZONE "GCP zone" "${ZONE:-asia-south1-b}"
prompt_var TARGET_VM_NAME "Replay VM name" "${default_vm_name}"
prompt_var RUNTIME_CONFIG_BUCKET_URL "Runtime config bucket URL (gs://.../runtime)" "${RUNTIME_CONFIG_BUCKET_URL:-gs://${default_project}-option-trading-runtime-config/runtime}"
prompt_var GHCR_IMAGE_PREFIX "GHCR image prefix" "${GHCR_IMAGE_PREFIX:-ghcr.io/amitsajwan}"

if download_current_release; then
  default_app_image_tag="$(manifest_field "${CURRENT_MANIFEST_PATH}" "app_image_tag")"
fi

prompt_var APP_IMAGE_TAG "Runtime image tag" "${default_app_image_tag:-latest}"
prompt_var SNAPSHOT_PARQUET_BUCKET_URL "Snapshot parquet bucket URL (gs://.../parquet_data)" "${default_snapshot_bucket}"
prompt_var REPLAY_START_DATE "Replay start date (YYYY-MM-DD)" "${REPLAY_START_DATE:-}"
prompt_var REPLAY_END_DATE "Replay end date (YYYY-MM-DD)" "${REPLAY_END_DATE:-${REPLAY_START_DATE}}"
prompt_var REPLAY_SPEED "Replay speed (0=max speed)" "${REPLAY_SPEED:-0}"

echo
echo "Historical guardrails:"
echo "  - replay topic will remain market:snapshot:v1:historical"
echo "  - only historical services will be started"
echo "  - this flow never asks for Kite auth"
echo "  - parquet must come from ${SNAPSHOT_PARQUET_BUCKET_URL}"
echo

echo "Running local historical preflight..."
LOCAL_PREFLIGHT_OUTPUT="$("${PY_BIN}" "${REPO_ROOT}/ops/gcp/operator_preflight.py" \
  --mode historical \
  --repo-root "${REPO_ROOT}" \
  --env-file "${ENV_COMPOSE}" \
  --snapshot-parquet-bucket-url "${SNAPSHOT_PARQUET_BUCKET_URL}" \
  --start-date "${REPLAY_START_DATE}" \
  --end-date "${REPLAY_END_DATE}")" || {
    echo "${LOCAL_PREFLIGHT_OUTPUT}"
    echo "Historical preflight failed. Resolve the blockers above before replay." >&2
    exit 1
  }
echo "${LOCAL_PREFLIGHT_OUTPUT}"

runtime_status="$(
  gcloud compute instances describe "${TARGET_VM_NAME}" \
    --project "${PROJECT_ID}" \
    --zone "${ZONE}" \
    --format='value(status)' 2>/dev/null || true
)"
if [ -z "${runtime_status}" ]; then
  echo "Replay VM not found: ${TARGET_VM_NAME} (${ZONE})" >&2
  exit 1
fi

if command -v docker >/dev/null 2>&1; then
  echo "Verifying GHCR image availability for ${APP_IMAGE_TAG}..."
  if ! verify_ghcr_images "${GHCR_IMAGE_PREFIX}" "${APP_IMAGE_TAG}"; then
    echo "One or more historical services are missing for tag ${APP_IMAGE_TAG}. Aborting." >&2
    exit 1
  fi
else
  echo "docker not found on operator host; skipping GHCR manifest preflight."
fi

if [ "${runtime_status}" != "RUNNING" ]; then
  if prompt_yes_no "Replay VM is ${runtime_status}. Start it now?" "Y"; then
    gcloud compute instances start "${TARGET_VM_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}"
  else
    echo "Replay VM must be running before historical replay." >&2
    exit 1
  fi
fi

set_env_key "${ENV_COMPOSE}" "GHCR_IMAGE_PREFIX" "${GHCR_IMAGE_PREFIX}"
set_env_key "${ENV_COMPOSE}" "APP_IMAGE_TAG" "${APP_IMAGE_TAG}"
set_env_key "${ENV_COMPOSE}" "HISTORICAL_TOPIC" "market:snapshot:v1:historical"

if prompt_yes_no "Publish the current runtime config bundle before replay startup?" "N"; then
  echo
  echo "Publishing runtime config bundle so the target VM can pick up the selected image tag and env values..."
  export RUNTIME_CONFIG_BUCKET_URL REPO_ROOT
  "${REPO_ROOT}/ops/gcp/publish_runtime_config.sh"
else
  echo "Skipping runtime config publish. Historical startup will use the config already present on ${TARGET_VM_NAME}."
fi

if prompt_yes_no "Sync parquet from GCS onto ${TARGET_VM_NAME}?" "Y"; then
  remote_gcloud "
    sudo mkdir -p /opt/option_trading/.data/ml_pipeline/parquet_data &&
    sudo gcloud storage rsync '${SNAPSHOT_PARQUET_BUCKET_URL%/}' '/opt/option_trading/.data/ml_pipeline/parquet_data' --recursive
  "
fi

echo
echo "Running remote historical preflight on ${TARGET_VM_NAME}..."
REMOTE_PREFLIGHT_CMD="cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml --profile historical_replay run --rm --entrypoint python historical_replay ops/gcp/operator_preflight.py --mode historical --repo-root /app --env-file /app/.env.compose --snapshot-parquet-bucket-url '${SNAPSHOT_PARQUET_BUCKET_URL}' --start-date '${REPLAY_START_DATE}' --end-date '${REPLAY_END_DATE}' --parquet-base /app/.data/ml_pipeline/parquet_data"
remote_gcloud "${REMOTE_PREFLIGHT_CMD}"

echo
echo "Starting historical consumers on ${TARGET_VM_NAME}..."
remote_gcloud "
  cd /opt/option_trading &&
  sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml --profile historical up -d redis mongo persistence_app_historical strategy_app_historical strategy_persistence_app_historical dashboard
"

if prompt_yes_no "Run one-shot replay for ${REPLAY_START_DATE} to ${REPLAY_END_DATE} now?" "Y"; then
  remote_gcloud "
    cd /opt/option_trading &&
    sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml --profile historical_replay run --rm historical_replay --start-date ${REPLAY_START_DATE} --end-date ${REPLAY_END_DATE} --speed ${REPLAY_SPEED}
  "
fi

echo
echo "Suggested verification commands:"
echo "  gcloud compute ssh ${TARGET_VM_NAME} --project ${PROJECT_ID} --zone ${ZONE} --command \"cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml ps\""
echo "  gcloud compute ssh ${TARGET_VM_NAME} --project ${PROJECT_ID} --zone ${ZONE} --command \"curl -fsS http://127.0.0.1:8008/api/health/replay\""
echo "  gcloud compute ssh ${TARGET_VM_NAME} --project ${PROJECT_ID} --zone ${ZONE} --command \"curl -fsS http://127.0.0.1:8008/api/historical/replay/status\""
