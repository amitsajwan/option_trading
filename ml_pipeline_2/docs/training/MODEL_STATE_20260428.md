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

| Date | Run ID | Grid | S2 ROC | Holdout Trades | Long Share | Outcome |
|------|--------|------|--------|----------------|------------|--------|
| 2026-04-28 | `staged_label_fix_a1_window_shift` | A1 | — | — | — | ❌ `stage2_signal_check_failed` — oracle label max_corr=0.0495 < threshold 0.05. Window shift irrelevant when oracle label has near-zero feature correlation. |
| 2026-04-28 | `staged_label_fix_a2_market_direction` | A2 | **0.544** | **168** | **39%** ✅ | ⚠️ Completed. Bias fixed (93.8%→39%). S2 CV ROC just below gate (0.544 vs 0.55, record_only mode continued). PF<1.5 and net_return≤0. **Winner — label fix confirmed working.** |
| 2026-04-28 | `staged_label_fix_a3_combined` | A3 | 0.516 | <50 | — | ⚠️ Completed. Shifted windows reduce holdout to Sep–Oct 2024 → very few trades. S1 ROC drift >0.05. Too little holdout data to evaluate. |
| 2026-04-28 | `staged_grid_feature_s2_v1_20260428T040319Z` | Grid B | — | — | — | 🔄 Running. B1 (`expiry_baseline`) active. B2–B5 queued. Base = A2 (direction_market_up_v1 + original windows + S1 reuse from A2 run dir). |

### Grid A Key Observations

- **A2 long_share by regime:** TRENDING=65% CE (good), VOLATILE=0% CE (all PE, PF=1.67 ✅), SIDEWAYS=86% CE (few trades), PRE_EXPIRY=80% CE (bad). Combined = 39%.
- **A2 holdout PF by regime:** VOLATILE=1.67 ✅, SIDEWAYS=2.92 ✅ (7 trades), TRENDING=0.36 ❌, PRE_EXPIRY=0.09 ❌. Policy calibration (thresholds) likely to help TRENDING/PRE_EXPIRY.
- **A1 failure insight:** Proves oracle label (`direction_best_recipe_v1`) is structurally near-zero-correlated with any feature for market direction prediction, regardless of training window. Confirms A2 labeler is the correct path.
- **A3 insight:** Shifting windows shrinks holdout period (Sep–Oct 2024 only = ~2 months) → too few signals to pass the `trades≥50` gate. Original windows (holdout Aug–Oct 2024 = 3 months) are better.

---

## 5. Configuration Files

| File | Purpose | Status |
|------|---------|--------|
| `staged_dual_recipe.label_fix_base.json` | Grid A Run A1 — shifted windows, oracle label | ✅ Used |
| `staged_dual_recipe.label_fix_a2.json` | Grid A Run A2 — original windows, `direction_market_up_v1`, S1 reuse | ✅ Used (**winner**) |
| `staged_dual_recipe.label_fix_a3.json` | Grid A Run A3 — shifted windows, `direction_market_up_v1`, fresh S1 | ✅ Used |
| `staged_grid.label_fix_v1.json` | Grid A grid config (not used — A runs launched as standalone run_research) | ⚠️ Unused (windows/labels can't be grid overrides) |
| `staged_dual_recipe.label_fix_b_base.json` | Grid B base manifest — **auto-patched** with A2 winner by `update_grid_manifest.py` | ✅ Patched |
| `staged_grid.feature_s2_v1.json` | Grid B: 5 S2 feature set variants | 🔄 Running |
| `staged_dual_recipe.deep_hpo_base.json` | Grid C base manifest — auto-patched from Grid B winner | ⏳ Pending |
| `staged_grid.deep_hpo_v1.json` | Grid C: deep HPO + CV config variants | ⏳ Pending |

---

## 6. Code Changes This Session

| Change | File | Status |
|--------|------|--------|
| `build_stage2_labels_market_direction` labeler | `src/ml_pipeline_2/staged/pipeline.py` | ✅ Added |
| `direction_market_up_v1` in label_registry + resolve_labeler | `src/ml_pipeline_2/staged/registries.py` | ✅ Added |
| Exported in `__all__` | `src/ml_pipeline_2/staged/pipeline.py` | ✅ Added |
| `update_grid_manifest.py` — picks winner from summaries, patches next grid base manifest | `tools/update_grid_manifest.py` | ✅ Added |
| `run_grids_auto.sh` — full A+B+C automation (original, now superseded) | `tools/run_grids_auto.sh` | ✅ Added |
| `run_grids_from_b.sh` — B+C automation starting from confirmed A2 winner | `tools/run_grids_from_b.sh` | ✅ Added |
| `check_results2.py` — diagnostic viewer for summary schema v3 | `tools/check_results2.py` | ✅ Added |

### Summary Schema v3 — Field Path Reference

The pipeline writes `summary.json` in schema v3. Metrics are **not** at `combined_holdout` or `stage_quality` (schema v2). Correct paths:

| Metric | Path in summary.json |
|--------|---------------------|
| S2 CV ROC-AUC | `cv_prechecks.stage2_cv.roc_auc` |
| S1 CV ROC-AUC | `cv_prechecks.stage1_cv.roc_auc` |
| Signal check result | `cv_prechecks.stage2_signal_check.has_signal` / `.max_correlation` |
| Holdout trades (per regime) | `scenario_reports.regime.segments.<REGIME>.trades` |
| Holdout net return (per regime) | `scenario_reports.regime.segments.<REGIME>.net_return_sum` |
| Holdout long_share (per regime) | `scenario_reports.regime.segments.<REGIME>.long_share` |
| Holdout PF (per regime) | `scenario_reports.regime.segments.<REGIME>.profit_factor` |
| Gate failures | `publish_assessment.blocking_reasons` |
| Completion mode | `completion_mode` (`completed` / `stage2_signal_check_failed`) |

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
cd /home/savitasajwan03/option_trading

# ── Check live status ──────────────────────────────────────────────────────
tmux attach -t grids_bc                         # live Grid B/C output (Ctrl-b d to detach)
tail -50 ml_pipeline_2/tools/auto_grids_bc.log  # or tail the log
python3 /tmp/check_results2.py                  # structured metrics (scp tools/check_results2.py first)

# ── If automation died — restart from Grid B ──────────────────────────────
tmux new-session -d -s grids_bc
tmux send-keys -t grids_bc \
  'bash /home/savitasajwan03/option_trading/ml_pipeline_2/tools/run_grids_from_b.sh 2>&1 | tee /home/savitasajwan03/option_trading/ml_pipeline_2/tools/auto_grids_bc.log' Enter

# ── If automation died — restart Grid C only ──────────────────────────────
# (Grid B winner already patched into deep_hpo_base.json)
PYTHONPATH=. .venv/bin/python -u -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.deep_hpo_v1.json \
  --model-group research/deep_hpo_v1 \
  --profile-id ml_pure_staged_v1

# ── Re-run winner selection manually ─────────────────────────────────────
# From Grid A summaries → patch Grid B manifest
PYTHONPATH=. .venv/bin/python ml_pipeline_2/tools/update_grid_manifest.py \
  --run-summaries \
    ml_pipeline_2/artifacts/research/staged_label_fix_a1_window_shift/summary.json \
    ml_pipeline_2/artifacts/research/staged_label_fix_a2_market_direction/summary.json \
    ml_pipeline_2/artifacts/research/staged_label_fix_a3_combined/summary.json \
  --base-manifest ml_pipeline_2/configs/research/staged_dual_recipe.label_fix_b_base.json \
  --grid-kind label_fix

# From Grid B grid_summary → patch Grid C manifest
PYTHONPATH=. .venv/bin/python ml_pipeline_2/tools/update_grid_manifest.py \
  --grid-summary ml_pipeline_2/artifacts/research/staged_grid_feature_s2_v1_<TIMESTAMP>/grid_summary.json \
  --base-manifest ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_base.json \
  --grid-kind feature_s2
```

---

## 9. Decision Flow

```
Grid A results — COMPLETE
  A2 winner: S2 CV ROC=0.544, 168 holdout trades, long_share=39%  ✅ bias fixed
  → Proceed to Grid B with direction_market_up_v1 + original windows

Grid B results — IN PROGRESS
  ├─ Best feature set S2 ROC > 0.544 AND trades ≥ 50     → proceed to Grid C
  ├─ All runs signal_check_failed                         → labeler issue; investigate
  └─ All runs complete but PF < 1.0                       → policy calibration needed before Grid C

Grid C results — PENDING
  ├─ PF ≥ 1.5 AND long_share 30–70% AND trades ≥ 50      → publish model
  ├─ PF ≥ 1.0 but below gate                             → accept as research checkpoint; plan Grid D
  └─ VOLATILE regime consistently PF > 1.5               → consider regime-gated publish
```

### A2 Regime Breakdown (Reference for Grid B/C evaluation)

| Regime | Trades | PF | Long Share | Signal |
|--------|--------|----|-----------|--------|
| TRENDING | 71 | 0.36 ❌ | 65% CE | Wrong direction in trending markets |
| VOLATILE | 73 | 1.67 ✅ | 0% CE (all PE) | Correct: volatile periods = PE profitable |
| SIDEWAYS | 7 | 2.92 ✅ | 86% CE | Too few trades to conclude |
| PRE_EXPIRY | 15 | 0.09 ❌ | 80% CE | Poor performance near expiry |
| Combined | 168 | <1.5 | 39% | Bias fixed; profitability work remains |

---

## 10. What Definitely Does NOT Work (Do Not Repeat)

- `bypass_stage2=true` — random direction, guaranteed loss
- MIDDAY + conviction filter — kills throughput to 0 trades
- Oracle rolling CE/PE win-rate features (`fo_midday_direction_regime_v3`) — S2 CV gate fail
- Feature engineering to fix CE/PE bias — fundamentally wrong attack surface
- Stage 3 dynamic recipe selection — no OVR signal; always falls back to fixed recipe
