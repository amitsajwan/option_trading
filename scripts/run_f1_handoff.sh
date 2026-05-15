#!/bin/bash
# F1 walk-forward training → 2024 OOS replay handoff.
#
# Polls the ML VM until walkforward_f1 finishes, then dumps the F1 summary and
# prints the manual next steps (model publish + 2024 replay).
#
# Run as: bash run_f1_handoff.sh   (intended to live on operator workstation)

set -euo pipefail

ML_VM="option-trading-ml-01"
RUNTIME_VM="option-trading-runtime-01"
ZONE="asia-south1-b"
RESEARCH_ROOT="/opt/option_trading/ml_pipeline_2/artifacts/research"
# Match by manifest_hash — robust against entity_id naming inheritance from C1 config.
# The first F1 run launched 2026-05-15 14:42 UTC reused C1's outputs.run_name; future
# launches will produce 'walkforward_f1_no2024_*' dirs, but this hash is unique.
F1_MANIFEST_HASH="ac8b777c853ffff861b48cdfac77160b50e3cc9118a25c091a6efe7454be3abd"
POLL_INTERVAL_SEC=600  # 10 minutes

echo "=== F1 handoff: polling ML VM every $POLL_INTERVAL_SEC sec ==="
echo "looking for run with manifest_hash=$F1_MANIFEST_HASH"

while true; do
  STATUS=$(gcloud compute ssh "$ML_VM" --zone="$ZONE" --command="
    sudo python3 -c '
import json, os, sys
root = \"$RESEARCH_ROOT\"
target = \"$F1_MANIFEST_HASH\"
for name in sorted(os.listdir(root), reverse=True):
    p = os.path.join(root, name, \"run_status.json\")
    if not os.path.exists(p): continue
    try:
        s = json.load(open(p))
    except Exception: continue
    if s.get(\"manifest_hash\") == target:
        print(name, s.get(\"status\", \"unknown\"))
        sys.exit(0)
print(\"NOT_FOUND\")
'
  " 2>&1 | tail -1)

  echo "[$(date -u +%FT%TZ)]  status: $STATUS"
  case "$STATUS" in
    *completed*) echo "F1 training COMPLETED"; break ;;
    *failed*|*error*) echo "F1 training FAILED — bailing out"; exit 1 ;;
    *) sleep "$POLL_INTERVAL_SEC" ;;
  esac
done

RUN_ID=$(echo "$STATUS" | awk '{print $1}')
echo
echo "=== F1 run_id: $RUN_ID ==="
echo

# Summary check
gcloud compute ssh "$ML_VM" --zone="$ZONE" --command="
  sudo cat $RESEARCH_ROOT/$RUN_ID/summary.json | python3 -m json.tool 2>/dev/null | \
    grep -E '\"status\"|\"profit_factor\"|\"max_drawdown\"|\"block_rate\"|\"blocking_reasons\"|\"roc_auc\"' | head -20
" 2>&1 | tail -20

echo
echo "=== next steps (manual) ==="
echo "1. Publish F1 model bundle from $ML_VM:$RESEARCH_ROOT/$RUN_ID/"
echo "   - Set ML_PURE_MODEL_PACKAGE / ML_PURE_THRESHOLD_REPORT env vars on $RUNTIME_VM"
echo "2. Recreate strategy_app_historical with F1 model"
echo "3. Launch 2024 full-year replay (existing replay infra works)"
echo "4. sudo python3 /home/amits/analyze_jsonl.py  on $RUNTIME_VM"
echo
echo "F1's training/valid/holdout windows (update analyze_jsonl.py C1_*_END constants):"
echo "  train_end:    2023-04-30"
echo "  valid_end:    2023-07-31"
echo "  holdout_end:  2023-10-31"
echo "All of 2024 in the resulting replay will be POST-HOLDOUT (true OOS) — the test we want."
