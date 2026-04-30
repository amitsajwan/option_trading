#!/usr/bin/env bash
set -euo pipefail

START_DATE="${1:?usage: launch_historical_replay_tmux.sh START_DATE END_DATE [SESSION] [SPEED]}"
END_DATE="${2:?usage: launch_historical_replay_tmux.sh START_DATE END_DATE [SESSION] [SPEED]}"
SESSION_NAME="${3:-historical_replay}"
SPEED="${4:-60}"

TOPIC="${TOPIC:-market:snapshot:v1:historical}"
REPO_ROOT="${REPO_ROOT:-/opt/option_trading}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env.compose}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
LOG_PATH="${LOG_PATH:-/tmp/${SESSION_NAME}.log}"

echo "=== Baseline Mongo counts ==="
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval "
var q = {trade_date_ist:{\$gte:'${START_DATE}',\$lte:'${END_DATE}'}};
printjson({
  trade_signals: db.trade_signals_historical.countDocuments(q),
  positions: db.strategy_positions_historical.countDocuments(q),
  votes: db.strategy_votes_historical.countDocuments(q),
  traces: db.strategy_decision_traces.countDocuments(q),
  phase1_snapshots: db.phase1_market_snapshots_historical.countDocuments(q)
});
"

echo
echo "=== tmux availability ==="
command -v tmux >/dev/null || sudo apt-get install -y tmux

echo
echo "=== Reset existing session ==="
tmux kill-session -t "${SESSION_NAME}" 2>/dev/null || true

echo
echo "=== Launch replay session ${SESSION_NAME} ==="
cd "${REPO_ROOT}"
tmux new-session -d -s "${SESSION_NAME}" \
  "sudo docker compose --env-file '${ENV_FILE}' -f '${COMPOSE_FILE}' run --rm historical_replay python -m snapshot_app.historical.replay_runner --base /app/.data/ml_pipeline/parquet_data --start-date '${START_DATE}' --end-date '${END_DATE}' --speed '${SPEED}' --topic '${TOPIC}' 2>&1 | tee '${LOG_PATH}'"

sleep 3
tmux ls | grep "${SESSION_NAME}"

echo
echo "=== Initial replay status ==="
sleep 10
sudo docker exec option_trading-redis-1 redis-cli GET system:historical:replay_status || true

echo
echo "=== Replay log preview ==="
head -20 "${LOG_PATH}" 2>/dev/null || echo "log not ready"
