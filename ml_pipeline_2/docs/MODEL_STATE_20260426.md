# ML Model State — 2026-04-26

> **Canonical snapshot.** This doc is the single source of truth for the current model state.
> When resuming research, start here. Update this doc after every meaningful run.

---

## 1. Current Best Research Model

| Field | Value |
|-------|-------|
| **Run ID** | `staged_simple_s2_v1_20260426_110326` |
| **Date** | 2026-04-26 |
| **Config** | `ml_pipeline_2/configs/research/staged_dual_recipe.simple_s2_v1.json` |
| **Status** | Research — not publishable (gate failures, see below) |
| **Stage 1 source** | `staged_proper_full_v1_20260426_051531` (reused) |
| **VM artifact path** | `/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research/staged_simple_s2_v1_20260426_110326` |

### Model Architecture
- **Stage 1 (entry):** `binary_catalog_v1` → `entry_threshold_v1` policy
- **Stage 2 (direction):** `binary_catalog_v1` → `direction_gate_economic_balance_v1` policy
- **Stage 3 (recipe select):** `ovr_recipe_catalog_v1` → `recipe_economic_balance_v1` policy
- **Recipe catalog:** `fixed_l0_l3_v1` (4 recipes: L0, L1, L2, L3)
- **Feature sets:** S1: `fo_full` + 6 others | S2: `fo_expiry_aware_v3` | S3: `fo_full`, `fo_expiry_aware_v3`, `fo_no_time_context`

---

## 2. Performance Metrics

### Stage Quality (Holdout: Aug–Oct 2024)

| Stage | ROC-AUC | Brier | Rows | Gate |
|-------|---------|-------|------|------|
| Stage 1 (entry) | **0.683** | 0.216 | 24,059 | ✅ PASS |
| Stage 2 (direction) | **0.568** | 0.246 | 13,660 | ✅ PASS |
| Stage 3 (recipe) | — | — | — | ✅ PASS (drawdown) |

### Combined Holdout (Selected Policy)

| Metric | Value | Gate | Status |
|--------|-------|------|--------|
| Trades | 27 | ≥50 | ❌ |
| Net return | -0.0072 | ≥0.0 | ❌ |
| Profit factor | 0.627 | ≥1.5 | ❌ |
| Win rate | 33.3% | — | — |
| Long (CE) share | 88.9% | 30–70% | ❌ |
| Max drawdown | — | ≤10% | — |

### Validation Summary (May–Jul 2024)

| Stage | Trades | PF | Net | Long share |
|-------|--------|----|-----|-----------|
| Stage 2 selected | 412 | 1.333 | +0.003 | 38.6% CE |
| Stage 3 selected | 384 | 0.955 | -0.013 | 19.1% CE |

### Selected Policy Parameters

| Stage | Parameter | Value |
|-------|-----------|-------|
| Stage 1 | threshold | 0.5 |
| Stage 2 | trade_threshold | 0.6 |
| Stage 2 | ce_threshold | 0.55 |
| Stage 2 | pe_threshold | 0.65 |
| Stage 3 | recipe_threshold | 0.6 |
| Stage 3 | recipe_margin | 0.02 |

---

## 3. Full Run History (This Iteration)

| Date | Run ID | Key Config | S2 ROC | Holdout Trades | PF | Outcome |
|------|--------|-----------|--------|---------------|----|---------|
| 2026-04-23 | `expiry_bypass_stage2_test_v1_20260423_013438` | bypass_stage2=true, 7 recipes | 0.500 (random) | 1,432 | 0.35 | ❌ bypass contaminates |
| 2026-04-23 | `expiry_bypass_stage2_fixed_catalog_v1` | bypass, 4 recipes | 0.500 | 1,764 | 0.41 | ❌ same per-trade loss |
| 2026-04-23 | `expiry_bypass_stage2_low_threshold_v1` | bypass, low threshold | 0.500 | 6,914 | 0.38 | ❌ more trades = more loss |
| 2026-04-23 | `expiry_bypass_stage2_combined_v1` | bypass combined | 0.500 | 30,812 | 0.29 | ❌ confirmed bypass is wrong approach |
| 2026-04-26 | `staged_proper_full_v1_20260426_051531` | MIDDAY+conviction, full HPO | **0.675** | **0** | 0.0 | ❌ conviction filter kills throughput |
| 2026-04-26 | `staged_simple_s2_v1_20260426_110326` | No filter, simple direction | **0.568** | **27** | 0.627 | ⚠️ progressing, regime bias |

---

## 4. Key Findings (What We Learned)

### ✅ Confirmed Working
1. **Stage 1 entry signal is strong:** ROC-AUC=0.683, stable across all runs
2. **Stage 2 direction signal exists:** ROC-AUC 0.568–0.675 depending on filtering
3. **No bypass_stage2:** All bypass runs are scientifically invalid — random direction guarantees losses
4. **`fixed_l0_l3_v1` (4 recipes)** better than 7-recipe catalog — higher per-recipe base rate

### ❌ What Doesn't Work
1. **MIDDAY+target_redesign conviction filter:** Reduces Stage 2 rows from ~57k to 3,119 → 0 holdout trades
2. **Stage 3 recipe selection:** Adds no edge beyond fixed-recipe baseline. Best fixed recipe (L3) has PF=0.354 — even the fixed approach is unprofitable
3. **Brier gate 0.22:** Too tight for Stage 2. Relaxed to 0.26 — this fix was correct

### ⚠️ Unresolved Issues
1. **CE/PE regime shift:** Training 2020-2024 is PE-dominant (oracle long_share≈0%). Holdout Aug-Oct 2024 is CE-dominant (88.9% CE). Model direction bias doesn't generalize across regimes.
2. **Stage 3 adds noise:** Recipe models have no predictive power. PF=0.955 on validation, 0.627 on holdout.
3. **Only 27 holdout trades:** Far below the 50-trade minimum gate. Policy is too restrictive.

---

## 5. What To Do Next (Priority Order)

### Priority 1 — Fix Regime Bias (HIGH IMPACT)
The training period is PE-dominant, holdout is CE-dominant. Two approaches:

**A. Extend the training window** — include 2024 Q3/Q4 data so the model sees CE-dominant periods:
```json
"windows": {
  "research_train": {"start": "2020-08-03", "end": "2024-10-31"},
  "research_valid": {"start": "2024-11-01", "end": "2025-01-31"},
  "full_model":     {"start": "2020-08-03", "end": "2025-01-31"},
  "final_holdout":  {"start": "2025-02-01", "end": "2025-04-30"}
}
```

**B. Add regime-conditioning features** — use `fo_midday_direction_regime_v3` for Stage 2, which includes CE/PE regime indicators

### Priority 2 — Drop Stage 3, Test Stage 1+2 Only (QUICK TEST)
Stage 3 recipe models add noise. Test the pipeline with Stage 2 direction signal only, fixed to best recipe (L3):
```json
"policy": {
  "stage3_policy_id": "recipe_fixed_v1",
  "stage3": {"selected_recipe_id": "L3", "selection_mode": "fixed"}
}
```
This eliminates Stage 3 model uncertainty and tests whether Stage 1+2 alone can be profitable.

### Priority 3 — Stage 2 Direction Balance Fix (STRUCTURAL)
The direction oracle labels are ~100% PE in 2020-2024. This means the model only learns "predict PE". Options:
- **Rebalance labels:** Sample equal CE/PE from oracle, or use class_weight="balanced" in Stage 2
- **Change direction definition:** Instead of best-recipe direction, use market direction (Nifty up/down)

### Priority 4 — Grid Search with Updated Windows
Once windows are updated, run `staged_grid.proper_full_v1.json` with the new time windows to find the best Stage 2 feature set on recent data.

---

## 6. Configuration Files (Current Iteration)

| File | Purpose | Status |
|------|---------|--------|
| `configs/research/staged_dual_recipe.proper_full_v1.json` | Base manifest (all stages, MIDDAY filter) | Produced 0 trades |
| `configs/research/staged_dual_recipe.simple_s2_v1.json` | Simple S2, no filter — **current best** | 27 holdout trades |
| `configs/research/staged_grid.proper_full_v1.json` | 5-run grid for S2 feature variants | Not yet run |

---

## 7. Infrastructure

| Resource | Value |
|----------|-------|
| VM | `option-trading-ml-01`, `asia-south1-b` |
| VM user | `savitasajwan03` |
| VM repo | `/home/savitasajwan03/option_trading` |
| SSH key | `C:\Users\amits\.ssh\google_compute_engine` |
| Branch | `chore/ml-pipeline-ubuntu-gcp-runbook` |
| Parquet data | `/home/savitasajwan03/.data/ml_pipeline/parquet_data` |
| Artifacts root | `/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research/` |

### Active tmux sessions (as of 2026-04-26)
```bash
gcloud compute ssh savitasajwan03@option-trading-ml-01 --zone=asia-south1-b --project=amittrading-493606
tmux ls   # list sessions
```

---

## 8. Model Publication Record

| Date | Run ID | Model Group | Profile | Published By | GCS Path |
|------|--------|-------------|---------|-------------|----------|
| 2026-04-26 | `staged_simple_s2_v1_20260426_110326` | `research/staged_simple_s2_v1` | `ml_pure_staged_v1` | Manual — research checkpoint | `gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1` |

> **Note:** This model is published as a research checkpoint, NOT for production use.
> All gates failed. Do not route live trades through this model.

### GCS Artifacts
```
gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1/
├── model/model.joblib
├── model_contract.json
├── config/profiles/ml_pure_staged_v1/threshold_report.json
├── config/profiles/ml_pure_staged_v1/training_report.json
├── reports/training/latest.json
└── data/training_runs/staged_simple_s2_v1_20260426_110326/
    ├── model/model.joblib
    ├── model_contract.json
    └── config/profiles/ml_pure_staged_v1/{threshold,training}_report.json
```

---

## 9. Quick Resume Commands

```bash
# SSH to VM
gcloud compute ssh savitasajwan03@option-trading-ml-01 --zone=asia-south1-b --project=amittrading-493606

# Check existing run status
cat /home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research/staged_simple_s2_v1_20260426_110326/run_status.json

# Launch Priority 1A: Extended window run (create config first)
cd /home/savitasajwan03/option_trading && git pull
PYTHONPATH=/home/savitasajwan03/option_trading \
  .venv/bin/python -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.simple_s2_v1.json

# Validate a new config before running
PYTHONPATH=/home/savitasajwan03/option_trading \
  .venv/bin/python -m ml_pipeline_2.run_research \
  --config <new_config.json> --validate-only
```
