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
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "pipeline already running (pid $(cat "$PID_FILE")). Use 'daemon.sh stop' first."
      exit 0
    fi
    # Stale PID file
    rm -f "$PID_FILE"

    # Use nohup + setsid for true daemonization that survives SSH disconnect.
    # tmux is still optional (status/tail rely on log file, not tmux).
    DRIVER_SCRIPT="$OUTPUT_ROOT/.driver.sh"
    cat > "$DRIVER_SCRIPT" <<EOF
#!/usr/bin/env bash
# Auto-generated driver. Launched detached from SSH session.
set -u
echo "DRIVER_START \$(date -u +%FT%TZ) PID=\$\$" >> "$OUTPUT_ROOT/pipeline.log"
echo \$\$ > "$PID_FILE"
$PYTHON -m ml_pipeline_2.scripts.model_selection.pipeline \\
  --config "$CONFIG" \\
  --output-root "$OUTPUT_ROOT" 2>&1 | tee -a "$OUTPUT_ROOT/pipeline.log"
RC=\${PIPESTATUS[0]}
echo "DAEMON_EXIT_CODE=\$RC" > "$OUTPUT_ROOT/exit_code"
echo "DRIVER_END \$(date -u +%FT%TZ) rc=\$RC" >> "$OUTPUT_ROOT/pipeline.log"
rm -f "$PID_FILE"
EOF
    chmod +x "$DRIVER_SCRIPT"

    # nohup: ignore SIGHUP. setsid: new process group + session (detach from terminal).
    # `&` + `disown`: background and don't track in shell job list.
    nohup setsid bash "$DRIVER_SCRIPT" </dev/null >/dev/null 2>&1 &
    DRIVER_PID=$!
    disown $DRIVER_PID 2>/dev/null || true

    # Wait briefly for PID file or driver start log to confirm launch
    for i in 1 2 3 4 5 6 7 8 9 10; do
      [[ -f "$PID_FILE" ]] && break
      sleep 0.5
    done

    # Also start tmux for interactive tailing convenience (best-effort)
    tmux new-session -d -s "$TMUX_SESSION" "tail -F $OUTPUT_ROOT/pipeline.log" 2>/dev/null || true

    if [[ -f "$PID_FILE" ]]; then
      echo "pipeline daemonized: pid=$(cat "$PID_FILE")  driver_pid=$DRIVER_PID"
    else
      echo "WARNING: PID file not written; driver may have failed to start"
      echo "check log: $OUTPUT_ROOT/pipeline.log"
    fi
    echo "output_root: $OUTPUT_ROOT"
    echo "tail with: tail -f $OUTPUT_ROOT/pipeline.log"
    echo "tmux (best-effort): $TMUX_SESSION"
    ;;

  status)
    echo "=== daemon process ==="
    if [[ -f "$PID_FILE" ]]; then
      PID=$(cat "$PID_FILE")
      if kill -0 "$PID" 2>/dev/null; then
        echo "running: pid=$PID"
        ps -o pid,etime,pcpu,pmem,cmd --no-headers -p "$PID" 2>/dev/null || true
        # Also show actively-running child python (the trainer)
        ACTIVE_TRAIN=$(pgrep -f train_option_pnl_mvp | head -1)
        [[ -n "$ACTIVE_TRAIN" ]] && ps -o pid,etime,pcpu,pmem,cmd --no-headers -p "$ACTIVE_TRAIN" 2>/dev/null || true
      else
        echo "stale PID file: pid=$PID NOT running"
      fi
    else
      echo "no PID file at $PID_FILE"
    fi
    echo
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
    if [[ -f "$PID_FILE" ]]; then
      PID=$(cat "$PID_FILE")
      if kill -0 "$PID" 2>/dev/null; then
        # Kill the whole process group so child trainers + tee also die
        kill -TERM -- "-$PID" 2>/dev/null || kill -TERM "$PID"
        sleep 2
        kill -KILL -- "-$PID" 2>/dev/null || true
        echo "killed daemon pid=$PID"
      else
        echo "stale PID file (pid $PID not running)"
      fi
      rm -f "$PID_FILE"
    fi
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
      tmux kill-session -t "$TMUX_SESSION"
      echo "killed tmux session: $TMUX_SESSION"
    fi
    # Also reap any orphaned trainer / pipeline processes
    pkill -TERM -f "ml_pipeline_2.scripts.model_selection.pipeline" 2>/dev/null || true
    pkill -TERM -f "ml_pipeline_2.scripts.train_option_pnl_mvp" 2>/dev/null || true
    sleep 1
    pkill -KILL -f "ml_pipeline_2.scripts.model_selection.pipeline" 2>/dev/null || true
    pkill -KILL -f "ml_pipeline_2.scripts.train_option_pnl_mvp" 2>/dev/null || true
    ;;

  *)
    echo "Usage: $0 {start|status|tail|stop}"
    exit 64
    ;;
esac
