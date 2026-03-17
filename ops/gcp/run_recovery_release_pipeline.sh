#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"
APPLY_RUNTIME_HANDOFF="${APPLY_RUNTIME_HANDOFF:-1}"
PUBLISH_RUNTIME_CONFIG="${PUBLISH_RUNTIME_CONFIG:-1}"

if [ ! -f "${OPERATOR_ENV_FILE}" ]; then
  echo "Operator env file not found: ${OPERATOR_ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

MODEL_GROUP="${MODEL_GROUP:?set MODEL_GROUP in operator.env}"
PROFILE_ID="${PROFILE_ID:?set PROFILE_ID in operator.env}"
RECOVERY_CONFIG="${RECOVERY_CONFIG:?set RECOVERY_CONFIG in operator.env}"
MODEL_BUCKET_URL="${MODEL_BUCKET_URL:?set MODEL_BUCKET_URL in operator.env}"

if [ ! -f "${REPO_ROOT}/.env.compose" ] && [ -f "${REPO_ROOT}/.env.compose.example" ]; then
  cp "${REPO_ROOT}/.env.compose.example" "${REPO_ROOT}/.env.compose"
  echo "Created ${REPO_ROOT}/.env.compose from .env.compose.example"
fi

if [ ! -d "${VENV_DIR}" ]; then
  python3 -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip
python -m pip install -e "${REPO_ROOT}/ml_pipeline_2"

RELEASE_JSON="$(mktemp)"
trap 'rm -f "${RELEASE_JSON}"' EXIT

python -m ml_pipeline_2.run_recovery_release \
  --config "${RECOVERY_CONFIG}" \
  --model-group "${MODEL_GROUP}" \
  --profile-id "${PROFILE_ID}" \
  --model-bucket-url "${MODEL_BUCKET_URL}" > "${RELEASE_JSON}"

echo "Release payload written to ${RELEASE_JSON}"
cat "${RELEASE_JSON}"

RELEASE_ENV_PATH="$(
  python - <<'PY' "${RELEASE_JSON}"
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["paths"]["runtime_env"])
PY
)"

if [ "${APPLY_RUNTIME_HANDOFF}" = "1" ]; then
  export RELEASE_ENV_PATH
  "${REPO_ROOT}/ops/gcp/apply_ml_pure_release.sh"
fi

if [ "${PUBLISH_RUNTIME_CONFIG}" = "1" ]; then
  export RUNTIME_CONFIG_BUCKET_URL="${RUNTIME_CONFIG_BUCKET_URL:?set RUNTIME_CONFIG_BUCKET_URL in operator.env}"
  export REPO_ROOT
  "${REPO_ROOT}/ops/gcp/publish_runtime_config.sh"
fi

echo
echo "Release pipeline complete."
echo "  runtime handoff: ${RELEASE_ENV_PATH}"
