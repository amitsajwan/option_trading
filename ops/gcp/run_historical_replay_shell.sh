#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
OPERATOR_ENV_FILE="${OPERATOR_ENV_FILE:-${REPO_ROOT}/ops/gcp/operator.env}"

if [ ! -f "${OPERATOR_ENV_FILE}" ]; then
  echo "Missing ${OPERATOR_ENV_FILE}. Run bootstrap first." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${OPERATOR_ENV_FILE}"

require_command() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Missing required command: ${cmd}" >&2
    exit 1
  fi
}

trim_cr() {
  tr -d '\r'
}

remote_gcloud() {
  local remote_command="$1"
  gcloud compute ssh "${TARGET_VM_NAME}" \
    --project "${PROJECT_ID}" \
    --zone "${ZONE}" \
    --command "${remote_command}"
}

detect_remote_repo_root() {
  remote_gcloud "for candidate in '${TARGET_REPO_ROOT:-/opt/option_trading}' \"\$HOME/option_trading\" \"\$HOME/option_trading_repo\" '/opt/option_trading'; do if [ -f \"\$candidate/docker-compose.yml\" ]; then printf '%s\n' \"\$candidate\"; exit 0; fi; done; exit 1" 2>/dev/null | trim_cr || true
}

detect_remote_compose_cmd() {
  remote_gcloud "if sudo docker compose version >/dev/null 2>&1; then printf '%s\n' 'sudo docker compose'; elif command -v docker-compose >/dev/null 2>&1; then printf '%s\n' 'sudo docker-compose'; else exit 1; fi" 2>/dev/null | trim_cr || true
}

require_command gcloud

TARGET_VM_NAME="${TARGET_VM_NAME:-${RUNTIME_NAME:-option-trading-runtime-01}}"
TARGET_REPO_ROOT="${TARGET_REPO_ROOT:-$(detect_remote_repo_root)}"
TARGET_REPO_ROOT="${TARGET_REPO_ROOT:-/opt/option_trading}"
REMOTE_COMPOSE_CMD="${REMOTE_COMPOSE_CMD:-$(detect_remote_compose_cmd)}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-.env.compose.historical}"

if [ -z "${REMOTE_COMPOSE_CMD}" ]; then
  echo "Could not detect docker compose support on ${TARGET_VM_NAME}." >&2
  exit 1
fi

PROJECT_ID="${PROJECT_ID:-gen-lang-client-0909109011}"
ZONE="${ZONE:-asia-south1-b}"
GHCR_IMAGE_PREFIX="${GHCR_IMAGE_PREFIX:-ghcr.io/amitsajwan}"
APP_IMAGE_TAG="${APP_IMAGE_TAG:-${TAG:-latest}}"
SNAPSHOT_PARQUET_BUCKET_URL="${SNAPSHOT_PARQUET_BUCKET_URL:-gs://gen-lang-client-0909109011-option-trading-snapshots/parquet_data}"
ML_PURE_RUN_ID="${ML_PURE_RUN_ID:-staged_dual_recipe_quick_publish_smoke_20260324_043508}"
ML_PURE_MODEL_GROUP="${ML_PURE_MODEL_GROUP:-banknifty_futures/h15_tp_smoke_test}"
REPLAY_START_DATE="${REPLAY_START_DATE:-}"
REPLAY_END_DATE="${REPLAY_END_DATE:-${REPLAY_START_DATE}}"
REPLAY_SPEED="${REPLAY_SPEED:-0}"
ML_PURE_MAX_FEATURE_AGE_SEC_HISTORICAL="${ML_PURE_MAX_FEATURE_AGE_SEC_HISTORICAL:-0}"

if [ -z "${REPLAY_START_DATE}" ]; then
  echo "Set REPLAY_START_DATE=YYYY-MM-DD before running." >&2
  exit 1
fi

if [ -z "${MODEL_BUCKET_URL:-}" ]; then
  echo "MODEL_BUCKET_URL is missing from operator env." >&2
  exit 1
fi

echo "Historical replay shell runner"
echo "  project: ${PROJECT_ID}"
echo "  zone: ${ZONE}"
echo "  vm: ${TARGET_VM_NAME}"
echo "  repo: ${TARGET_REPO_ROOT}"
echo "  image: ${GHCR_IMAGE_PREFIX}/*:${APP_IMAGE_TAG}"
echo "  model: ${ML_PURE_MODEL_GROUP}"
echo "  run_id: ${ML_PURE_RUN_ID}"
echo "  parquet: ${SNAPSHOT_PARQUET_BUCKET_URL}"
echo "  dates: ${REPLAY_START_DATE} -> ${REPLAY_END_DATE}"
echo "  remote env: ${REMOTE_ENV_FILE}"
echo

remote_gcloud "
  set -e
  cd '${TARGET_REPO_ROOT}'
  mkdir -p .run .data/ml_pipeline/parquet_data ml_pipeline_2/artifacts/published_models/$(dirname "${ML_PURE_MODEL_GROUP}")
  cat > '${REMOTE_ENV_FILE}' <<'EOF'
GHCR_IMAGE_PREFIX=${GHCR_IMAGE_PREFIX}
APP_IMAGE_TAG=${APP_IMAGE_TAG}
STRATEGY_ENGINE=ml_pure
HISTORICAL_TOPIC=market:snapshot:v1:historical
STRATEGY_VOTE_TOPIC_HISTORICAL=market:strategy:votes:v1:historical
TRADE_SIGNAL_TOPIC_HISTORICAL=market:strategy:signals:v1:historical
STRATEGY_POSITION_TOPIC_HISTORICAL=market:strategy:positions:v1:historical
MONGO_DB=trading_ai
MONGO_COLL_STRATEGY_VOTES_HISTORICAL=strategy_votes_historical
MONGO_COLL_TRADE_SIGNALS_HISTORICAL=trade_signals_historical
MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL=strategy_positions_historical
ML_PURE_RUN_ID=${ML_PURE_RUN_ID}
ML_PURE_MODEL_GROUP=${ML_PURE_MODEL_GROUP}
ML_PURE_MODEL_PACKAGE=
ML_PURE_THRESHOLD_REPORT=
ML_PURE_MAX_FEATURE_AGE_SEC_HISTORICAL=${ML_PURE_MAX_FEATURE_AGE_SEC_HISTORICAL}
STRATEGY_ROLLOUT_STAGE_HISTORICAL=capped_live
STRATEGY_POSITION_SIZE_MULTIPLIER_HISTORICAL=0.25
STRATEGY_ML_RUNTIME_GUARD_FILE_HISTORICAL=.run/ml_runtime_guard_live.json
EOF
"

remote_gcloud "
  set -e
  GCLOUD_BIN=\$(command -v gcloud || true)
  if [ -z \"\${GCLOUD_BIN}\" ] && [ -x /snap/bin/gcloud ]; then
    GCLOUD_BIN=/snap/bin/gcloud
  fi
  if [ -z \"\${GCLOUD_BIN}\" ] || [ ! -x \"\${GCLOUD_BIN}\" ]; then
    echo 'gcloud is not installed on the target VM' >&2
    exit 1
  fi
  mkdir -p '${TARGET_REPO_ROOT}/.data/ml_pipeline/parquet_data'
  mkdir -p '${TARGET_REPO_ROOT}/ml_pipeline_2/artifacts/published_models/$(dirname "${ML_PURE_MODEL_GROUP}")'
  \"\${GCLOUD_BIN}\" storage rsync '${SNAPSHOT_PARQUET_BUCKET_URL%/}' '${TARGET_REPO_ROOT}/.data/ml_pipeline/parquet_data' --recursive
  \"\${GCLOUD_BIN}\" storage rsync '${MODEL_BUCKET_URL%/}/${ML_PURE_MODEL_GROUP}' '${TARGET_REPO_ROOT}/ml_pipeline_2/artifacts/published_models/${ML_PURE_MODEL_GROUP}' --recursive
"

remote_gcloud "
  set -e
  cd '${TARGET_REPO_ROOT}'
  export GHCR_IMAGE_PREFIX='${GHCR_IMAGE_PREFIX}'
  export APP_IMAGE_TAG='${APP_IMAGE_TAG}'
  sudo docker exec option_trading_redis_1 redis-cli DEL 'strategy_app:consumer_lock:market:snapshot:v1:historical' >/dev/null 2>&1 || true
  ${REMOTE_COMPOSE_CMD} --env-file ${REMOTE_ENV_FILE} -f docker-compose.yml -f docker-compose.gcp.yml rm -fsv dashboard persistence_app_historical strategy_app_historical strategy_persistence_app_historical || true
  ${REMOTE_COMPOSE_CMD} --env-file ${REMOTE_ENV_FILE} -f docker-compose.yml -f docker-compose.gcp.yml --profile historical up -d redis mongo persistence_app_historical strategy_app_historical strategy_persistence_app_historical dashboard
"

remote_gcloud "
  set -e
  cd '${TARGET_REPO_ROOT}'
  export GHCR_IMAGE_PREFIX='${GHCR_IMAGE_PREFIX}'
  export APP_IMAGE_TAG='${APP_IMAGE_TAG}'
  ${REMOTE_COMPOSE_CMD} --env-file ${REMOTE_ENV_FILE} -f docker-compose.yml -f docker-compose.gcp.yml --profile historical_replay run --rm --entrypoint python historical_replay -m snapshot_app.historical.replay_runner --base /app/.data/ml_pipeline/parquet_data --topic market:snapshot:v1:historical --start-date ${REPLAY_START_DATE} --end-date ${REPLAY_END_DATE} --speed ${REPLAY_SPEED}
"

echo
echo "Verification:"
remote_gcloud "
  set -e
  cd '${TARGET_REPO_ROOT}'
  export GHCR_IMAGE_PREFIX='${GHCR_IMAGE_PREFIX}'
  export APP_IMAGE_TAG='${APP_IMAGE_TAG}'
  ${REMOTE_COMPOSE_CMD} --env-file ${REMOTE_ENV_FILE} -f docker-compose.yml -f docker-compose.gcp.yml ps
  printf '\n--- strategy_app_historical ---\n'
  ${REMOTE_COMPOSE_CMD} --env-file ${REMOTE_ENV_FILE} -f docker-compose.yml -f docker-compose.gcp.yml logs --tail 60 strategy_app_historical
  printf '\n--- replay status ---\n'
  curl -fsS http://127.0.0.1:8008/api/historical/replay/status
"
