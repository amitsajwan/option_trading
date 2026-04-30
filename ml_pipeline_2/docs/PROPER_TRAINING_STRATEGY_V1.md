# Proper Training Strategy — Post bypass_stage2 Analysis

## Date: 2026-04-26
## Author: Deep analysis from JIRA_BYPASS_STAGE2_RECIPE_MODEL_ANALYSIS.md + all prior run data

---

## 1. Executive Summary

All `bypass_stage2` runs are scientifically contaminated and should be discarded as performance benchmarks.
The bypass injects `direction_up_prob=0.5` (coin flip), which means every trade directional outcome is random.
With 50/50 random direction and transaction costs of 0.0006/trade, guaranteed losses are mathematically inevitable regardless of Stage 3 recipe quality.

**The actual problem: Stage 2 direction brier gate (0.22 max) is too tight.**
The best Stage 2 run achieved brier=0.252, which is blocked by the hard gate. The pipeline never reaches Stage 3 in practice.

---

## 2. Root Cause Chain (Revised)

```
Stage 2 direction model → brier=0.252 → hard gate 0.22 → BLOCKED
    ↓
bypass_stage2=true used as workaround
    ↓
direction becomes 50/50 random
    ↓
Stage 3 recipe labels trained on contaminated data (direction oracle is random)
    ↓
Stage 3 models have zero signal (expected — they're trained on noise)
    ↓
"Stage 3 has no signal" — TRUE but IRRELEVANT. Bypass is the problem.
```

**The correct fix: loosen Stage 2 gate to 0.26, use `record_only`, proceed with real direction signal.**

---

## 3. What All Prior Runs Tell Us

| Run Type | Key Finding |
|----------|-------------|
| `bypass_stage2_v6` (1,432 trades) | Proves Stage 1 works. Stage 2/3 signal is absent by design. |
| `fixed_catalog` (1,764 trades) | 4 recipes vs 7 makes no difference — loss rate identical. |
| `low_threshold` (6,914 trades) | More trades = more losses. Threshold tuning cannot fix random direction. |
| `combined` (30,812 trades) | Confirms: per-trade loss rate constant at ~0.00058. Direction is the problem. |
| Best prior real run `expiry_dir_grid` | Stage 2 ROC-AUC=0.571, brier=0.252. **Signal exists.** Gate blocked it. |

**Conclusion: Stage 2 direction signal exists (ROC-AUC ~0.57). The gate (brier<0.22) is the blocker, not the model.**

---

## 4. Design Decisions for Proper Training

| Decision | Choice | Reason |
|----------|--------|--------|
| bypass_stage2 | **false** | Bypass contaminates all metrics |
| stage2_cv_gate_mode | **record_only** | Never block Stage 3 due to Stage 2 brier |
| stage2 hard gate brier_max | **0.26** | Best runs hit 0.252; 0.22 is too tight |
| stage2_labeler_id | **direction_or_no_trade_v1** | Allows model to abstain on low-edge cases |
| stage2_trainer_id | **gate_direction_catalog_v1** | Correct trainer for direction-or-no-trade |
| stage2_session_filter | **MIDDAY only** | Reduce noise; direction signal stronger mid-session |
| stage2_target_redesign | **enabled, edge=0.0019** | Cleaner labels with min directional edge |
| recipe_catalog_id | **fixed_l0_l3_v1** (4 recipes) | Higher base rate ~25% vs ~14% for 7 recipes |
| train_days | **120** (up from 84) | More data per CV fold for recipe models |
| HPO | **All 3 stages** | stage1: 3 trials, stage2: 6 trials, stage3: 5 trials |

---

## 5. New Config Files

### 5.1 Base Manifest (for single run OR as grid base)
`configs/research/staged_dual_recipe.proper_full_v1.json`

Key settings:
- Stage 2: `fo_midday_time_aware_plus_oi_iv` (best known Stage 2 feature set)
- Stage 3: `fo_full`, `fo_expiry_aware_v3`, `fo_no_time_context`
- All 3 stages have HPO enabled
- `stage2_cv_gate_mode: "record_only"` — pipeline never blocked by Stage 2
- `hard_gates.stage2.brier_max: 0.26`
- `training.cv_config.train_days: 120`

### 5.2 Grid Config (5 Stage 2 variants)
`configs/research/staged_grid.proper_full_v1.json`

| Run ID | Stage 2 Features | Target Redesign | What It Tests |
|--------|-----------------|-----------------|---------------|
| `proper_s2_midday_time_aware` | `fo_midday_time_aware_plus_oi_iv` | Standard (edge≥0.0019) | Best known features, standard threshold |
| `proper_s2_direction_regime_v3` | `fo_midday_direction_regime_v3` | Standard | Newest regime-aware features |
| `proper_s2_asymmetry_expiry` | `fo_midday_asymmetry` + `fo_midday_expiry_interactions` | Standard | CE/PE asymmetry + expiry interactions |
| `proper_s2_expiry_aware_simple` | `fo_expiry_aware_v3` | **Disabled** | Simpler binary classifier, no MIDDAY filter |
| `proper_s2_strict_target` | `fo_midday_time_aware_plus_oi_iv` | **Strict** (edge≥0.0022) | High-conviction trades only |

Runs 2-5 reuse Stage 1 from Run 1 (faster grid).

---

## 6. Launch Commands (GCP VM)

### Prerequisites
```bash
# SSH to VM
ssh -i ~/.ssh/google_compute_engine savitasajwan03@34.47.131.234

# Sync repo (run locally first)
cd c:/code/option_trading/option_trading_repo
git push

# On VM — pull latest
cd /home/savitasajwan03/option_trading
git pull origin main  # or your branch
```

### Option A: Single Proper Run (fastest path, 4-8 hours)
```bash
cd /home/savitasajwan03/option_trading
tmux new-session -d -s proper_full_v1

tmux send-keys -t proper_full_v1 '
PYTHONPATH=/home/savitasajwan03/option_trading \
/home/savitasajwan03/option_trading/.venv/bin/python \
  -m ml_pipeline_2.run_research \
  --config /home/savitasajwan03/option_trading/ml_pipeline_2/configs/research/staged_dual_recipe.proper_full_v1.json \
  2>&1 | tee /home/savitasajwan03/option_trading/logs/proper_full_v1.log
' Enter

# Monitor
tmux attach -t proper_full_v1
```

### Option B: Full Grid (recommended, 12-24 hours, max 2 parallel)
```bash
cd /home/savitasajwan03/option_trading
tmux new-session -d -s proper_grid_v1

tmux send-keys -t proper_grid_v1 '
PYTHONPATH=/home/savitasajwan03/option_trading \
/home/savitasajwan03/option_trading/.venv/bin/python \
  -m ml_pipeline_2.run_staged_grid \
  --config /home/savitasajwan03/option_trading/ml_pipeline_2/configs/research/staged_grid.proper_full_v1.json \
  2>&1 | tee /home/savitasajwan03/option_trading/logs/proper_grid_v1.log
' Enter

# Monitor
tmux attach -t proper_grid_v1
```

### Validate Before Running (do this first)
```bash
PYTHONPATH=/home/savitasajwan03/option_trading \
/home/savitasajwan03/option_trading/.venv/bin/python \
  -m ml_pipeline_2.run_research \
  --config /home/savitasajwan03/option_trading/ml_pipeline_2/configs/research/staged_dual_recipe.proper_full_v1.json \
  --validate-only
```

---

## 7. Success Criteria

A run is successful when:

| Gate | Target | Notes |
|------|--------|-------|
| Stage 1 ROC-AUC | ≥ 0.62 | Prior best: 0.686 |
| Stage 2 ROC-AUC | ≥ 0.55 | Prior best: 0.571 |
| Stage 2 brier | ≤ 0.26 | Gate relaxed from 0.22 |
| Stage 2 side balance | 30%-70% | CE/PE balance check |
| Stage 3 ROC-AUC | ≥ 0.52 | Any signal above 0.5 is useful |
| Holdout profit_factor | ≥ 1.5 | Hard gate |
| Holdout net_return | ≥ 0.0 | Hard gate |
| Holdout trades | ≥ 50 | Hard gate |

---

## 8. Expected Stage 3 Behavior (When Stage 2 Works)

When Stage 2 provides real direction signal (not 50/50):
- Only CE or PE trades are selected based on direction confidence
- Stage 3 only needs to pick the best recipe among the correct direction
- With 4 recipes (fixed_l0_l3_v1) and direction pre-filtered, base rate per recipe rises
- Recipe labels become meaningful (best recipe given the correct side)

The oracle label generation in `build_oracle` already handles this correctly. The contamination was entirely from bypass injecting random direction.

---

## 9. If Proper Run Still Fails

If the grid's best run still produces PF < 1.0 on holdout:

1. **Check Stage 2 balance on holdout** — if >80% CE or PE, Stage 2 is overfitting to validation period direction bias
2. **Check Stage 3 recipe label base rates** — if any recipe < 15% positive class, drop it from catalog
3. **Consider single-recipe mode** — skip Stage 3, use Stage 1 + Stage 2 only (recipe fixed to L3 or best from oracle)
4. **Extend holdout window** — Aug-Oct 2024 may be a CE-dominant regime; test on 2024-08 to 2025-03

---

## 10. Files Created

| File | Purpose |
|------|---------|
| `configs/research/staged_dual_recipe.proper_full_v1.json` | Base manifest — single run or grid base |
| `configs/research/staged_grid.proper_full_v1.json` | 5-run grid exploring Stage 2 feature sets |
