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
PY="${PY:-python3}"
PARQUET_ROOT="${PARQUET_ROOT:-$HOME/parquet_data}"
ENRICH="${ENRICH:-1}"
VIEW_DATASET="${VIEW_DATASET:-stage1_entry_view_v3_candidate}"
LOGDIR="$HOME/bmm_logs"
mkdir -p "$LOGDIR"

# Ensure the config's relative parquet_root resolves to the real data dir.
DATA_LINK="$REPO/.data/ml_pipeline/parquet_data"
if [ ! -e "$DATA_LINK" ]; then
  mkdir -p "$(dirname "$DATA_LINK")"
  ln -s "$PARQUET_ROOT" "$DATA_LINK"
  echo "linked $DATA_LINK -> $PARQUET_ROOT"
fi

CONFIGS=(
  bmm_h05m_010pct
  bmm_h10m_015pct
  bmm_h15m_020pct
  bmm_h20m_030pct
  bmm_h30m_040pct
)

echo "=== BMM grid launcher ==="
echo "repo=$REPO  parquet_root=$PARQUET_ROOT  logdir=$LOGDIR"

if [ "$ENRICH" = "1" ]; then
  echo "--- [1/2] enriching $VIEW_DATASET with compression features ---"
  "$PY" ml_pipeline_2/scripts/enrich_view_compression.py \
    --parquet-root "$PARQUET_ROOT" \
    --view-dataset "$VIEW_DATASET" \
    --flat-dataset snapshots_ml_flat_v2 2>&1 | tee "$LOGDIR/enrich.log"
  echo "--- enrich done ---"
else
  echo "--- [1/2] ENRICH=0, skipping view enrichment ---"
fi

echo "--- [2/2] launching ${#CONFIGS[@]} trainings in parallel ---"
for run in "${CONFIGS[@]}"; do
  cfg="ml_pipeline_2/configs/research/staged_dual_recipe.${run}.json"
  log="$LOGDIR/${run}.log"
  echo "  launching $run -> $log"
  nohup "$PY" -m ml_pipeline_2.run_research --config "$cfg" > "$log" 2>&1 &
  echo "    pid=$!"
done
echo ""
echo "All launched. Watch with:"
echo "  python ml_pipeline_2/scripts/bmm_results.py --watch"
echo "  tail -f $LOGDIR/bmm_h15m_020pct.log"
wait
echo "=== all BMM trainings finished ==="
"$PY" ml_pipeline_2/scripts/bmm_results.py
