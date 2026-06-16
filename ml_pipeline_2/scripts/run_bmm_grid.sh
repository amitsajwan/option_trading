#!/usr/bin/env bash
# Launch the Lean-5 BMM grid in parallel on the ML VM.
#
#   Step 1: rebuild stage views WITH the new compression features (shared module).
#   Step 2: launch all 5 horizon configs in parallel, each with its own log.
#   Step 3: print how to watch incremental results.
#
# Usage (inside a tmux session on the VM):
#   cd ~/option_trading && bash ml_pipeline_2/scripts/run_bmm_grid.sh
#
# Env overrides:
#   PARQUET_ROOT   (default: $HOME/parquet_data)
#   REBUILD        (default: 1; set 0 to skip the stage-view rebuild)
#   REBUILD_START  (default: 2022-01-01)   REBUILD_END (default: 2024-10-31)
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
PARQUET_ROOT="${PARQUET_ROOT:-$HOME/parquet_data}"
REBUILD="${REBUILD:-1}"
REBUILD_START="${REBUILD_START:-2022-01-01}"
REBUILD_END="${REBUILD_END:-2024-10-31}"
LOGDIR="$HOME/bmm_logs"
mkdir -p "$LOGDIR"

CONFIGS=(
  bmm_h05m_010pct
  bmm_h10m_015pct
  bmm_h15m_020pct
  bmm_h20m_030pct
  bmm_h30m_040pct
)

echo "=== BMM grid launcher ==="
echo "repo=$REPO  parquet_root=$PARQUET_ROOT  logdir=$LOGDIR"

if [ "$REBUILD" = "1" ]; then
  echo "--- [1/2] rebuilding stage views WITH compression features ($REBUILD_START..$REBUILD_END) ---"
  python -m snapshot_app.historical.rebuild_stage_views_from_flat \
    --parquet-root "$PARQUET_ROOT" \
    --source-flat-dataset snapshots_ml_flat_v2 \
    --base-dataset market_base \
    --start-date "$REBUILD_START" --end-date "$REBUILD_END" \
    --no-resume 2>&1 | tee "$LOGDIR/rebuild.log"
  echo "--- rebuild done ---"
else
  echo "--- [1/2] REBUILD=0, skipping stage-view rebuild ---"
fi

echo "--- [2/2] launching ${#CONFIGS[@]} trainings in parallel ---"
for run in "${CONFIGS[@]}"; do
  cfg="ml_pipeline_2/configs/research/staged_dual_recipe.${run}.json"
  log="$LOGDIR/${run}.log"
  echo "  launching $run -> $log"
  nohup python -m ml_pipeline_2.run_research --config "$cfg" > "$log" 2>&1 &
  echo "    pid=$!"
done
echo ""
echo "All launched. Watch with:"
echo "  python ml_pipeline_2/scripts/bmm_results.py --watch"
echo "  tail -f $LOGDIR/bmm_h15m_020pct.log"
wait
echo "=== all BMM trainings finished ==="
python ml_pipeline_2/scripts/bmm_results.py
