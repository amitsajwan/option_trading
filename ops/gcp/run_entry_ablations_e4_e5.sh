#!/usr/bin/env bash
# Overnight queue: E4 (harsh label) then E5 (short window). E3 optional — already completed on VM.
#
#   bash ops/gcp/run_entry_ablations_e4_e5.sh
#   bash ops/gcp/run_entry_ablations_e4_e5.sh status
#
# Reattach: tmux attach -t entry_e4_e5
set -euo pipefail

cd /opt/option_trading
export PYTHONPATH=/opt/option_trading
SESSION="entry_e4_e5"
LOG_ROOT="/tmp/entry_e4_e5"
mkdir -p "$LOG_ROOT"

CONFIGS=(
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
  for name in "${CONFIGS[@]}"; do
    latest=$(ls -td "ml_pipeline_2/artifacts/research/${name}"_* 2>/dev/null | head -1 || true)
    if [[ -n "$latest" ]]; then
      echo "  ${name}: ${latest}"
      if [[ -f "${latest}/summary.json" ]]; then
        .venv/bin/python - <<PY
import json
from pathlib import Path
s = json.loads(Path("${latest}/summary.json").read_text())
print("    status:", s.get("status"), "publish:", (s.get("publish_assessment") or {}).get("decision"))
err = s.get("error")
if err:
    print("    error:", str(err)[:180])
PY
      fi
    else
      echo "  ${name}: (no run dir)"
    fi
  done
  exit 0
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "session '$SESSION' already running. Use: $0 status"
  exit 1
fi

echo "Preflight: v1 view × v1 support pairing for E4/E5"
for name in "${CONFIGS[@]}"; do
  cfg="ml_pipeline_2/configs/research/staged_dual_recipe.${name}.json"
  .venv/bin/python - <<PY
import json
from pathlib import Path
p = Path("${cfg}")
m = json.loads(p.read_text())
view = m["views"]["stage1_view_id"]
support = m["inputs"]["support_dataset"]
if view.endswith("_v1") and support.endswith("_v2"):
    raise SystemExit(f"BAD PAIR {view} x {support} in {p}")
print("OK", p.name, view, support)
PY
done

INNER=""
for name in "${CONFIGS[@]}"; do
  cfg="ml_pipeline_2/configs/research/staged_dual_recipe.${name}.json"
  log="${LOG_ROOT}/${name}.log"
  INNER+="echo '=== $(date -u +%FT%TZ) START ${name} ===' | tee -a ${log} ; "
  INNER+=".venv/bin/python -u -m ml_pipeline_2.scripts.run_entry_s1_only_hpo --config ${cfg} --run-reuse-mode restart >> ${log} 2>&1 ; "
  INNER+="echo '=== $(date -u +%FT%TZ) END ${name} (exit $?) ===' | tee -a ${log} ; "
done
INNER+="echo 'E4+E5 DONE' ; sleep 5"

tmux new-session -d -s "$SESSION" "bash -lc \"${INNER}\""
echo "started tmux '$SESSION' — E4 -> E5"
echo "logs: ${LOG_ROOT}/"
echo "status: bash $0 status"
