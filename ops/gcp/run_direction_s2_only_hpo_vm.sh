#!/usr/bin/env bash
# Decoupled Stage-2 direction HPO on unified runtime VM (stop compose first).
# Default manifest: direction_s2_only_hpo_v2.json (override with DIR_S2_MANIFEST=...v1.json)
set -euo pipefail
cd /opt/option_trading
export PYTHONPATH=/opt/option_trading
DIR_S2_MANIFEST="${DIR_S2_MANIFEST:-ml_pipeline_2/configs/research/staged_dual_recipe.direction_s2_only_hpo_v2.json}"
LOG=/tmp/direction_s2_only_hpo.log
PIDFILE=/tmp/direction_s2_only_hpo.pid

if [[ "${1:-}" == "validate" ]]; then
  exec .venv/bin/python -u -m ml_pipeline_2.scripts.run_direction_s2_only_hpo \
    --config "$DIR_S2_MANIFEST" --validate-only
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

nohup .venv/bin/python -u -m ml_pipeline_2.scripts.run_direction_s2_only_hpo \
  --config "$DIR_S2_MANIFEST" \
  >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "Started direction_s2_only_hpo pid=$(cat "$PIDFILE") manifest=$DIR_S2_MANIFEST log=$LOG"
echo "Monitor: tail -f $LOG"
