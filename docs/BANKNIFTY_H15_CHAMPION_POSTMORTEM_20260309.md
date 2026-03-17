# BankNifty H15 Champion Search Postmortem (Plain-English)

Last updated: 2026-03-09  
Audience: New reader with no prior context

## 1) One-page Summary

We attempted a full model search to find a new champion for:

- `model_group = banknifty_futures/h15_tp_auto`
- futures-labeled setup (`horizon=15m`, `TP=0.25%`, `SL=0.08%`)

The full search run (`run_id=20260308_190837`) did execute all 36 planned experiments, but **none passed promotion gates**.  
Because no model was promotable, the pipeline selected a **fallback** model (`fo_options_structure_only__logreg_c1`) that had `net_return_sum = 0.0` (better than negative values), even though it was non-promotable.

That selected fallback model produced very low probabilities:

- `CE prob max = 0.1094`
- `PE prob max = 0.0978`

With thresholds `CE=0.35`, `PE=0.30`, no rows could trigger trades, so stage-B had:

- `trades = 0`
- `block_rate = 1.0`
- `promotion = HOLD`

Important: `run_id=20260308_165135` never completed, so it cannot be champion.

---

## 2) Basic Terms

- **Run**: One training/evaluation job execution identified by `run_id`.
- **Experiment**: One `(feature_set, model)` combination inside a run.
- **Champion**: The selected candidate after run completion.
- **Promotable**: Candidate that passes required gates (not just best score).
- **Fallback champion**: Selected only when no promotable candidate exists.

---

## 3) What We Intended To Do

Goal: Keep labels/window fixed and run a full in-space search (no pipeline redesign) to find a better model.

Planned search space:

- 9 feature sets
- 4 model specs
- total 36 experiments

Target scoring policy:

- objective: `trade_utility`
- thresholds: `CE=0.35`, `PE=0.30`
- strict promotion gates

---

## 4) What Actually Ran

### Run Status Snapshot

1. `run_id=20260308_165135`  
   - profile: `v2_2_1y_end20240731_hold3m_h15_tp25_sl08_thr26`  
   - status: `running` (stuck), phase `holdout_build`, event `start`  
   - not completed, not eligible for champion

2. `run_id=20260308_164057`  
   - status: completed  
   - default stage-eval at that time: HOLD

3. `run_id=20260308_190837`  
   - status: completed  
   - full 36-experiment search done  
   - no promotable model

Notes:

- There is also a `run_failed_20260308_190837.json` artifact from an earlier failed attempt (TP precheck).  
  Final successful run artifacts for `20260308_190837` do exist and were used for selection.

---

## 5) Training Setup Used in the 36-Experiment Search

From `run_20260308_190837.json` and profile reports:

- feature profile: `futures_core`
- objective: `trade_utility`
- label instrument: `futures`
- label target: `path_tp_sl_time_stop_zero`
- horizon: `15m`
- TP: `0.0025`
- SL: `0.0008`
- window: `2023-08-01` to `2024-07-31`
- holdout: `2024-08-01` to `2024-10-31`

### Feature sets trained (9)

- `fo_full`
- `fo_no_opening_range`
- `fo_no_time_context`
- `fo_no_oi_volume`
- `fo_no_otm_levels`
- `fo_atm_plus_aggregates`
- `fo_trend_vol_only`
- `fo_options_structure_only`
- `fo_core_momentum`

### Models trained (4)

- `logreg_c1`
- `logreg_balanced`
- `xgb_fast`
- `xgb_balanced`

Total experiments: `9 x 4 = 36`.

---

## 6) EDA -> FE -> Modeling -> Eval Flow (Code-Level)

This section is the exact flow used by code, in plain language.

### Orchestrators

1. Runtime training wrapper:
   - `ml_pipeline/src/ml_pipeline/train_model_standard.py`
2. Core pipeline:
   - `ml_pipeline/src/ml_pipeline/train_two_year_pipeline.py`

`train_model_standard` calls `run_two_year_pipeline(...)`, then performs publish/eval summary steps.

### Stage A: EDA (separate diagnostic pipeline, optional)

Main file:

- `ml_pipeline/src/ml_pipeline/eda/stage.py`

What it does:

- profiles raw archive quality (`quality_profiler`)
- builds canonical event snapshots for selected days
- writes profiling artifacts/reports for diagnostics

Important note:

- EDA stage is not required for every training run; it is a parallel diagnostic capability.

### Stage B: Canonical Dataset Build (`T03`)

Main file:

- `ml_pipeline/src/ml_pipeline/dataset_builder.py`

Called from:

- `run_two_year_pipeline(...)`

What it does:

- loads futures, spot, options minute data day-by-day
- aligns by timestamp
- computes ATM and neighboring strike option slices (`m1/0/p1`)
- adds aggregate option stats (`ce_oi_total`, `pe_oi_total`, `pcr_oi`, etc.)
- outputs canonical panel parquet (`*_t03_canonical_panel.parquet`)

### Stage C: Feature Engineering (`T04`)

Main file:

- `ml_pipeline/src/ml_pipeline/feature/engineering.py`

What it adds:

- futures momentum/trend/volatility features (`ret_*`, `ema_*`, `rsi`, `atr`)
- opening-range features
- basis/volume context
- option-derived context (`atm_call_return_1m`, `ce_pe_oi_diff`, etc.)
- DTE/expiry flags
- VIX-derived context
- regime flags (`attach_regime_features`)

Output:

- feature table parquet (`*_t04_features.parquet`)

### Stage D: Labeling (`T05`)

Main file:

- `ml_pipeline/src/ml_pipeline/label_engine.py`

What it does:

- builds labels using chosen label instrument (`futures` or `options`)
- for your run: futures-based path TP/SL/time-stop target
- writes labeled dataset (`*_t05_labeled_features.parquet`)

Also builds holdout labeled data for reserved months:

- `*_t05_holdout_labeled_features.parquet`

### Stage E: Modeling / Search

Main file:

- `ml_pipeline/src/ml_pipeline/training_cycle.py`

What it does:

- selects base features via profile (`select_feature_columns`)
- applies preprocessing gates (missing-rate, quantile clipping, impute, scale)
- iterates feature-set x model grid
- runs walk-forward folds
- computes predictive metrics and trade-utility metrics
- selects best experiment via objective + constraints logic
- trains final CE/PE models and packages joblib

Outputs:

- training report json (`*_training_cycle_report.json`)
- model package joblib (`*_best_model.joblib`)

### Stage F: Evaluation / Promotion

Main files:

- `ml_pipeline/src/ml_pipeline/evaluation/futures_direction_eval.py`
- `ml_pipeline/src/ml_pipeline/evaluation/futures_stage_metrics.py`
- `ml_pipeline/src/ml_pipeline/publishing/promotion_summary.py`

What it does:

- scores holdout labeled data
- Stage A: predictive-quality gates
- Stage B: futures utility gates (trades/PF/DD/side-share/block-rate)
- Stage C: mapping diagnostics (non-blocking)
- computes `promotion_eligible` and promotion decision (`PROMOTE`/`HOLD`)

Run artifacts written by standard trainer:

- `run_<run_id>.json`
- `futures_stage_eval_<run_id>.json`
- profile threshold report
- model contract

---

## 7) What Data We Actually Had (Snapshot/Input Inventory)

This section answers: "What was available in data?" not "what model selected."

For both `run_id=20260308_164057` and `run_id=20260308_190837`, holdout labeled snapshot data was effectively the same shape:

- rows: `24059`
- columns: `188`

The holdout snapshots contained futures, spot, options, and derived regime/label fields.  
Column family counts (holdout labeled frame):

- `fut_*`: 10
- `spot_*`: 4
- `opt_*`: 36
- `strike_*`: 4
- `atm_*`: 5
- `ce_*`: 21
- `pe_*`: 19
- `ret_*`: 3
- `ema_*`: 7
- `opening_range_*`: 5
- `regime_*`: 8
- `vix_*`: 3
- label/forward/path columns: 23

Key non-null rates in holdout (to prove option snapshot data existed):

- `opt_0_ce_close`: `0.9998`
- `opt_0_pe_close`: `0.9994`
- `opt_m1_ce_close`: `0.9998`
- `opt_p1_pe_close`: `0.9990`
- `ce_oi_total`: `1.0000`
- `pe_oi_total`: `1.0000`
- `pcr_oi`: `1.0000`
- `fut_close`: `1.0000`
- `spot_close`: `0.9975`
- `dte_days`: `1.0000`
- `is_expiry_day`: `1.0000`
- `is_near_expiry`: `1.0000`

Conclusion: input data was rich; low-probability outcome was not because option data was missing from snapshots.

---

## 8) What Features Were Available vs What Features Were Used

### Base feature profile selected for this run

Run `20260308_190837` used `feature_profile = futures_core`.

Feature-count by profile on same labeled training frame:

- `futures_core`: 37
- `futures_options_only`: 128
- `all`: 134
- `core_v2`: 40

So with `futures_core`, the base candidate set is intentionally much narrower than full options-rich feature space.

### Feature-set intersections inside this run

When `futures_core` base columns are intersected with the 9 feature sets:

- `fo_full`: 37
- `fo_no_opening_range`: 34
- `fo_no_time_context`: 35
- `fo_no_oi_volume`: 37
- `fo_no_otm_levels`: 37
- `fo_atm_plus_aggregates`: 20
- `fo_trend_vol_only`: 20
- `fo_options_structure_only`: 2 (`minute_of_day`, `day_of_week`)
- `fo_core_momentum`: 20

This is why `fo_options_structure_only` became only 2 features in this specific run design.

### Final selected model input contract

Current published model contract requires:

- `minute_of_day`
- `day_of_week`

(`required_count = 2`)

---

## 9) Objective and Gate Logic (What Model Was Trained For)

Training objective was `trade_utility`.

At experiment evaluation time:

1. Model predicts CE and PE probabilities on test fold.
2. Action rule:
   - buy CE if `ce_prob >= ce_threshold` and CE dominates
   - buy PE if `pe_prob >= pe_threshold` and PE dominates
   - else HOLD
3. Trade return uses path-exit semantics:
   - `tp` -> `+take_profit_pct`
   - `sl` -> `-stop_loss_pct`
   - `time_stop` -> 0 or dropped depending config
   - minus `cost_per_trade`
4. Utility aggregates:
   - `trades_total`
   - `net_return_sum`
   - `profit_factor`
   - `max_drawdown_pct`
5. Constraints must pass for promotable objective:
   - `trades_total >= min_trades`
   - `profit_factor >= min_profit_factor`
   - `abs(max_drawdown_pct) <= max_equity_drawdown_pct`

If constraints fail, `objective_value = null` for primary champion selection.

Run `20260308_190837` utility config:

- `ce_threshold=0.35`
- `pe_threshold=0.30`
- `min_profit_factor=1.3`
- `max_equity_drawdown_pct=0.15`
- `min_trades=50`
- `cost_per_trade=0.0006`

---

## 10) How Champion Selection Works in This Pipeline

### Primary rule (strict)

For `trade_utility`, a candidate gets a valid objective only if utility constraints pass.

- If constraints fail -> `objective_value = null`
- If constraints pass -> `objective_value = net_return_sum`

Then best candidate is selected among non-null objective values.

### Fallback rule (only when no promotable model)

If all candidates have `objective_value = null`, fallback ranks by raw `net_return_sum` anyway (even if non-promotable).  
This can pick zero-trade/zero-return candidates if most others are negative.

---

## 11) Why No Champion Was Promoted

In `run_id=20260308_190837`:

- `experiments_total = 36`
- `promotable_count = 0`
- `no_promotable_model = true`

Across leaderboard:

- many experiments traded, but none passed constraints
- none had positive net return under the configured gates

So promotion decision was HOLD.

---

## 12) Why Selected Probabilities Were So Low

Selected fallback model:

- `experiment_id = fo_options_structure_only__logreg_c1`
- selected by fallback
- non-promotable

On holdout scoring:

- CE mean: `0.0738`, max: `0.1094`
- PE mean: `0.0789`, max: `0.0978`

Configured thresholds:

- CE threshold: `0.35`
- PE threshold: `0.30`

Result:

- CE hits = 0
- PE hits = 0
- all rows HOLD
- stage-B trades = 0

---

## 13) Why `fo_options_structure_only` Became Only 2 Features Here

This is a key detail.

Base feature profile for the run was `futures_core`, which mostly contains futures/regime/time features and does not include option microstructure columns like `opt_*`, `strike_*`, `ce_oi_total`, etc.

Then feature set `fo_options_structure_only` applies include-regex for mostly option-structure columns plus:

- `minute_of_day`
- `day_of_week`

Since most option-structure columns were absent from the `futures_core` base set, the intersection left only:

- `minute_of_day`
- `day_of_week`

So this was not random behavior; it is deterministic from:

1. base feature profile (`futures_core`)
2. include rules of `fo_options_structure_only`

---

## 14) Why This Felt Contradictory vs Earlier Good Results

Earlier re-evaluation artifact (`164057` with tuned thresholds) showed good stage-B:

- `trades = 68`
- `PF = 1.62`
- `net_return_sum = +0.0492`
- `decision = PROMOTE`

and earlier probability tails were much higher:

- `ce_prob_max = 0.4381`
- `pe_prob_max = 0.4566`

But this came from a different evaluated model/selection path than the fallback selected in `190837`.

---

## 15) What `trade_sheet_20260308_164057_*` Experiments Show

This section explains why you saw "good results" in some artifacts.

### A) Futures return replay with model signals (`thr35_30`)

`trade_sheet_20260308_164057_thr35_30_summary.json`:

- trades: 68
- win rate: 58.82%
- net return sum: `+0.0492489` (R-style, relative)
- avg net return/trade: `+0.0007242`

This aligns with stage-eval promotion for that re-evaluation path.

### B) ATM option premium recalc (strict contract mapping)

`trade_sheet_20260308_164057_atm_option_recalc_100k_summary.json`:

- trades: 0
- total net pnl: `0`

Interpretation: model signals existed, but strict option contract mapping did not find executable entries under that recalc policy.

### C) ATM option fallback recalc (nearest expiry/strike fallback)

`trade_sheet_20260308_164057_atm_option_recalc_fallback_100k_summary.json`:

- trades: 68
- win rate: 60.29%
- total net pnl: `588,208.26` INR on `100,000` stake/trade framework

Interpretation: with relaxed contract mapping fallback, the same signal stream produced executable option trades and positive PnL.

### Why these can look conflicting

They are different execution/repricing assumptions:

- futures-return proxy utility
- strict option contract mapping
- fallback option mapping

So they are not contradictory; they answer different operational questions.

---

## 16) Direct Answers to the Main Questions

### Q1. Why were we not able to get a champion?

Because in the 36-experiment run, zero candidates passed the promotion constraints (`promotable_count=0`), so primary selection could not choose a promotable model.

### Q2. Why were probabilities so low?

Because the fallback-selected model for that run behaved like a weak/base-rate predictor for this task, with outputs clustered around ~0.07 to ~0.10, far below operational thresholds.

### Q3. Did the 36-grid run actually execute?

Yes. It completed all 36 experiments. The failure was quality/promotion outcome, not coverage.

### Q4. Is `run_id=20260308_165135` the champion run?

No. It is incomplete (`status=running`) and cannot be used as champion.

---

## 17) Suggested Guardrails (Process, Not Theory)

1. Do not auto-publish fallback_non_promotable model packages to active runtime path.
2. In fallback ranking, penalize `trades < min_trades` (or force `-inf`) so zero-trade models cannot win by `0 > negative`.
3. Make promotion state explicit in runtime switch logic (only promotable run IDs eligible unless forced override).
4. When using `feature_profile=futures_core`, avoid running option-only feature sets or tag them as expected-low-information variants.

---

## 18) Evidence Files

Primary run artifacts:

- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/reports/training/run_status_20260308_165135.json`
- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/reports/training/run_status_20260308_190837.json`
- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/reports/training/run_20260308_190837.json`
- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/reports/training/champion_selection_20260308_190837.json`
- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/reports/training/futures_stage_eval_20260308_190837.json`
- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/config/profiles/v2_3_1y_end20240731_hold3m_h15_tp25_sl08_grid36_thr35_30/training_report.json`
- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/model_contract.json`

Historical comparison artifacts:

- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/reports/training/futures_stage_eval_20260308_164057_thr35_30_publish.json`
- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/reports/training/threshold_scan_20260308_164057_quickgate.json`
- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/reports/training/trade_sheet_20260308_164057_thr35_30_summary.json`
- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/reports/training/trade_sheet_20260308_164057_atm_option_recalc_100k_summary.json`
- `ml_pipeline/artifacts/models/by_features/banknifty_futures/h15_tp_auto/reports/training/trade_sheet_20260308_164057_atm_option_recalc_fallback_100k_summary.json`

Code paths for selection behavior:

- `ml_pipeline/src/ml_pipeline/training_cycle.py`
- `ml_pipeline/src/ml_pipeline/train_model_standard.py`
- `ml_pipeline/src/ml_pipeline/feature/profiles.py`
