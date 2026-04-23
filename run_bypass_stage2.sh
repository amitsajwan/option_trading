#!/bin/bash
set -e
cd /home/savitasajwan03/option_trading
export PYTHONPATH=/home/savitasajwan03/option_trading
LOGFILE=/home/savitasajwan03/option_trading/logs/bypass_stage2_$(date -u +%Y%m%d_%H%M%S).log
CONFIG=/home/savitasajwan03/option_trading/ml_pipeline_2/configs/research/staged_single_run.expiry_bypass_stage2.json

echo "Starting bypass_stage2 run at $(date -u)" > "$LOGFILE"
nohup /home/savitasajwan03/option_trading/.venv/bin/python -u -m ml_pipeline_2.run_research --config "$CONFIG" >> "$LOGFILE" 2>&1 &
echo $!
