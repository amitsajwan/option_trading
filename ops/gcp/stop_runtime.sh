#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"

if [ ! -f "${OPERATOR_ENV_FILE}" ]; then
  echo "Operator env file not found: ${OPERATOR_ENV_FILE}" >&2
  echo "Copy ops/gcp/operator.env.example to ops/gcp/operator.env and fill it in." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID in operator.env}"
ZONE="${ZONE:?set ZONE in operator.env}"
RUNTIME_NAME="${RUNTIME_NAME:?set RUNTIME_NAME in operator.env}"

status="$(
  gcloud compute instances describe "${RUNTIME_NAME}" \
    --project "${PROJECT_ID}" \
    --zone "${ZONE}" \
    --format='value(status)' 2>/dev/null || true
)"

if [ -z "${status}" ]; then
  echo "Runtime VM not found: ${RUNTIME_NAME} (${ZONE})"
  exit 0
fi

if [ "${status}" = "TERMINATED" ]; then
  echo "Runtime VM is already stopped: ${RUNTIME_NAME}"
  exit 0
fi

SKIP_GRACEFUL_STOP="${SKIP_GRACEFUL_STOP:-0}"

if [ "${SKIP_GRACEFUL_STOP}" != "1" ] && [ "${status}" = "RUNNING" ]; then
  echo "Gracefully stopping containers on ${RUNTIME_NAME} before VM shutdown..."
  gcloud compute ssh "${RUNTIME_NAME}" \
    --project "${PROJECT_ID}" \
    --zone "${ZONE}" \
    --quiet \
    --command 'cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml --profile historical --profile dashboard --profile strategy_eval down --timeout 30 2>&1 | tail -5; sudo docker ps -q | xargs -r sudo docker stop 2>/dev/null; echo "containers_drained"' \
    || echo "Warning: graceful container stop via SSH failed; proceeding with hard VM stop."
  echo "Container drain complete."
fi

echo "Stopping runtime VM ${RUNTIME_NAME} in ${ZONE}"
gcloud compute instances stop "${RUNTIME_NAME}" \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}"

echo "Runtime VM stopped: ${RUNTIME_NAME}"
