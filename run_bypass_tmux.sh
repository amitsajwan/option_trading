#!/bin/bash
SESSION=bypass_stage2
LOGDIR=/home/savitasajwan03/option_trading/logs
ARTIFACTDIR=/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research
CONFIG=/home/savitasajwan03/option_trading/ml_pipeline_2/configs/research/staged_single_run.expiry_bypass_stage2.json

# Kill old session if exists
tmux kill-session -t $SESSION 2>/dev/null

# Create new session and run training
tmux new-session -d -s $SESSION -n training "cd /home/savitasajwan03/option_trading && export PYTHONPATH=/home/savitasajwan03/option_trading && /home/savitasajwan03/option_trading/.venv/bin/python -u -m ml_pipeline_2.run_research --config $CONFIG --run-reuse-mode fail_if_exists 2>&1 | tee $LOGDIR/bypass_stage2_tmux_$(date -u +%Y%m%d_%H%M%S).log"

echo "tmux session $SESSION started. Attach with: tmux attach -t $SESSION"
tmux ls
