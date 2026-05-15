#!/bin/bash
# Polls F1 walk-forward training. When it reaches status='completed', launches
# Path B1 (option-aware label retrain) in a new tmux session on the ML VM.
#
# Safety: only launches B1 if F1's status is 'completed'. If F1 'failed' or
# 'error', this script exits without launching B1 — operator decides next step.
#
# Run on operator workstation; intended to leave running overnight.
#
# Usage: bash launch_pathb1_when_f1_done.sh

set -euo pipefail

ML_VM="option-trading-ml-01"
ZONE="asia-south1-b"
RESEARCH_ROOT="/opt/option_trading/ml_pipeline_2/artifacts/research"
F1_MANIFEST_HASH="ac8b777c853ffff861b48cdfac77160b50e3cc9118a25c091a6efe7454be3abd"
B1_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_b1_optcost_200bps.json"
POLL_INTERVAL_SEC=600  # 10 minutes

echo "=== F1 → B1 chain launcher ==="
echo "F1 manifest_hash: $F1_MANIFEST_HASH"
echo "B1 config: $B1_CONFIG"
echo "Polls every $POLL_INTERVAL_SEC sec"
echo

# Phase 1: wait for F1
while true; do
  RESULT=$(gcloud compute ssh "$ML_VM" --zone="$ZONE" --command="
    sudo python3 -c '
import json, os, sys
root = \"$RESEARCH_ROOT\"
target = \"$F1_MANIFEST_HASH\"
for name in sorted(os.listdir(root), reverse=True):
    p = os.path.join(root, name, \"run_status.json\")
    if not os.path.exists(p): continue
    try:
        s = json.load(open(p))
    except: continue
    if s.get(\"manifest_hash\") == target:
        print(name, s.get(\"status\", \"unknown\"), s.get(\"active_stage\", \"\"))
        sys.exit(0)
print(\"NOT_FOUND\")
'
  " 2>&1 | tail -1)
  echo "[$(date -u +%FT%TZ)]  F1: $RESULT"

  case "$RESULT" in
    *completed*) echo; echo "F1 COMPLETED — proceeding to launch B1"; break ;;
    *failed*|*error*) echo; echo "F1 FAILED — NOT launching B1. Operator decision required."; exit 1 ;;
    *NOT_FOUND*) echo "F1 run not yet started — waiting"; sleep $POLL_INTERVAL_SEC ;;
    *) sleep $POLL_INTERVAL_SEC ;;
  esac
done

# Phase 2: dump F1 summary for visibility
echo
echo "=== F1 summary (for context before B1 launch) ==="
F1_RUN_ID=$(echo "$RESULT" | awk '{print $1}')
gcloud compute ssh "$ML_VM" --zone="$ZONE" --command="
  sudo cat $RESEARCH_ROOT/$F1_RUN_ID/summary.json | python3 -m json.tool 2>/dev/null | \
    grep -E '\"status\"|\"profit_factor\"|\"max_drawdown\"|\"block_rate\"|\"blocking_reasons\"|\"roc_auc\"' | head -25
" 2>&1 | tail -25

# Phase 3: launch B1
echo
echo "=== launching B1 (option-aware label, cost_per_trade=0.02) ==="
gcloud compute ssh "$ML_VM" --zone="$ZONE" --command="
cd /opt/option_trading
LOGFILE=/tmp/pathb1_optcost_\$(date +%Y%m%d_%H%M%S).log
echo \"LOGFILE=\$LOGFILE\"

sudo tmux new-session -d -s pathb1 \"cd /opt/option_trading && sudo /opt/option_trading/.venv/bin/python -m ml_pipeline_2.run_research --config $B1_CONFIG 2>&1 | tee \$LOGFILE\"
sleep 6
sudo tmux ls
echo
sudo ps -o pid,pcpu,pmem,etime,cmd --no-headers -p \$(pgrep -f 'b1_optcost_200bps' | head -1) 2>&1 | head -3
" 2>&1 | tail -15

echo
echo "=== B1 launched. Manifest hash will be visible after ~30s in run_status.json ==="
echo "Check progress later with:"
echo "  gcloud compute ssh $ML_VM --zone=$ZONE --command='sudo ls -lat $RESEARCH_ROOT/ | head -3'"
