#!/usr/bin/env bash
set -euo pipefail

RUNTIME_CONFIG_BUCKET_URL="${RUNTIME_CONFIG_BUCKET_URL:?set RUNTIME_CONFIG_BUCKET_URL, for example gs://my-runtime-config/runtime}"
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGE_DIR}"' EXIT

if [ ! -f "${REPO_ROOT}/.env.compose" ]; then
  echo "Missing ${REPO_ROOT}/.env.compose" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${REPO_ROOT}/.env.compose"

float_gt() {
  awk -v lhs="$1" -v rhs="$2" 'BEGIN { exit !(lhs > rhs) }'
}

strategy_engine="$(printf '%s' "${STRATEGY_ENGINE:-}" | tr '[:upper:]' '[:lower:]')"
guard_file="${STRATEGY_ML_RUNTIME_GUARD_FILE:-}"
run_id="${ML_PURE_RUN_ID:-}"
model_group="${ML_PURE_MODEL_GROUP:-}"
model_package="${ML_PURE_MODEL_PACKAGE:-}"
threshold_report="${ML_PURE_THRESHOLD_REPORT:-}"
rollout_stage="$(printf '%s' "${STRATEGY_ROLLOUT_STAGE:-paper}" | tr '[:upper:]' '[:lower:]')"
size_multiplier="${STRATEGY_POSITION_SIZE_MULTIPLIER:-1.0}"

if [ "${strategy_engine}" = "ml_pure" ]; then
  run_mode=0
  explicit_mode=0
  if [ -n "${run_id}" ] || [ -n "${model_group}" ]; then
    run_mode=1
  fi
  if [ -n "${model_package}" ] || [ -n "${threshold_report}" ]; then
    explicit_mode=1
  fi
  if [ "${run_mode}" = "1" ] && [ "${explicit_mode}" = "1" ]; then
    echo "Invalid .env.compose: ml_pure GCP runtime config cannot mix run-id mode and explicit-path mode." >&2
    exit 1
  fi
  if [ "${run_mode}" = "0" ] && [ "${explicit_mode}" = "0" ]; then
    echo "Invalid .env.compose: ml_pure GCP runtime config requires ML_PURE_RUN_ID+ML_PURE_MODEL_GROUP or ML_PURE_MODEL_PACKAGE+ML_PURE_THRESHOLD_REPORT." >&2
    exit 1
  fi
  if [ "${run_mode}" = "1" ] && { [ -z "${run_id}" ] || [ -z "${model_group}" ]; }; then
    echo "Invalid .env.compose: ml_pure run-id mode requires both ML_PURE_RUN_ID and ML_PURE_MODEL_GROUP." >&2
    exit 1
  fi
  if [ "${explicit_mode}" = "1" ] && { [ -z "${model_package}" ] || [ -z "${threshold_report}" ]; }; then
    echo "Invalid .env.compose: ml_pure explicit-path mode requires both ML_PURE_MODEL_PACKAGE and ML_PURE_THRESHOLD_REPORT." >&2
    exit 1
  fi
  if [ "${rollout_stage}" != "capped_live" ]; then
    echo "Invalid .env.compose: ml_pure runtime requires STRATEGY_ROLLOUT_STAGE=capped_live." >&2
    exit 1
  fi
  if float_gt "${size_multiplier}" "0.25"; then
    echo "Invalid .env.compose: ml_pure runtime requires STRATEGY_POSITION_SIZE_MULTIPLIER <= 0.25." >&2
    exit 1
  fi
  if [ -z "${guard_file}" ]; then
    echo "Invalid .env.compose: ml_pure runtime requires STRATEGY_ML_RUNTIME_GUARD_FILE." >&2
    exit 1
  fi
  case "${guard_file}" in
    /*)
      echo "Invalid .env.compose: STRATEGY_ML_RUNTIME_GUARD_FILE must be repo-relative for GCP runtime sync, for example .run/ml_runtime_guard_live.json." >&2
      exit 1
      ;;
  esac
  if [ ! -f "${REPO_ROOT}/${guard_file}" ]; then
    echo "Missing ML runtime guard file referenced by .env.compose: ${REPO_ROOT}/${guard_file}" >&2
    exit 1
  fi
fi

mkdir -p "${STAGE_DIR}/ingestion_app"
cp "${REPO_ROOT}/.env.compose" "${STAGE_DIR}/.env.compose"

if [ -f "${REPO_ROOT}/ingestion_app/credentials.json" ]; then
  cp "${REPO_ROOT}/ingestion_app/credentials.json" "${STAGE_DIR}/ingestion_app/credentials.json"
fi

if [ -n "${guard_file}" ]; then
  mkdir -p "${STAGE_DIR}/$(dirname "${guard_file}")"
  cp "${REPO_ROOT}/${guard_file}" "${STAGE_DIR}/${guard_file}"
fi

echo "Syncing runtime bootstrap bundle to ${RUNTIME_CONFIG_BUCKET_URL}"
gcloud storage rsync "${STAGE_DIR}" "${RUNTIME_CONFIG_BUCKET_URL%/}" --recursive
echo "Runtime config sync complete."
