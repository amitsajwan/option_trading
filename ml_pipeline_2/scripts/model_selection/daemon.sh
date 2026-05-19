#!/usr/bin/env bash
# daemon.sh — single-instance wrapper for the model selection pipeline.
#
# Usage:
#   bash daemon.sh                  # start (idempotent; resumes if state.json present)
#   bash daemon.sh status           # show status
#   bash daemon.sh stop             # graceful stop (sends SIGTERM to running tmux session)
#   bash daemon.sh tail             # tail the pipeline log
#
# Environment overrides:
#   REPO_DIR        — repo root on the host (default: /opt/option_trading)
#   CONFIG          — recipe matrix JSON path (default: ml_pipeline_2/scripts/model_selection/recipe_matrix.json)
#   OUTPUT_ROOT     — where state + cells live (default: <REPO_DIR>/ml_pipeline_2/artifacts/model_selection_runs/run_<ts>)
#   TMUX_SESSION    — tmux session name (default: model_selection)
#   PYTHON          — python binary (default: /opt/option_trading/.venv/bin/python)
#
# Design:
#   - Single-instance: PID file prevents double-launch.
#   - tmux-detached: survives ssh disconnect; tail with `daemon.sh tail`.
#   - Resumable: re-running with same OUTPUT_ROOT picks up from state.json.
#   - Safe: never auto-promotes a winner; that's a separate `promote_winner.sh`.

set -euo pipefail

REPO_DIR=${REPO_DIR:-/opt/option_trading}
CONFIG=${CONFIG:-ml_pipeline_2/scripts/model_selection/recipe_matrix.json}
TMUX_SESSION=${TMUX_SESSION:-model_selection}
PYTHON=${PYTHON:-/opt/option_trading/.venv/bin/python}

PID_DIR=${PID_DIR:-/var/run}
[ -w "$PID_DIR" ] || PID_DIR=/tmp
PID_FILE="$PID_DIR/model_selection.pid"

cd "$REPO_DIR"

# Determine OUTPUT_ROOT — keep a stable per-day path so resumability is meaningful
if [[ -z "${OUTPUT_ROOT:-}" ]]; then
  TS=$(date +%Y%m%d)
  OUTPUT_ROOT="$REPO_DIR/ml_pipeline_2/artifacts/model_selection_runs/run_${TS}"
fi
mkdir -p "$OUTPUT_ROOT"

CMD=${1:-start}

case "$CMD" in
  start)
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
      echo "tmux session '$TMUX_SESSION' already running. Use 'daemon.sh status' or 'daemon.sh tail'."
      exit 0
    fi
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "pipeline already running (pid $(cat "$PID_FILE")). Use 'daemon.sh stop' first."
      exit 0
    fi

    INNER_CMD="$PYTHON -m ml_pipeline_2.scripts.model_selection.pipeline \
      --config $CONFIG \
      --output-root $OUTPUT_ROOT 2>&1 | tee -a $OUTPUT_ROOT/pipeline.log; \
      echo DAEMON_EXIT_CODE=\$? > $OUTPUT_ROOT/exit_code"

    tmux new-session -d -s "$TMUX_SESSION" "bash -lc '$INNER_CMD'"
    echo "started tmux session: $TMUX_SESSION"
    echo "output_root: $OUTPUT_ROOT"
    echo "tail with: tail -f $OUTPUT_ROOT/pipeline.log"
    ;;

  status)
    echo "=== tmux sessions ==="
    tmux ls 2>&1 | grep -E "$TMUX_SESSION" || echo "(no '$TMUX_SESSION' session)"
    echo
    echo "=== output_root contents ==="
    ls -la "$OUTPUT_ROOT" 2>/dev/null | head -20
    if [[ -f "$OUTPUT_ROOT/state.json" ]]; then
      echo
      echo "=== state.json summary ==="
      $PYTHON -c "
import json, sys
with open('$OUTPUT_ROOT/state.json') as f:
    s = json.load(f)
print(f\"phase:           {s.get('phase')}\")
print(f\"started_at:      {s.get('started_at')}\")
print(f\"updated_at:      {s.get('updated_at')}\")
print(f\"cells_total:     {s.get('cells_total')}\")
print(f\"cells_completed: {s.get('cells_completed')}\")
print(f\"  passed:        {s.get('cells_passed')}\")
print(f\"  failed:        {s.get('cells_failed')}\")
print(f\"  errored:       {s.get('cells_errored')}\")
"
    fi
    if [[ -f "$OUTPUT_ROOT/leaderboard.md" ]]; then
      echo
      echo "=== leaderboard.md (head) ==="
      head -25 "$OUTPUT_ROOT/leaderboard.md"
    fi
    ;;

  tail)
    LOG="$OUTPUT_ROOT/pipeline.log"
    if [[ -f "$LOG" ]]; then
      tail -f "$LOG"
    else
      echo "no log at $LOG yet"
    fi
    ;;

  stop)
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
      tmux kill-session -t "$TMUX_SESSION"
      echo "killed tmux session: $TMUX_SESSION"
    else
      echo "no tmux session '$TMUX_SESSION' to stop"
    fi
    ;;

  *)
    echo "Usage: $0 {start|status|tail|stop}"
    exit 64
    ;;
esac
