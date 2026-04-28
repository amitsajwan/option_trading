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
| 2026-04-28 | [MODEL_STATE_20260428.md](MODEL_STATE_20260428.md) | Attack label root cause: (A1) window shift, (A2) market direction label `direction_market_up_v1`, (A3) combined. Then feature grid (B) + deep HPO (C). | A2: S2_ROC=0.544, 168 trades, long_share=39% ✅ bias fixed | 🔄 Grid B running (B1–B5 with A2 label) |

---

## Key Findings Across Sessions

### What is confirmed working
- **Stage 1 entry signal:** ROC-AUC=0.683, Brier=0.216, stable across all runs
- **Stage 2 direction signal exists:** ROC-AUC 0.544–0.675 depending on label and feature set
- **`direction_market_up_v1` label fixes CE/PE bias:** long_share dropped from 93.8% → 39% on holdout (A2 run). Structurally balanced ~50/50 in training too.
- **`fixed_l0_l3_v1` (4 recipes)** beats 7-recipe catalog — higher per-recipe base rate
- **`recipe_fixed_baseline_guard_v1` policy** correctly falls back to fixed recipe when dynamic is non-inferior; prevents Stage 3 from adding noise

### What definitively does not work
- **bypass_stage2=true** — random direction, guaranteed loss, scientifically invalid
- **MIDDAY+conviction filter** — reduces S2 rows from ~57k to ~3k, kills throughput (0 holdout trades)
- **Stage 3 dynamic recipe selection** — no OVR signal in any run; always falls back to fixed recipe
- **Feature engineering to fix CE/PE bias** — oracle regime features, MIDDAY+OI/IV all tried; none moved long_share below 90%
- **Window shift alone (A1)** — oracle label signal correlation = 0.0495 (threshold 0.05); stage2_signal_check_failed. Oracle label is structurally near-zero-signal for market direction regardless of window.
- **Grid override for `windows`/`labels` keys** — disallowed by manifest validator; must use standalone `run_research` manifests instead

### Root cause of CE/PE bias
Oracle direction labels (2020–2024) are PE-dominant because 2020–2024 Indian market was PE-dominant. Model correctly learned training distribution. Holdout (Aug–Oct 2024) is CE-dominant. This is a **data/label problem**, not a model problem.

**Fix confirmed working (A2):** `direction_market_up_v1` — labels market direction by comparing `best_ce_net_return_after_cost` vs `best_pe_net_return_after_cost`. Produces ~50/50 CE/PE balance structurally. Signal is learnable (S2 CV ROC=0.544). Bias in holdout dropped from 93.8% → 39% CE.

---

## Current Session (2026-04-28)

See [MODEL_STATE_20260428.md](MODEL_STATE_20260428.md) for full detail.

| Grid | Runs | Winner | Status |
|------|------|--------|--------|
| **Grid A** | A1 window_shift, A2 market_direction, A3 combined | **A2** — S2_ROC=0.544, 168 trades, long_share=39% | ✅ Complete |
| **Grid B** `staged_grid.feature_s2_v1.json` | B1–B5 S2 feature sets (base = A2 label + S1 reuse) | TBD | 🔄 Running (B1 active, B2–B5 queued) |
| **Grid C** `staged_grid.deep_hpo_v1.json` | C1–C3 deep HPO + CV variants | TBD | ⏳ After Grid B |

**Automation:** `run_grids_from_b.sh` running in tmux session `grids_bc`. Auto-patches Grid C manifest from Grid B winner and runs sequentially.

---

## How To Resume Research

```bash
# SSH to VM
gcloud compute ssh savitasajwan03@option-trading-ml-01 --zone=asia-south1-b --project=amittrading-493606

# Check live automation status
tmux attach -t grids_bc          # live output
# or
tail -50 ml_pipeline_2/tools/auto_grids_bc.log

# Check run metrics (schema v3)
python3 /tmp/check_results2.py   # scp from tools/ if not present

# If automation died — restart from Grid B
bash ml_pipeline_2/tools/run_grids_from_b.sh

# If automation died — restart from Grid C only
PYTHONPATH=. .venv/bin/python -u -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.deep_hpo_v1.json \
  --model-group research/deep_hpo_v1 \
  --profile-id ml_pure_staged_v1

# Summary schema note (v3): metrics are at
#   cv_prechecks.stage2_cv.roc_auc            (S2 CV ROC)
#   scenario_reports.regime.segments          (holdout trades/net_return/long_share per regime)
#   publish_assessment.blocking_reasons       (gate failures)
```
