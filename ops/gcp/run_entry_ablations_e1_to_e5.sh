#!/usr/bin/env bash
# Sequential entry stage-1 ablations E1–E5.
# Each is a single fixed-config training run (no HPO) varying ONE variable from
# the C1 baseline (which had holdout AUC 0.683, drift 0.017, 22K trades PF=3.99).
#
# E1: pure C1 reproduction        (labeler=entry_best_recipe_v1, view=v1, features=fo_full, train=2020-2024)
# E2: E1 + view=v2                (test view migration)
# E3: E1 + features=fo_velocity_v1 (test new velocity features)
# E4: E1 + labeler=entry_bn_5m_100pts_v1 (test if harsh label is the break)
# E5: E1 + train=2022-2024        (test if 2020-2021 history matters)
#
# Usage:
#   bash ops/gcp/run_entry_ablations_e1_to_e5.sh          # run all 5 in tmux
#   bash ops/gcp/run_entry_ablations_e1_to_e5.sh status   # show status / tail logs
#
# Reattach: tmux attach -t entry_ablations
set -euo pipefail

cd /opt/option_trading
export PYTHONPATH=/opt/option_trading
SESSION="entry_ablations"
LOG_ROOT="/tmp/entry_ablations"
mkdir -p "$LOG_ROOT"

CONFIGS=(
  "entry_s1_ablate_e1_c1_repro"
  "entry_s1_ablate_e2_view_v2"
  "entry_s1_ablate_e3_velocity"
  "entry_s1_ablate_e4_harsh_label"
  "entry_s1_ablate_e5_short_window"
)

if [[ "${1:-}" == "status" ]]; then
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "tmux session '$SESSION' is RUNNING"
    tmux capture-pane -pt "$SESSION" 2>/dev/null | tail -30
  else
    echo "tmux session '$SESSION' is NOT RUNNING"
  fi
  echo
  echo "=== latest run dirs ==="
  for name in "${CONFIGS[@]}"; do
    latest=$(ls -td "ml_pipeline_2/artifacts/research/${name}"_* 2>/dev/null | head -1 || true)
    if [[ -n "$latest" ]]; then
      echo "  ${name}: ${latest}"
      [[ -f "${latest}/summary.json" ]] && echo "    summary.json present" || echo "    (still running or failed)"
    else
      echo "  ${name}: (no run dir yet)"
    fi
  done
  exit 0
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "session '$SESSION' already running. Use: $0 status  or  tmux attach -t $SESSION"
  exit 1
fi

# Build the inner command that runs all 5 sequentially
INNER=""
for name in "${CONFIGS[@]}"; do
  cfg="ml_pipeline_2/configs/research/staged_dual_recipe.${name}.json"
  log="${LOG_ROOT}/${name}.log"
  INNER+="echo '=== $(date -u +%FT%TZ) START ${name} ===' | tee -a ${log} ; "
  INNER+=".venv/bin/python -u -m ml_pipeline_2.scripts.run_entry_s1_only_hpo --config ${cfg} >> ${log} 2>&1 ; "
  INNER+="echo '=== $(date -u +%FT%TZ) END ${name} (exit $?) ===' | tee -a ${log} ; "
done
INNER+="echo 'ALL ABLATIONS DONE' ; sleep 5"

tmux new-session -d -s "$SESSION" "bash -lc \"${INNER}\""
echo "started tmux session '$SESSION'. Configs queued:"
for name in "${CONFIGS[@]}"; do
  echo "  - ${name}"
done
echo "logs: ${LOG_ROOT}/<name>.log"
echo "reattach: tmux attach -t ${SESSION}"
echo "status:   bash $0 status"
