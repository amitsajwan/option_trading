#!/usr/bin/env bash
set -euo pipefail
START_DATE="${1:-2024-08-01}"
END_DATE="${2:-2024-10-31}"
SESSION="${3:-ml_entry_replay}"
REPO="${REPO:-/opt/option_trading}"
LOG="/tmp/${SESSION}.log"

cd "${REPO}"
tmux kill-session -t "${SESSION}" 2>/dev/null || true
tmux new-session -d -s "${SESSION}" \
  "sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml --profile historical_replay run --rm historical_replay python -m snapshot_app.historical.replay_runner --base /app/.data/ml_pipeline/parquet_data --topic market:snapshot:v1:historical --start-date ${START_DATE} --end-date ${END_DATE} --speed 0 2>&1 | tee ${LOG}"
echo "tmux session: ${SESSION}"
echo "log: ${LOG}"
tmux ls | grep "${SESSION}" || true
sleep 5
tail -5 "${LOG}" 2>/dev/null || true
