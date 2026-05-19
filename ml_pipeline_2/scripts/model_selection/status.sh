#!/usr/bin/env bash
# status.sh — read-only summary of the most recent model-selection run.
# Safe to invoke at any time. No side effects.
set -euo pipefail

REPO_DIR=${REPO_DIR:-/opt/option_trading}
PYTHON=${PYTHON:-/opt/option_trading/.venv/bin/python}

RUN_ROOT="$REPO_DIR/ml_pipeline_2/artifacts/model_selection_runs"
if [[ ! -d "$RUN_ROOT" ]]; then
  echo "no model_selection_runs/ yet at $RUN_ROOT"
  exit 0
fi

# Latest run dir by name (run_YYYYMMDD)
LATEST=$(ls -1dt "$RUN_ROOT"/run_* 2>/dev/null | head -1 || true)
if [[ -z "$LATEST" ]]; then
  echo "no run_* under $RUN_ROOT"
  exit 0
fi
echo "=== latest run: $LATEST ==="

if [[ -f "$LATEST/state.json" ]]; then
  $PYTHON -c "
import json
with open('$LATEST/state.json') as f: s = json.load(f)
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

if [[ -f "$LATEST/leaderboard.md" ]]; then
  echo
  echo "=== leaderboard.md (head 30 lines) ==="
  head -30 "$LATEST/leaderboard.md"
fi
