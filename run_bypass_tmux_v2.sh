#!/bin/bash
set -e
SESSION=bypass_stage2
LOGDIR=/home/savitasajwan03/option_trading/logs
ARTIFACTDIR=/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research
CONFIG=/home/savitasajwan03/option_trading/ml_pipeline_2/configs/research/staged_single_run.expiry_bypass_stage2.json

# Kill old session if exists
tmux kill-session -t $SESSION 2>/dev/null || true

# Create wrapper script that runs training and logs everything
WRAPPER=/tmp/bypass_stage2_wrapper.sh
cat > $WRAPPER << 'INNEREOF'
#!/bin/bash
set -e
cd /home/savitasajwan03/option_trading
export PYTHONPATH=/home/savitasajwan03/option_trading
LOGFILE=/home/savitasajwan03/option_trading/logs/bypass_stage2_tmux_$(date -u +%Y%m%d_%H%M%S).log
echo "Starting at $(date -u)" > "$LOGFILE"
/home/savitasajwan03/option_trading/.venv/bin/python -u -m ml_pipeline_2.run_research \
  --config /home/savitasajwan03/option_trading/ml_pipeline_2/configs/research/staged_single_run.expiry_bypass_stage2.json \
  --run-reuse-mode fail_if_exists >> "$LOGFILE" 2>&1
echo "Finished at $(date -u) with exit code $?" >> "$LOGFILE"
INNEREOF
chmod +x $WRAPPER

# Create new tmux session running the wrapper
tmux new-session -d -s $SESSION -n training "$WRAPPER"

echo "tmux session $SESSION started"
tmux ls | grep $SESSION
