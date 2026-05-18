#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=/opt/option_trading
HOLDOUT_START=${HOLDOUT_START:-2024-05-01}
HOLDOUT_END=${HOLDOUT_END:-2024-07-31}

if [[ ! -d "$REPO_DIR" ]]; then
  sudo mkdir -p "$REPO_DIR"
  sudo chown "$(whoami)" "$REPO_DIR"
fi

if [[ -d "$REPO_DIR/.git" ]]; then
  cd "$REPO_DIR" && git pull --ff-only
else
  git clone https://github.com/amitsajwan/option_trading.git "$REPO_DIR"
fi

chmod +x "$REPO_DIR/ml_pipeline_2/scripts/gcp_run_grid.sh"

# Start tmux session named 'training' if not running
if ! tmux has-session -t training 2>/dev/null; then
  tmux new -s training -d 'bash -lc "REPO_DIR=/opt/option_trading HOLDOUT_START='"'"$HOLDOUT_START"'"' HOLDOUT_END='"'"$HOLDOUT_END"'"' /opt/option_trading/ml_pipeline_2/scripts/gcp_run_grid.sh |& tee /opt/option_trading/ml_pipeline_2/artifacts/research/grid_run_$(date +%Y%m%d_%H%M%S).log"'
fi

tmux ls || true
