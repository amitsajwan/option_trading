#!/usr/bin/env bash
# Replay a previously-promoted day through the historical engine stack,
# optionally with a config preset that overrides strategy_app_historical env.
#
# Pre-requisite: that day's snapshots are in `phase1_market_snapshots_historical`
# (use promote_today_to_historical.py for live-collected days).
#
# Usage:
#   ./run_replay.sh                                # today IST, baseline preset
#   ./run_replay.sh 2026-05-27                     # specific date, baseline preset
#   ./run_replay.sh 2026-05-27 baseline            # explicit preset
#   ./run_replay.sh 2026-05-27 r1s_no_time_window  # custom preset
#   ./run_replay.sh 2026-05-27 baseline 1800       # date + preset + eval-side speed
#
# Presets live in ./replay_configs/<name>.env — each is a small env-file that
# OVERLAYS .env.compose for strategy_app_historical only. Define one per
# experiment hypothesis: it doubles as documentation of what the experiment
# changed.
#
# Eval-side `speed` controls how fast the orchestrator pumps snapshots into
# the engine (0 = as-fast-as-possible). The dashboard REPLAY tab has its own
# independent UI speed control once results are in.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/option_trading}"
COMPOSE_ENV="${REPO_ROOT}/.env.compose"
PRESET_DIR="${REPO_ROOT}/ops/gcp/replay_configs"
API="${EVAL_API:-http://127.0.0.1:8008/api/strategy/evaluation/runs}"

DATE="${1:-$(TZ=Asia/Kolkata date +%F)}"
PRESET="${2:-baseline}"
SPEED="${3:-0}"

PRESET_FILE="${PRESET_DIR}/${PRESET}.env"
if [[ ! -f "${PRESET_FILE}" ]]; then
  echo "ERROR: preset not found: ${PRESET_FILE}" >&2
  echo "Available presets:" >&2
  ls -1 "${PRESET_DIR}"/*.env 2>/dev/null | sed 's|.*/||; s|\.env$||' | sed 's/^/  /' >&2
  exit 2
fi

cd "${REPO_ROOT}"

echo "[$(date -u +%FT%TZ)] date=${DATE} preset=${PRESET} speed=${SPEED}"
echo "[$(date -u +%FT%TZ)] preset content:"
sed 's/^/  /' "${PRESET_FILE}"
echo ""

# Start dependencies that DON'T need preset overrides (orchestrator + persistence).
echo "[$(date -u +%FT%TZ)] starting orchestrator + historical persistence"
sudo docker compose --env-file "${COMPOSE_ENV}" up -d \
  strategy_persistence_app_historical \
  persistence_app_historical \
  strategy_eval_orchestrator 2>&1 | tail -6

# strategy_app_historical needs the preset overlay applied. Stop it, then start
# it with the overlay env-file appended. Second --env-file wins on duplicate keys.
# (Note: docker compose merges env_file directives left-to-right; the last
# value wins. .env.compose is loaded from the service's `env_file:` block;
# our --env-file flag here drives ${VAR:-default} substitution in the compose
# file itself. So putting overrides in BOTH paths is safest.)
echo "[$(date -u +%FT%TZ)] stopping strategy_app_historical to apply preset"
sudo docker compose --env-file "${COMPOSE_ENV}" stop strategy_app_historical 2>&1 | tail -3 || true
sudo docker exec option_trading-redis-1 redis-cli DEL \
  strategy_app_historical:consumer_lock:market:snapshot:v1:historical 2>&1 || true

echo "[$(date -u +%FT%TZ)] starting strategy_app_historical with preset overlay"
sudo docker compose \
  --env-file "${COMPOSE_ENV}" \
  --env-file "${PRESET_FILE}" \
  up -d --force-recreate strategy_app_historical 2>&1 | tail -3

# Wait for consumer subscription.
echo "[$(date -u +%FT%TZ)] waiting for strategy_app_historical to subscribe"
for i in $(seq 1 60); do
  if sudo docker logs option_trading-strategy_app_historical-1 --since 2m 2>&1 \
     | grep -q "strategy consumer subscribed"; then
    echo "  historical consumer ready"
    break
  fi
  sleep 2
done

# Verify the preset actually took effect.
echo "[$(date -u +%FT%TZ)] preset env applied to strategy_app_historical:"
PRESET_KEYS=$(awk -F= '!/^#/ && /=/ {print $1}' "${PRESET_FILE}" | tr '\n' '|' | sed 's/|$//')
if [[ -n "${PRESET_KEYS}" ]]; then
  sudo docker exec option_trading-strategy_app_historical-1 printenv \
    | grep -E "^(${PRESET_KEYS})=" | sed 's/^/  /' || echo "  (no matching env vars surfaced — preset may be empty)"
fi

# Trigger the replay.
echo "[$(date -u +%FT%TZ)] POST ${API}  date=${DATE} speed=${SPEED}"
RESP=$(curl -s -X POST "${API}" \
  -H "Content-Type: application/json" \
  -d "{\"dataset\":\"historical\",\"date_from\":\"${DATE}\",\"date_to\":\"${DATE}\",\"speed\":${SPEED}}")
echo "${RESP}" | python3 -m json.tool
RUN_ID=$(echo "${RESP}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("run_id",""))')

if [[ -z "${RUN_ID}" ]]; then
  echo "ERROR: no run_id returned" >&2
  exit 1
fi
echo "run_id=${RUN_ID}"

# Poll status.
echo "[$(date -u +%FT%TZ)] polling for completion"
for i in $(seq 1 240); do
  STATUS=$(curl -s "${API}/${RUN_ID}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || true)
  echo "  poll #${i} status=${STATUS}"
  case "${STATUS}" in
    completed|failed|cancelled) break ;;
  esac
  sleep 5
done

echo ""
echo "==== REPLAY COMPLETE ===="
echo "date    = ${DATE}"
echo "preset  = ${PRESET}"
echo "run_id  = ${RUN_ID}"
echo ""
echo "Dashboard REPLAY tab (pick date + run_id, then play at any UI speed):"
echo "  http://<vm-ip>:8008/?mode=replay&date=${DATE}&run_id=${RUN_ID}"
echo ""
echo "Summary JSON:"
echo "  curl -s 'http://127.0.0.1:8008/api/strategy/evaluation/summary?dataset=historical&date_from=${DATE}&date_to=${DATE}&run_id=${RUN_ID}' | python3 -m json.tool"
