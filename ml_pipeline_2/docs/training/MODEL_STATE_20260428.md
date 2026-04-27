# ML Model State — 2026-04-28

> **Canonical snapshot for this research iteration.**
> Update this doc after every meaningful run in this session.
> When resuming, start here.

---

## 1. Research Context (Why This Session Exists)

All prior feature engineering attempts to fix CE/PE bias have been exhausted:
- Oracle rolling win-rate features → S2 CV gate fail (ROC 0.535), 0 trades
- MIDDAY+OI/IV features → 113 trades, 93.8% CE bias — bias unchanged
- Window/feature combinations → bias is in the **label definition**, not the features

**Root cause (confirmed):** `direction_label` in the oracle is CE/PE based on which recipe had the best after-cost return. 2020–2024 training data had more PE-profitable snapshots → ~90% PE label share. Holdout (Aug–Oct 2024) is CE-dominant. Feature engineering cannot overcome a label imbalance of this magnitude.

**This session attacks the root cause at two independent levels:**
- **Label level:** Replace oracle direction with returns-based market direction (CE vs PE return comparison)
- **Data level:** Shift training window forward to include CE-dominant Jul–Aug 2024 data in training

---

## 2. Current Best Baseline (Inherited)

| Field | Value |
|-------|-------|
| **Run ID** | `regime_fix_s2_midday_noconv` (best of 2026-04-27 grid) |
| **Config** | `staged_dual_recipe.regime_fix_v1.json` + `fo_midday_time_aware_plus_oi_iv` S2 features |
| **S2 ROC** | 0.570 |
| **Holdout Trades** | 113 |
| **Holdout PF** | 0.352 |
| **Long share** | 93.8% CE — ❌ severe bias |
| **Status** | Failed all gates. Best throughput run to date. |

**Stage 1** (stable, reusable for same-window runs):
- Run: `staged_proper_full_v1_20260426_051531`
- ROC-AUC: **0.683**, Brier: 0.216 — ✅ PASS, stable across all runs
- VM path: `/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research/staged_proper_full_v1_20260426_051531`

---

## 3. This Session's Grid Plan

### Grid A — Label Fix Grid (`staged_grid.label_fix_v1.json`)

Three runs, each isolating one independent hypothesis. Full pipeline per run (~4–5 hours each).

| Run | Hypothesis | Windows | S2 Label | S1 | Expected Effect |
|-----|-----------|---------|----------|-----|-----------------|
| **A1** `label_fix_a1_window_shift` | Window shift: include CE-dominant Jul–Aug 2024 in training | Shifted: train→Jun 2024, holdout→Sep–Oct 2024 | `direction_best_recipe_v1` (oracle) | Fresh retrain | `long_share` drops from 93% toward 60–75% |
| **A2** `label_fix_a2_market_direction` | Market direction label: CE wins if `best_ce_return > best_pe_return` | Original (train→Apr 2024, holdout→Aug–Oct 2024) | `direction_market_up_v1` (**new**) | Reuse `staged_proper_full_v1_20260426_051531` | Structurally ~50/50 CE/PE on train AND holdout |
| **A3** `label_fix_a3_combined` | Both fixes together | Shifted (same as A1) | `direction_market_up_v1` (**new**) | Fresh retrain | Compound effect; only run after A1+A2 understood |

**New labeler `direction_market_up_v1` logic:**
```
ce_ret = best_ce_net_return_after_cost   (best CE return across all 4 recipes, after cost)
pe_ret = best_pe_net_return_after_cost   (best PE return across all 4 recipes, after cost)

label = CE  if  ce_ret > pe_ret           (market went up, CE won)
label = PE  if  pe_ret > ce_ret           (market went down, PE won)
skip        if  |ce_ret - pe_ret| < min_edge  (near-tie, ambiguous direction — skip row)
```
Min edge default: `0.002` (0.2% after cost). Configurable via `training.stage2_decisive_move_filter.min_ce_pe_edge`.

**Key properties of new label:**
- Does NOT require oracle positive (`entry_label == 1` still required, oracle valid not required)
- Structurally balanced: ~50/50 CE/PE regardless of market regime
- Min-edge filter = data quality filter (excludes near-ties where direction is ambiguous)
- Overrides oracle `direction_label` so CV gate, training target, and holdout evaluation all use market direction

**Run decision gates (check after each A run):**

| Check | Target | Action if Failed |
|-------|--------|-----------------|
| S2 CV ROC-AUC | ≥ 0.55 | Label has no signal — investigate before proceeding |
| Holdout `long_share` | 30–70% | Bias not fixed — do not declare success |
| Holdout trades | ≥ 50 | Throughput issue — check S2 threshold grid |
| Holdout PF (progress bar) | ≥ 1.0 | Model unprofitable — check if validation window is also CE-dominant |
| Holdout PF (gate) | ≥ 1.5 | Not deployable — proceed to Grid B |

---

### Grid B — Feature Set Grid (`staged_grid.feature_s2_v1.json`)

Run on the best-A label configuration. Tests 5 S2 feature sets; S1 reused from Grid A winner.

| Run | S2 Feature Set | Rationale |
|-----|---------------|-----------|
| B1 | `fo_expiry_aware_v3` | Current proven baseline (control) |
| B2 | `fo_full` | All features; tree models self-select via HPO |
| B3 | `fo_midday_asymmetry` | CE-PE OI diff + dealer proxy — most direct market direction proxy |
| B4 | `fo_midday_time_aware_plus_oi_iv` | Best throughput in prior grid (113 trades) |
| B5 | `fo_midday_expiry_interactions` | IV skew + OI + expiry; captures vol-regime direction |

Grid B config: `staged_grid.feature_s2_v1.json`. Base manifest: `staged_dual_recipe.label_fix_b_base.json`.
Update `stage1_reuse.source_run_id/dir` in base manifest with actual winning A-run artifact path before launching.

---

### Grid C — Deep HPO Grid (`staged_grid.deep_hpo_v1.json`)

Run on the best-B feature set. Deepens HPO search and tests CV config variants.

| Run | Key Change | Config |
|-----|-----------|--------|
| C1 | S2 HPO: 12 trials/model, 80 max experiments, 4h budget | Base + HPO override |
| C2 | CV: `train_days=180` (6-month train per fold) | Base + cv_config override |
| C3 | CV: `valid_days=42` (2-month valid per fold, matches holdout) | Base + cv_config override |

Grid C config: `staged_grid.deep_hpo_v1.json`. Base: best-B manifest (fill after Grid B).

---

## 4. Run History (This Session)

| Date | Run ID | Grid | S2 ROC | Holdout Trades | PF | Long Share | Outcome |
|------|--------|------|--------|---------------|----|------------|---------|
| — | — | Grid A (in queue) | — | — | — | — | Not started |

---

## 5. Configuration Files

| File | Purpose | Status |
|------|---------|--------|
| `staged_dual_recipe.label_fix_base.json` | Grid A base manifest (A1 windows, oracle label) | ✅ Created |
| `staged_grid.label_fix_v1.json` | Grid A: 3 runs (A1 window shift, A2 market direction, A3 combined) | ✅ Created |
| `staged_dual_recipe.label_fix_b_base.json` | Grid B base manifest — update S1 reuse path after Grid A | ✅ Created (needs S1 path update) |
| `staged_grid.feature_s2_v1.json` | Grid B: 5 S2 feature set variants | ✅ Created |
| `staged_dual_recipe.deep_hpo_base.json` | Grid C base manifest — update after Grid B | ✅ Created (needs update) |
| `staged_grid.deep_hpo_v1.json` | Grid C: deep HPO + CV config variants | ✅ Created |

---

## 6. Code Changes This Session

| Change | File | Status |
|--------|------|--------|
| `build_stage2_labels_market_direction` labeler function | `src/ml_pipeline_2/staged/pipeline.py` | ✅ Added |
| `direction_market_up_v1` registered in label_registry + resolve_labeler | `src/ml_pipeline_2/staged/registries.py` | ✅ Added |
| Exported in `__all__` | `src/ml_pipeline_2/staged/pipeline.py` | ✅ Added |

---

## 7. Infrastructure

| Resource | Value |
|----------|-------|
| VM | `option-trading-ml-01`, `asia-south1-b` |
| VM user | `savitasajwan03` |
| VM repo | `/home/savitasajwan03/option_trading` |
| SSH | `gcloud compute ssh savitasajwan03@option-trading-ml-01 --zone=asia-south1-b --project=amittrading-493606` |
| Branch | `chore/ml-pipeline-ubuntu-gcp-runbook` |
| Parquet data | `/home/savitasajwan03/.data/ml_pipeline/parquet_data` |
| Artifacts root | `/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research/` |

---

## 8. Quick Resume Commands

```bash
# SSH
gcloud compute ssh savitasajwan03@option-trading-ml-01 --zone=asia-south1-b --project=amittrading-493606

# Pull latest (includes new labeler + configs)
cd /home/savitasajwan03/option_trading && git pull

# Step 0 — EDA: skew diagnostic on best prior run (1 hour, optional but recommended)
PYTHONPATH=/home/savitasajwan03/option_trading \
  .venv/bin/python -u -m ml_pipeline_2.run_stage12_skew_diagnostic \
  --artifacts-dir ml_pipeline_2/artifacts/research/staged_grid_regime_fix_v1_20260427T080148Z/regime_fix_s2_midday_noconv

# Step 1 — Launch Grid A (tmux, background)
tmux new-session -d -s grid_a
tmux send-keys -t grid_a "
cd /home/savitasajwan03/option_trading && git pull && \
PYTHONPATH=/home/savitasajwan03/option_trading \
  .venv/bin/python -u -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.label_fix_v1.json \
  --model-group research/label_fix_v1 \
  --profile-id ml_pure_staged_v1
" Enter

# Check Grid A progress
tmux attach -t grid_a
# OR
cat /home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research/staged_grid_label_fix_v1_*/*/run_status.json

# Step 2 — After Grid A: update staged_dual_recipe.label_fix_b_base.json with winning S1 path
# Then launch Grid B
tmux new-session -d -s grid_b
tmux send-keys -t grid_b "
cd /home/savitasajwan03/option_trading && git pull && \
PYTHONPATH=/home/savitasajwan03/option_trading \
  .venv/bin/python -u -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.feature_s2_v1.json \
  --model-group research/feature_s2_v1 \
  --profile-id ml_pure_staged_v1
" Enter

# Step 3 — After Grid B: update staged_dual_recipe.deep_hpo_base.json with winning config
# Then launch Grid C
PYTHONPATH=/home/savitasajwan03/option_trading \
  .venv/bin/python -u -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.deep_hpo_v1.json \
  --model-group research/deep_hpo_v1 \
  --profile-id ml_pure_staged_v1
```

---

## 9. Decision Flow

```
Grid A results
  ├─ A2 or A3 CV ROC ≥ 0.55 AND long_share 30–70%  → proceed to Grid B with winning label
  ├─ A1 only improved bias (long_share 50–75%)      → Grid B with A1 windows + oracle label
  └─ All A runs still biased (long_share > 80%)     → label fix didn't work; new hypothesis needed

Grid B results
  ├─ Best feature set PF ≥ 1.0 on holdout           → proceed to Grid C (deepen HPO)
  └─ All features PF < 1.0                          → model lacks signal; re-examine label

Grid C results
  ├─ PF ≥ 1.5 AND long_share 30–70% AND trades ≥ 50 → publish model
  └─ PF ≥ 1.0 but not at gate                      → accept as research checkpoint; plan Grid D
```

---

## 10. What Definitely Does NOT Work (Do Not Repeat)

- `bypass_stage2=true` — random direction, guaranteed loss
- MIDDAY + conviction filter — kills throughput to 0 trades
- Oracle rolling CE/PE win-rate features (`fo_midday_direction_regime_v3`) — S2 CV gate fail
- Feature engineering to fix CE/PE bias — fundamentally wrong attack surface
- Stage 3 dynamic recipe selection — no OVR signal; always falls back to fixed recipe
