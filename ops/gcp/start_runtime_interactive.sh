#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
ENV_COMPOSE="${REPO_ROOT}/.env.compose"
CURRENT_RELEASE_DIR="${REPO_ROOT}/.run/gcp_release"
CURRENT_MANIFEST_PATH="${CURRENT_RELEASE_DIR}/current_runtime_release.json"
CURRENT_RUNTIME_ENV_PATH="${CURRENT_RELEASE_DIR}/current_ml_pure_runtime.env"

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

prompt_secret() {
  local var_name="$1"
  local prompt_text="$2"
  local entered=""
  read -r -s -p "${prompt_text}: " entered || true
  echo
  if [ -z "${entered}" ]; then
    return 1
  fi
  printf -v "${var_name}" '%s' "${entered}"
  return 0
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

check_python_modules() {
  local py_bin="$1"
  shift
  "${py_bin}" - "$@" <<'PY'
import importlib.util
import sys

missing = [name for name in sys.argv[1:] if importlib.util.find_spec(name) is None]
if missing:
    print("\n".join(missing))
    raise SystemExit(1)
PY
}

ensure_kite_auth_dependencies() {
  local py_bin="$1"
  local missing_modules=""
  missing_modules="$(check_python_modules "${py_bin}" dotenv kiteconnect urllib3 2>/dev/null)" && return 0
  echo "Missing Kite auth Python modules on this operator machine."
  if [ -n "${missing_modules}" ]; then
    echo "Missing: ${missing_modules}"
  fi
  if ! prompt_yes_no "Install required modules now (${py_bin} -m pip install python-dotenv kiteconnect)?" "Y"; then
    return 1
  fi
  "${py_bin}" -m pip install --user python-dotenv kiteconnect
  check_python_modules "${py_bin}" dotenv kiteconnect urllib3 >/dev/null
}

run_kite_auth() {
  local py_bin="$1"
  local kite_api_key="${KITE_API_KEY:-}"
  local kite_api_secret="${KITE_API_SECRET:-}"
  if ! ensure_kite_auth_dependencies "${py_bin}"; then
    return 1
  fi
  if [ -z "${kite_api_key}" ]; then
    read -r -p "KITE_API_KEY (required for browser auth): " kite_api_key || true
  fi
  if [ -z "${kite_api_secret}" ]; then
    prompt_secret kite_api_secret "KITE_API_SECRET (required for browser auth)" || true
  fi
  if [ -z "${kite_api_key}" ] || [ -z "${kite_api_secret}" ]; then
    echo "Kite browser auth requires KITE_API_KEY and KITE_API_SECRET." >&2
    return 1
  fi
  (
    cd "${REPO_ROOT}"
    KITE_API_KEY="${kite_api_key}" KITE_API_SECRET="${kite_api_secret}" "${py_bin}" -m ingestion_app.kite_auth --force
  )
}

sync_kite_env_from_credentials() {
  local credentials_path="${REPO_ROOT}/ingestion_app/credentials.json"
  local py_bin="$1"
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

download_current_release() {
  mkdir -p "${CURRENT_RELEASE_DIR}"
  gcloud storage cp "${RUNTIME_CONFIG_BUCKET_URL%/}/release/current_runtime_release.json" "${CURRENT_MANIFEST_PATH}" >/dev/null 2>&1 || return 1
  gcloud storage cp "${RUNTIME_CONFIG_BUCKET_URL%/}/release/current_ml_pure_runtime.env" "${CURRENT_RUNTIME_ENV_PATH}" >/dev/null 2>&1 || return 1
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

kite_status() {
  "${PY_BIN}" - "${REPO_ROOT}/ingestion_app/credentials.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("missing")
    raise SystemExit(0)
try:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
except Exception:
    print("stale_or_unreadable")
    raise SystemExit(0)

api_key = str(payload.get("api_key") or "").strip()
access_token = str(payload.get("access_token") or "").strip()
print("present" if api_key and access_token else "stale_or_unreadable")
PY
}

require_command gcloud
PY_BIN="$(find_python_bin)"
if [ -z "${PY_BIN}" ]; then
  echo "Python is required for runtime validation, release loading, and Kite credential parsing." >&2
  exit 1
fi

echo "Runtime deploy interactive setup"
echo "Press Enter to accept defaults shown in [brackets]."
echo

default_project="${PROJECT_ID:-$(detect_default_project)}"
default_project="${default_project:-gen-lang-client-0909109011}"
default_runtime_config_url="${RUNTIME_CONFIG_BUCKET_URL:-gs://${default_project}-option-trading-runtime-config/runtime}"
default_image_source="${IMAGE_SOURCE:-ghcr}"
prompt_var PROJECT_ID "GCP project id" "${default_project}"
prompt_var REGION "GCP region" "${REGION:-asia-south1}"
prompt_var ZONE "GCP zone" "${ZONE:-asia-south1-b}"
prompt_var RUNTIME_NAME "Runtime VM name" "${RUNTIME_NAME:-option-trading-runtime-01}"
prompt_var RUNTIME_CONFIG_BUCKET_URL "Runtime config bucket URL (gs://.../runtime)" "${default_runtime_config_url}"
prompt_var IMAGE_SOURCE "Image source (ghcr/local_build)" "${default_image_source}"
IMAGE_SOURCE="${IMAGE_SOURCE,,}"
if [[ "${IMAGE_SOURCE}" != "ghcr" && "${IMAGE_SOURCE}" != "local_build" ]]; then
  echo "Unsupported IMAGE_SOURCE=${IMAGE_SOURCE}. Use ghcr or local_build." >&2
  exit 1
fi
if [ "${IMAGE_SOURCE}" = "ghcr" ]; then
  prompt_var GHCR_IMAGE_PREFIX "GHCR image prefix" "${GHCR_IMAGE_PREFIX:-ghcr.io/amitsajwan}"
else
  GHCR_IMAGE_PREFIX="${GHCR_IMAGE_PREFIX:-ghcr.io/amitsajwan}"
fi

RELEASE_MANIFEST_PATH=""
RELEASE_ENV_PATH=""

if download_current_release; then
  CURRENT_APP_IMAGE_TAG="$(manifest_field "${CURRENT_MANIFEST_PATH}" "app_image_tag")"
  CURRENT_RUN_ID="$(manifest_field "${CURRENT_MANIFEST_PATH}" "run_id")"
  CURRENT_MODEL_GROUP="$(manifest_field "${CURRENT_MANIFEST_PATH}" "model_group")"
  echo "Latest approved release found in runtime-config bucket:"
  echo "  run_id: ${CURRENT_RUN_ID}"
  echo "  model_group: ${CURRENT_MODEL_GROUP}"
  echo "  app_image_tag: ${CURRENT_APP_IMAGE_TAG}"
  echo
  if prompt_yes_no "Use latest approved release from runtime-config bucket?" "Y"; then
    RELEASE_MANIFEST_PATH="${CURRENT_MANIFEST_PATH}"
    RELEASE_ENV_PATH="${CURRENT_RUNTIME_ENV_PATH}"
    APP_IMAGE_TAG="${CURRENT_APP_IMAGE_TAG}"
  fi
fi

if [ -z "${RELEASE_MANIFEST_PATH}" ]; then
  prompt_var RELEASE_MANIFEST_PATH "Runtime release manifest path" "${RELEASE_MANIFEST_PATH:-}"
  APP_IMAGE_TAG="$(manifest_field "${RELEASE_MANIFEST_PATH}" "app_image_tag")"
  RELEASE_ENV_RAW="$(manifest_field "${RELEASE_MANIFEST_PATH}" "runtime_env_path")"
  if [ -z "${RELEASE_ENV_RAW}" ]; then
    echo "runtime_env_path missing from ${RELEASE_MANIFEST_PATH}" >&2
    exit 1
  fi
  RELEASE_ENV_PATH="${REPO_ROOT}/${RELEASE_ENV_RAW}"
fi

if [ ! -f "${RELEASE_MANIFEST_PATH}" ]; then
  echo "Release manifest not found: ${RELEASE_MANIFEST_PATH}" >&2
  exit 1
fi
if [ ! -f "${RELEASE_ENV_PATH}" ]; then
  echo "Runtime env file not found: ${RELEASE_ENV_PATH}" >&2
  exit 1
fi

export RELEASE_ENV_PATH
"${REPO_ROOT}/ops/gcp/apply_ml_pure_release.sh"

THRESHOLD_REPORT_PATH="$(manifest_field "${RELEASE_MANIFEST_PATH}" "threshold_report")"
TRAINING_SUMMARY_PATH="$(manifest_field "${RELEASE_MANIFEST_PATH}" "training_summary")"
RUNTIME_GUARD_PATH="$(manifest_field "${RELEASE_MANIFEST_PATH}" "runtime_guard_path")"

set_env_key "${ENV_COMPOSE}" "STRATEGY_ENGINE" "ml_pure"
set_env_key "${ENV_COMPOSE}" "STRATEGY_ROLLOUT_STAGE" "capped_live"
set_env_key "${ENV_COMPOSE}" "STRATEGY_POSITION_SIZE_MULTIPLIER" "0.25"
set_env_key "${ENV_COMPOSE}" "STRATEGY_ML_RUNTIME_GUARD_FILE" "${RUNTIME_GUARD_PATH:-.run/ml_runtime_guard_live.json}"
set_env_key "${ENV_COMPOSE}" "IMAGE_SOURCE" "${IMAGE_SOURCE}"
set_env_key "${ENV_COMPOSE}" "GHCR_IMAGE_PREFIX" "${GHCR_IMAGE_PREFIX}"
set_env_key "${ENV_COMPOSE}" "APP_IMAGE_TAG" "${APP_IMAGE_TAG}"
set_env_key "${ENV_COMPOSE}" "INGESTION_COLLECTORS_ENABLED" "1"
if [ -n "${THRESHOLD_REPORT_PATH}" ]; then
  set_env_key "${ENV_COMPOSE}" "ML_PURE_THRESHOLD_REPORT" "${THRESHOLD_REPORT_PATH}"
fi
if [ -n "${TRAINING_SUMMARY_PATH}" ]; then
  set_env_key "${ENV_COMPOSE}" "ML_PURE_TRAINING_SUMMARY_PATH" "${TRAINING_SUMMARY_PATH}"
fi

KITE_STATE="$(kite_status)"
echo
echo "Kite credentials status: ${KITE_STATE}"
if [ "${KITE_STATE}" = "present" ]; then
  if prompt_yes_no "Refresh Kite browser auth now?" "N"; then
    run_kite_auth "${PY_BIN}"
    sync_kite_env_from_credentials "${PY_BIN}"
  else
    sync_kite_env_from_credentials "${PY_BIN}" || true
  fi
else
  echo "Live deploy requires valid Kite credentials."
  run_kite_auth "${PY_BIN}"
  sync_kite_env_from_credentials "${PY_BIN}"
fi

mkdir -p "${REPO_ROOT}/.run"
if [ ! -f "${REPO_ROOT}/.run/ml_runtime_guard_live.json" ] && [ "${RUNTIME_GUARD_PATH:-}" = ".run/ml_runtime_guard_live.json" ]; then
  read -r -p ".run/ml_runtime_guard_live.json missing. Create smoke guard now? [y/N]: " create_guard || true
  if [[ ! "${create_guard:-N}" =~ ^[Yy]$ ]]; then
    echo "Missing guard file. Create it manually or use an existing approved guard." >&2
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
echo "Running live preflight..."
PREFLIGHT_OUTPUT="$("${PY_BIN}" "${REPO_ROOT}/ops/gcp/operator_preflight.py" \
  --mode live \
  --repo-root "${REPO_ROOT}" \
  --env-file "${ENV_COMPOSE}" \
  --release-manifest-path "${RELEASE_MANIFEST_PATH}" \
  --image-source "${IMAGE_SOURCE}" \
  --ghcr-image-prefix "${GHCR_IMAGE_PREFIX}" \
  --credentials-path "${REPO_ROOT}/ingestion_app/credentials.json")" || {
    echo "${PREFLIGHT_OUTPUT}"
    echo "Live preflight failed. Resolve the blockers above before deploy." >&2
    exit 1
  }
echo "${PREFLIGHT_OUTPUT}"

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
