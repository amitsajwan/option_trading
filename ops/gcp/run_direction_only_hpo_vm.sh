#!/usr/bin/env bash
# Launch direction-only HPO on ML VM (run via gcloud ssh or on VM directly).
set -euo pipefail
cd /opt/option_trading
export PYTHONPATH=/opt/option_trading
LOG=/tmp/direction_only_hpo.log
PIDFILE=/tmp/direction_only_hpo.pid

if [[ "${1:-}" == "validate" ]]; then
  exec .venv/bin/python -u -m ml_pipeline_2.scripts.run_direction_only_hpo --validate-only
fi

if [[ "${1:-}" == "status" ]]; then
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "RUNNING pid=$(cat "$PIDFILE")"
    tail -20 "$LOG"
  else
    echo "NOT RUNNING"
    tail -30 "$LOG" 2>/dev/null || true
  fi
  exit 0
fi

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Already running pid=$(cat "$PIDFILE"). Use: $0 status"
  exit 1
fi

nohup .venv/bin/python -u -m ml_pipeline_2.scripts.run_direction_only_hpo \
  >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "Started direction_only_hpo pid=$(cat "$PIDFILE") log=$LOG"
echo "Monitor: tail -f $LOG"
