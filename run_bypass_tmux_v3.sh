#!/bin/bash
SESSION=bypass_stage2_v3
LOGDIR=/home/savitasajwan03/option_trading/logs
CONFIG=/home/savitasajwan03/option_trading/ml_pipeline_2/configs/research/staged_single_run.expiry_bypass_stage2.json

# Kill old session if exists
tmux kill-session -t $SESSION 2>/dev/null || true

# Create new session directly running Python
# Use -u for unbuffered, redirect to log
tmux new-session -d -s $SESSION \
  "cd /home/savitasajwan03/option_trading && export PYTHONPATH=/home/savitasajwan03/option_trading && /home/savitasajwan03/option_trading/.venv/bin/python -u -m ml_pipeline_2.run_research --config $CONFIG 2>&1 | tee $LOGDIR/bypass_stage2_v3_$(date -u +%Y%m%d_%H%M%S).log"

echo "tmux session $SESSION started"
tmux ls | grep $SESSION
