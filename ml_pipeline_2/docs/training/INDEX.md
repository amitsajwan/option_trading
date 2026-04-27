# Training Research Journal — Index

Each session gets a dated `MODEL_STATE_YYYYMMDD.md` file. This index is the quick-reference across all sessions.

Start a new session doc when beginning a new research iteration with a distinct hypothesis. Update the current doc after every meaningful run within the same iteration.

---

## Sessions

| Date | Doc | Hypothesis | Best Result | Outcome |
|------|-----|-----------|-------------|---------|
| 2026-04-23 | — | bypass_stage2=true to increase throughput | PF=0.41, 1,764 trades | ❌ Bypass is scientifically invalid — random direction guarantees loss |
| 2026-04-26 | [MODEL_STATE_20260426.md](MODEL_STATE_20260426.md) | Staged pipeline: proper S1+S2+S3 with HPO | 27 trades, PF=0.627 (27 trades only) | ⚠️ Stage 1 signal confirmed (ROC 0.683). S2 direction signal exists but CE/PE regime bias blocks holdout |
| 2026-04-27 | [MODEL_STATE_20260426.md](MODEL_STATE_20260426.md) | Fix CE/PE regime bias via feature engineering (oracle rolling win rates, MIDDAY+OI/IV) | 113 trades, PF=0.352, 93.8% CE | ❌ Feature engineering cannot fix label imbalance. Bias is structural in training data |

---

## Key Findings Across Sessions

### What is confirmed working
- **Stage 1 entry signal:** ROC-AUC=0.683, Brier=0.216, stable across all runs
- **Stage 2 direction signal exists:** ROC-AUC 0.568–0.675 depending on feature set
- **`fixed_l0_l3_v1` (4 recipes)** beats 7-recipe catalog — higher per-recipe base rate
- **`recipe_fixed_baseline_guard_v1` policy** correctly falls back to fixed recipe when dynamic is non-inferior; prevents Stage 3 from adding noise

### What definitively does not work
- **bypass_stage2=true** — random direction, guaranteed loss, scientifically invalid
- **MIDDAY+conviction filter** — reduces S2 rows from ~57k to ~3k, kills throughput (0 holdout trades)
- **Stage 3 dynamic recipe selection** — no OVR signal in any run; always falls back to fixed recipe
- **Feature engineering to fix CE/PE bias** — oracle regime features, MIDDAY+OI/IV all tried; none moved long_share below 90%

### Root cause of CE/PE bias
Oracle direction labels (2020–2024) are PE-dominant because 2020–2024 Indian market was PE-dominant. Model correctly learned training distribution. Holdout (Aug–Oct 2024) is CE-dominant. This is a **data/label problem**, not a model problem.

---

## Next Session

| Priority | Action | Expected Effect |
|----------|--------|----------------|
| 1 | **Shift training windows** — include Jul–Aug 2024 CE data in training | Model sees CE-dominant examples; long_share should move toward 50% |
| 2 | **Change Stage 2 label** — use Nifty market direction (up/down) instead of oracle best-recipe direction | Structurally balanced labels (~50/50 by definition) |
| 3 | **Runtime regime gate** — condition trade side on rolling CE/PE win rates at inference time | Works with existing model; doesn't fix root cause |

---

## How To Resume Research

```bash
# SSH to VM
gcloud compute ssh savitasajwan03@option-trading-ml-01 --zone=asia-south1-b --project=amittrading-493606

# Check current run status
tmux ls
cat /home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research/<run_dir>/run_status.json

# Start a new grid run
cd /home/savitasajwan03/option_trading && git pull
PYTHONPATH=/home/savitasajwan03/option_trading \
  .venv/bin/python -u -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.<name>.json \
  --model-group research/<name> \
  --profile-id ml_pure_staged_v1
```
