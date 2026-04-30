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
| 2026-04-28/29 | [MODEL_STATE_20260428.md](MODEL_STATE_20260428.md) | Attack label root cause (A→B→C grids). A2 fixed bias. B4 best feature set. C = deep HPO running. | A2: S2_ROC=0.544, 39% long. B4: S2_ROC=0.545, 329 trades, 51% long. VOLATILE PF=1.31–1.82 ✅ consistent edge | 🔄 Grid C running (C1 active, C2+C3 queued) |

---

## Key Findings Across Sessions

### What is confirmed working
- **Stage 1 entry signal:** ROC-AUC=0.683, Brier=0.216, stable across all runs
- **Stage 2 direction signal exists:** ROC-AUC 0.544–0.545 with `direction_market_up_v1` (ceiling confirmed across 5 feature sets in Grid B)
- **`direction_market_up_v1` label fixes CE/PE bias:** long_share dropped from 93.8% → 39–51% across Grid B runs. Structurally balanced ~50/50.
- **VOLATILE regime is the reliable edge:** VOLATILE PF=1.31–1.82 across A2, B1, B4, B5 (4 independent runs). Consistent PE-dominant prediction in volatile markets. This is the path to profitability.
- **`fixed_l0_l3_v1` (4 recipes)** beats 7-recipe catalog — higher per-recipe base rate
- **`recipe_fixed_baseline_guard_v1` policy** correctly falls back to fixed recipe when dynamic is non-inferior; prevents Stage 3 from adding noise

### What definitively does not work
- **bypass_stage2=true** — random direction, guaranteed loss, scientifically invalid
- **MIDDAY+conviction filter** — reduces S2 rows from ~57k to ~3k, kills throughput (0 holdout trades)
- **Stage 3 dynamic recipe selection** — no OVR signal in any run; always falls back to fixed recipe
- **Feature engineering to fix CE/PE bias** — oracle regime features, MIDDAY+OI/IV all tried; none moved long_share below 90%
- **Window shift alone (A1)** — oracle label has max_corr=0.0495 with any feature; stage2_signal_check_failed. Irrelevant to label quality.
- **`fo_full` S2 feature set (B2)** — 1198 trades all losing (PF 0.04–0.50 per regime); too many features = noise overwhelms signal
- **Grid override for `windows`, `labels`, `cv_config` keys** — all disallowed by manifest validator; must use standalone `run_research` manifests instead

### Root cause of CE/PE bias
Oracle direction labels (2020–2024) are PE-dominant because 2020–2024 Indian market was PE-dominant. Model correctly learned training distribution. Holdout (Aug–Oct 2024) is CE-dominant. This is a **data/label problem**, not a model problem.

**Fix confirmed working (A2):** `direction_market_up_v1` — labels market direction by comparing `best_ce_net_return_after_cost` vs `best_pe_net_return_after_cost`. Produces ~50/50 CE/PE balance. Signal is learnable (S2 CV ROC=0.544). Bias dropped from 93.8% → 39% CE.

### Current Research Bottleneck (as of 2026-04-29)

Label bias is fixed. The remaining problem is the **TRENDING regime**: Aug–Oct 2024 was a bear-trend period (NIFTY peaked Sep 2024, crashed -8% by Oct). The model learned bull-trending = CE wins from 2020–2024 training data, but the holdout TRENDING = bear-trend = PE wins. TRENDING is 57% of B4 holdout trades at PF=0.31.

**The exploitable edge is VOLATILE regime** (PF=1.31–1.82 consistently). Grid D will focus here.

---

## Current Session (2026-04-28)

See [MODEL_STATE_20260428.md](MODEL_STATE_20260428.md) for full detail.

| Grid | Runs | Winner | Status |
|------|------|--------|--------|
| **Grid A** | A1 window_shift, A2 market_direction, A3 combined | **A2** — S2_ROC=0.544, 168 trades, long_share=39% | ✅ Complete |
| **Grid B** `staged_grid.feature_s2_v1.json` | B1–B5 S2 feature sets | **B4** `fo_midday_time_aware_plus_oi_iv` — S2_ROC=0.545, 329 trades, long_share=51% | ✅ Complete |
| **Grid C** standalone manifests (c1/c2/c3) | C1 deep HPO baseline, C2 cv train=180d, C3 cv valid=42d | TBD | 🔄 C1 running (tmux `grid_c`); C2+C3 queued |
| **Grid D** (planned) | D1 VOLATILE-gated, D2 threshold tightening, D3 per-regime models | TBD | ⏳ After Grid C |

**Key Grid C objective:** Confirm whether VOLATILE PF ≥ 1.5 is achievable with deeper HPO. If yes → regime-gated publish. If no → Grid D1 (VOLATILE-only S2 training).

---

## How To Resume Research

```bash
# SSH to VM
gcloud compute ssh savitasajwan03@option-trading-ml-01 --zone=asia-south1-b --project=amittrading-493606
cd /home/savitasajwan03/option_trading

# ── Grid C is running. Check status ──────────────────────────────────────
tmux attach -t grid_c                              # live C1 output
tail -50 ml_pipeline_2/tools/auto_grid_c.log       # automation log
tail -50 ml_pipeline_2/tools/c2_run.log            # C2 (starts after C1)
tail -50 ml_pipeline_2/tools/c3_run.log            # C3 (parallel with C2)
python3 /tmp/check_b_runs.py                       # metrics for completed runs

# ── If Grid C died — restart ───────────────────────────────────────────
tmux new-session -d -s grid_c
tmux send-keys -t grid_c \
  'bash /home/savitasajwan03/option_trading/ml_pipeline_2/tools/run_c_only.sh 2>&1 | tee /home/savitasajwan03/option_trading/ml_pipeline_2/tools/auto_grid_c.log' Enter

# ── Deploy updated code ─────────────────────────────────────────────────────
git checkout -- <conflicting-file> && git pull --ff-only   # reset VM local changes then pull

# ── Summary schema note (v3) ───────────────────────────────────────────────
# cv_prechecks.stage2_cv.roc_auc             (S2 CV ROC)
# scenario_reports.regime.segments.<R>.trades / .profit_factor / .long_share
# publish_assessment.blocking_reasons
```
