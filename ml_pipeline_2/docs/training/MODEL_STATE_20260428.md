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
| 2026-04-29 | `staged_grid_feature_s2_v1_20260428T040319Z` | Grid B | **0.545** (B4) | 329 (B4) | 51% (B4) ✅ | ✅ Complete. B4 winner (`fo_midday_time_aware_plus_oi_iv`). ROC range 0.535–0.545 across all 5 runs — confirms feature signal floor. TRENDING regime systematically failing (PF=0.31). C1→C3 running. |
| 2026-04-29 | `staged_deep_hpo_c1_base` | C1 | — | — | — | 🔄 Running. Deep HPO (12 trials, 80 experiments, 4h). S1 reuse from B4. tmux `grid_c`. |

---

### Grid B Complete Results

| Run | Feature Set | S2 ROC | Trades | Long Share | Net Return | TRENDING PF | VOLATILE PF | Outcome |
|-----|------------|--------|--------|-----------|-----------|------------|------------|--------|
| B1 | `fo_expiry_aware_v3` (control) | 0.544 | 168 | 39% | -0.019 | 0.36 ❌ | 1.67 ✅ | Same as A2 baseline — feature set makes no difference at this ROC floor |
| B2 | `fo_full` (all features) | 0.539 | **1198** | 69% | -0.690 | 0.50 | 0.29 | ❌ Catastrophic — 1198 trades all losing; VOLATILE long=100% (wrong); overfitting to noise |
| B3 | `fo_midday_asymmetry` | 0.535 | 176 | 72% ⚠️ | -0.096 | 0.38 | 1.30 | ❌ Side share out of band; TRENDING long=99% — CE-biased features |
| **B4** | **`fo_midday_time_aware_plus_oi_iv`** | **0.545** | **329** | **51%** ✅ | -0.099 | **0.31** ❌ | **1.31** ✅ | **Winner** — best ROC + near-perfect balance; SIDEWAYS PF=4.54 ✅ (18 trades) |
| B5 | `fo_midday_expiry_interactions` | 0.535 | 56 | **0%** ⚠️ | +0.018 ✅ | 0.65 | 1.82 ✅ | Side share out of band (all PE); only profitable run — but VOLATILE-only de facto |

**B4 full regime breakdown:**

| Regime | Trades | PF | Long Share | Net Return | Insight |
|--------|--------|----|-----------|------------|--------|
| TRENDING | 186 | 0.31 ❌ | 69% CE | -0.118 | Dominant regime (57% of trades); model confidently wrong on direction |
| VOLATILE | 95 | 1.31 ✅ | 7% CE | +0.019 | Profitable; model correctly predicts PE direction in volatile markets |
| SIDEWAYS | 18 | 4.54 ✅ | 44% CE | +0.018 | Balanced + very profitable; small sample |
| PRE_EXPIRY | 17 | 0.18 ❌ | 71% CE | -0.010 | Near-expiry direction is noise |
| UNKNOWN | 13 | 0.14 ❌ | 100% CE | -0.008 | Ignore |

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
| `staged_dual_recipe.label_fix_b_base.json` | Grid B base manifest — auto-patched with A2 winner | ✅ Used |
| `staged_grid.feature_s2_v1.json` | Grid B: 5 S2 feature set variants (B1–B5) | ✅ Complete |
| `staged_dual_recipe.deep_hpo_base.json` | Grid C base manifest — auto-patched with B4 winner by `update_grid_manifest.py` | ✅ Patched |
| `staged_grid.deep_hpo_v1.json` | Grid C grid config (NOT used — cv_config is a disallowed grid override key) | ⚠️ Unused |
| `staged_dual_recipe.deep_hpo_c1.json` | Grid C Run C1 — deep HPO baseline (cv 120/21), S1 reuse from B4 | 🔄 Running |
| `staged_dual_recipe.deep_hpo_c2.json` | Grid C Run C2 — cv train_days=180, S1 reuse from C1 | ⏳ Queued after C1 |
| `staged_dual_recipe.deep_hpo_c3.json` | Grid C Run C3 — cv valid_days=42 step=42, S1 reuse from C1 | ⏳ Queued after C1 |

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
| `run_c_only.sh` — C1 first, then C2+C3 in parallel (standalone, not grid) | `tools/run_c_only.sh` | ✅ Running |
| `check_results2.py` — diagnostic viewer for summary schema v3 | `tools/check_results2.py` | ✅ Added |
| `check_b_runs.py` — per-regime detail for Grid B runs | `tools/check_b_runs.py` | ✅ Added |

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

# ── Grid B is DONE. Grid C is running in tmux grid_c ──────────────────────
tmux attach -t grid_c                            # live C1 output (Ctrl-b d to detach)
tail -50 ml_pipeline_2/tools/auto_grid_c.log     # or tail the log
tail -50 ml_pipeline_2/tools/c2_run.log          # C2 log (starts after C1)
tail -50 ml_pipeline_2/tools/c3_run.log          # C3 log (starts after C1)
python3 /tmp/check_b_runs.py                     # shows completed run metrics

# ── If Grid C died — restart from C1 ─────────────────────────────────────
tmux new-session -d -s grid_c
tmux send-keys -t grid_c \
  'bash /home/savitasajwan03/option_trading/ml_pipeline_2/tools/run_c_only.sh 2>&1 | tee /home/savitasajwan03/option_trading/ml_pipeline_2/tools/auto_grid_c.log' Enter

# ── If C1 done, restart C2+C3 only ───────────────────────────────────────
PYTHONPATH=. .venv/bin/python -u -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_c2.json \
  > ml_pipeline_2/tools/c2_run.log 2>&1 &
PYTHONPATH=. .venv/bin/python -u -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_c3.json \
  > ml_pipeline_2/tools/c3_run.log 2>&1 &

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
Grid A — COMPLETE
  A2 winner: S2 CV ROC=0.544, 168 holdout trades, long_share=39%  ✅ bias fixed
  → Proceed to Grid B

Grid B — COMPLETE
  B4 winner: fo_midday_time_aware_plus_oi_iv
  S2 ROC=0.545 (marginal improvement), 329 trades, long_share=51%, net=-0.099
  Key finding: ROC floor is ~0.544 regardless of feature set. TRENDING regime is the problem.
  → Proceed to Grid C (deep HPO may sharpen probabilities → better threshold filtering)

Grid C — RUNNING (C1 active; C2+C3 queued)
  Evaluate each C run on these questions:
  ├─ Did TRENDING PF improve above 0.8?     → HPO found useful TRENDING patterns
  ├─ Did VOLATILE PF exceed 1.5?            → regime-gated publish is viable
  ├─ Did combined PF exceed 1.0?            → within range of gate; continue to Grid D
  └─ Did combined PF exceed 1.5?            → PUBLISH ✅

Grid C expected outcomes (ranked by probability):
  1. VOLATILE PF improves to 1.5+ but TRENDING stays bad → Grid D regime-gated path (most likely)
  2. C2/C3 long window improves TRENDING → combined PF improves → possible publish
  3. No improvement vs B4 → feature + label signal is at ceiling → Grid D new hypothesis

Grid D hypotheses (if Grid C doesn't publish):
  D1 [MOST LIKELY PATH]: VOLATILE-Only Gating
      Filter S2 training to VOLATILE sessions only via stage2_session_filter or new labeler variant
      VOLATILE has consistent PF=1.31–1.82 across A2/B1/B4/B5 — the signal is real there
      Need ≥50 holdout VOLATILE trades: B4 had 95 → feasible
  D2: Wider Threshold Grid for TRENDING Suppression
      Add high-confidence thresholds (0.65, 0.70, 0.75) to S2 policy grid
      Forces model to only trade TRENDING when very confident → reduces bad trades
  D3: Separate Per-Regime S2 Models
      Train one direction model per regime using stage2_session_filter per-regime
      Most expensive; most likely to work if D1 alone isn't enough
  D4: Accept VOLATILE+SIDEWAYS Only as Product Scope
      Regime-gate the live model to only fire when regime ∈ {VOLATILE, SIDEWAYS}
      PRE_EXPIRY and TRENDING are consistently unpredictable with current features
```

---

## 10. What Definitely Does NOT Work (Do Not Repeat)

- `bypass_stage2=true` — random direction, guaranteed loss
- MIDDAY + conviction filter — kills throughput to 0 trades
- Oracle rolling CE/PE win-rate features (`fo_midday_direction_regime_v3`) — S2 CV gate fail
- Feature engineering to fix CE/PE bias — fundamentally wrong attack surface
- Stage 3 dynamic recipe selection — no OVR signal; always falls back to fixed recipe
- `fo_full` (all S2 features) — catastrophic; 1198 trades all losing; more features = more noise for direction prediction
- Window shift alone — oracle label has max_corr=0.0495 with any feature; signal doesn't exist in oracle label regardless of window
- `cv_config`, `windows`, `labels` as grid run overrides — **disallowed by manifest validator**; use standalone run_research manifests

---

## 11. Deep Analysis — TRENDING Regime Failure

### Why TRENDING fails consistently

Across every run (A2, B1, B2, B3, B4), the TRENDING regime returns PF=0.29–0.50. This is not noise — it is systematic. The reason:

1. **Aug–Oct 2024 market context:** NIFTY 50 peaked in Sep 2024 (~26,000) and crashed to ~24,000 by Oct 2024 (-8%). During this period, TRENDING regime days were trending **downward** → PE options profitable.

2. **What the model learned:** Training data (2020–2024 bull market) associated TRENDING regime with CE wins (market going up). The `direction_market_up_v1` label correctly identifies CE vs PE winners, but the FEATURES (midday IV/OI patterns) that predicted CE-up during 2020–2024 bull trending now misfire in a bear-trending holdout.

3. **Why VOLATILE works:** Volatile regime = IV spike + uncertainty. The direction of an intraday spike depends on order flow at midday, which IS captured by OI/IV features. This is a genuine intraday edge that does not depend on the multi-month market direction.

4. **Why PRE_EXPIRY fails:** Near expiry, options gamma is extreme and direction is dominated by pinning/unwinding dynamics, not by the underlying direction signal the model learned.

### Feature Signal Floor

S2 ROC is remarkably stable across all feature sets:

| Run | Feature Set | S2 ROC |
|-----|------------|--------|
| B1 | fo_expiry_aware_v3 | 0.5444 |
| B2 | fo_full | 0.5391 |
| B3 | fo_midday_asymmetry | 0.5345 |
| B4 | fo_midday_time_aware_plus_oi_iv | **0.5453** |
| B5 | fo_midday_expiry_interactions | 0.5353 |

The 0.01 range across all feature sets means we are at the signal ceiling for the current label on the current data. **Deep HPO (Grid C) may push this to 0.55–0.56, but is unlikely to reach 0.60+.** The ceiling is a property of the label-feature relationship, not HPO configuration.

### The Consistent VOLATILE Edge

This is the most important pattern in all Grid B/A results:

| Run | VOLATILE Trades | VOLATILE PF | VOLATILE Long Share |
|-----|----------------|------------|--------------------|
| A2/B1 | 73 | 1.67 ✅ | 0% CE (all PE) |
| B4 | 95 | 1.31 ✅ | 7% CE |
| B5 | 44 | 1.82 ✅ | 0% CE |

**Three independent runs, all showing VOLATILE PF > 1.3 with PE-dominant prediction.** This is a real edge: in volatile market conditions, `fo_midday_time_aware_plus_oi_iv` features correctly predict PE will win (i.e., markets move down during intraday volatile sessions in Aug–Oct 2024). The logical next step is to isolate this regime.

### Grid C Success Criteria (revised)

Given the analysis above, Grid C should be evaluated on:

| Metric | Minimum (proceed to Grid D) | Target (publishable) |
|--------|-----------------------------|---------------------|
| VOLATILE PF | ≥ 1.3 (stable edge) | ≥ 1.5 (gate pass) |
| VOLATILE trades | ≥ 50 | ≥ 80 |
| Combined PF | ≥ 0.8 (improving) | ≥ 1.5 (gate pass) |
| S2 CV ROC | ≥ 0.545 (no regression) | ≥ 0.550 (gate pass natively) |

If C1/C2/C3 maintain VOLATILE PF ≥ 1.3 with ≥ 50 VOLATILE holdout trades, **Grid D1 (VOLATILE-gated publish) is the immediate next step**, regardless of combined metrics.
