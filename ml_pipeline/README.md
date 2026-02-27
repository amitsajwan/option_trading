# ML Pipeline (BankNifty Intraday Option Buying)

This module provides a reproducible ML workflow for the strategy blueprint:

- 1-minute feature generation
- 3-minute directional-strength label
- chronological split + optional walk-forward
- probability threshold optimization
- simple cost-aware backtest

Execution tracking: see `ml_pipeline/EXECUTION_PLAN.md`.
Phase 2 tracking: see `ml_pipeline/PHASE2_EXECUTION_PLAN.md`.
Phase 3 tracking: see `ml_pipeline/PHASE3_EXECUTION_PLAN.md`.
Current canonical docs: `ml_pipeline/PHASE3_EXECUTION_PLAN.md`, `ml_pipeline/docs/live_inference_spec_v2.md`, `ml_pipeline/docs/operator_runbook_v2.md`, `ml_pipeline/docs/model_card_v2_addendum.md`, `ml_pipeline/docs/retraining_sop_v2_addendum.md`.
Legacy docs note: older `v1`/historical docs are retained for audit trail; use current canonical docs for active implementation.
Core data/model specs: `ml_pipeline/docs/data_contract.md`, `ml_pipeline/docs/feature_spec_v1.md`, `ml_pipeline/docs/label_spec_v2.md`, `ml_pipeline/docs/walk_forward_spec_v1.md`.
Core execution specs: `ml_pipeline/docs/live_inference_spec_v2.md`, `ml_pipeline/docs/execution_simulator_spec_v2.md`, `ml_pipeline/docs/execution_monitoring_spec_v1.md`.
Core operations docs: `ml_pipeline/docs/operator_runbook_v2.md`, `ml_pipeline/docs/model_card_v2_addendum.md`, `ml_pipeline/docs/retraining_sop_v2_addendum.md`, `ml_pipeline/docs/reproducibility_spec_v2.md`.
User guide (stage commands): `ml_pipeline/artifacts/data/README.md` (`EDA -> FE -> MODEL -> EVAL` quick path included).
Historical and review docs: keep under `ml_pipeline/docs/` for audit/reference only.

## Phase 3 Active Task Map

- `T26`: dataset freeze + split lineage report (`ml_pipeline/artifacts/t26_dataset_freeze_report.json`, `ml_pipeline/artifacts/t26_dataset_freeze_summary.md`)
- `T27`: label/horizon validation (update `ml_pipeline/docs/label_spec_v2.md` + report artifact)
- `T28`: leakage audit + purged walk-forward (update `ml_pipeline/docs/walk_forward_spec_v1.md` + report artifact)
- `T29`: model zoo + hyperparameter optimization (training/eval artifacts + champion selection report)
- `T30`: execution simulator v2 (`ml_pipeline/docs/execution_simulator_spec_v2.md`)
- `T31`: calibration + thresholding (update threshold/calibration docs + report artifact)
- `T32`: overfit/underfit diagnostics + cost/slippage stress (diagnostics + robustness artifacts)
- `T33`: order intent + reconciliation + runtime guards (contracts + reconciliation/guard reports)
- `T34`: consolidated runbook/model card/SOP/reproducibility (`v2` docs updated in place)

## Install

```powershell
python -m pip install -r ml_pipeline/requirements-ml.txt
```

## Post-Training Evaluation (Model Package + Threshold Report)

After `feature.label_stage` -> `feature.stage` -> `modeling.train`, run:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.evaluation.model_eval --model-package ml_pipeline\artifacts\models\by_features\core_v2\h5_ts0_lgbm_regime\model\model.joblib --threshold-report ml_pipeline\artifacts\models\by_features\core_v2\h5_ts0_lgbm_regime\config\profiles\openfe_v9_dual\threshold_report.json --train ml_pipeline\artifacts\data\feature_engineering\features\core_v2\train.parquet --valid ml_pipeline\artifacts\data\feature_engineering\features\core_v2\valid.parquet --eval ml_pipeline\artifacts\data\feature_engineering\features\core_v2\eval.parquet --profile-id openfe_v9_dual --latest-days 20 --oos-split eval
```

Outputs:

- `.../reports/evaluation/openfe_v9_dual_evaluation_report.json`
- `.../reports/evaluation/openfe_v9_dual_eval_summary.json` (used by Trading Model Catalog / terminal quality snapshot)

## Expected input data

Single CSV/Parquet with at least:

- `timestamp`
- Futures: `fut_open`, `fut_high`, `fut_low`, `fut_close`, `fut_volume`

Useful optional columns:

- `fut_vwap`
- `atm_call_close`, `atm_put_close`
- `atm_oi`, `ce_oi`, `pe_oi`
- `iv`

Current Phase 3 model track uses futures+options features. Spot/depth fields are not required unless explicitly re-enabled in plan.

## T26 Dataset Freeze (Futures+Options-Only)

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.dataset_freeze --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --feature-profile futures_options_only --report-out ml_pipeline\artifacts\t26_dataset_freeze_report.json --summary-out ml_pipeline\artifacts\t26_dataset_freeze_summary.md
```

## T27 Label and Horizon Validation (Breakout-Aware)

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.label_validation --labeled-data ml_pipeline\artifacts\t29_2y_auto_t05_labeled_features.parquet --horizon-minutes 3 --return-threshold 0.002 --alt-out ml_pipeline\artifacts\t27_breakout_alternative_labels.parquet --report-out ml_pipeline\artifacts\t27_label_validation_report.json --summary-out ml_pipeline\artifacts\t27_label_validation_summary.md
```

## T28 Leakage Audit + Purged Walk-Forward

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.leakage_audit --labeled-data ml_pipeline\artifacts\t29_2y_auto_t05_labeled_features.parquet --feature-profile futures_options_only --train-days 180 --valid-days 30 --test-days 30 --step-days 30 --purge-days 1 --embargo-days 1 --report-out ml_pipeline\artifacts\t28_leakage_audit_report.json --summary-out ml_pipeline\artifacts\t28_leakage_audit_summary.md
```

## T31 Calibration + Thresholding V2

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.calibration_threshold_v2 --labeled-data ml_pipeline\artifacts\t29_2y_auto_t05_labeled_features.parquet --feature-profile futures_options_only --train-days 180 --valid-days 30 --test-days 30 --step-days 30 --purge-days 1 --embargo-days 1 --report-out ml_pipeline\artifacts\t31_calibration_threshold_report.json --summary-out ml_pipeline\artifacts\t31_calibration_threshold_summary.md
```

## T29 Training Cycle (Feature + Model Combinations)

Runs iterative feature-set/model combinations on real labeled historical data with preprocessing:
- missing-rate feature gate
- quantile clipping
- median imputation

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.training_cycle --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --feature-profile futures_options_only --objective rmse --train-days 3 --valid-days 1 --test-days 1 --step-days 1 --report-out ml_pipeline\artifacts\t29_training_cycle_report.json --model-out ml_pipeline\artifacts\t29_best_model.joblib
```

## Automated 2-Year Training (Real Historical Data)

One command to run:
1) day discovery (common fut/options/spot days),
2) 2-year window selection,
3) canonical panel build,
4) feature generation,
5) label generation,
6) feature/model training-cycle search.

Supports resume by reusing existing chunk/stage artifacts.

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.train_two_year_pipeline --base-path C:\Users\amits\Downloads\archive\banknifty_data --lookback-years 2 --artifact-prefix t29_2y_auto --objective rmse --feature-profile futures_options_only --train-days 180 --valid-days 30 --test-days 30 --step-days 30 --max-experiments 8 --chunk-size-days 70
```

Disable reuse/resume (full rebuild):

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.train_two_year_pipeline --base-path C:\Users\amits\Downloads\archive\banknifty_data --lookback-years 2 --artifact-prefix t29_2y_auto --objective rmse --feature-profile futures_options_only --train-days 180 --valid-days 30 --test-days 30 --step-days 30 --max-experiments 8 --chunk-size-days 70 --no-reuse-artifacts
```

## Train

```powershell
python -m ml_pipeline.train --data C:\path\banknifty_features.parquet --model-out ml_pipeline\artifacts\model.joblib --report-out ml_pipeline\artifacts\train_report.json
```

## Backtest

```powershell
python -m ml_pipeline.backtest --data C:\path\banknifty_features.parquet --model ml_pipeline\artifacts\model.joblib --report-out ml_pipeline\artifacts\backtest_report.json
```

## Notes

- Default target is 3-minute future return threshold (`0.20%`).
- Optional MAE/MFE gating is supported in config.
- This is intentionally separate from `market_data` runtime and should consume exported historical data from your canonical replay pipeline.

## Schema Validation (T01)

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
$env:LOCAL_HISTORICAL_BASE = "C:\Users\amits\Downloads\archive\banknifty_data"
python -m ml_pipeline.schema_validator --out ml_pipeline\artifacts\t01_schema_validation_report.json
```

Run tests:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m unittest discover -s ml_pipeline/tests -v
```

## T02 Data Quality Profiling

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
$env:LOCAL_HISTORICAL_BASE = "C:\Users\amits\Downloads\archive\banknifty_data"
python -m ml_pipeline.quality_profiler
```

Artifacts:

- `ml_pipeline/artifacts/t02_data_quality_report.json`
- `ml_pipeline/artifacts/t02_data_quality_summary.md`

## T03 Canonical Dataset Build

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
$env:LOCAL_HISTORICAL_BASE = "C:\Users\amits\Downloads\archive\banknifty_data"
python -m ml_pipeline.dataset_builder
```

Artifact:

- `ml_pipeline/artifacts/t03_canonical_panel.parquet`

## T04 Feature Table Build

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.feature_engineering --panel ml_pipeline\artifacts\t03_canonical_panel.parquet --out ml_pipeline\artifacts\t04_features.parquet
```

Artifact:

- `ml_pipeline/artifacts/t04_features.parquet`

## T05 Label Build

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
$env:LOCAL_HISTORICAL_BASE = "C:\Users\amits\Downloads\archive\banknifty_data"
python -m ml_pipeline.label_engine --features ml_pipeline\artifacts\t04_features.parquet --out ml_pipeline\artifacts\t05_labeled_features.parquet --report-out ml_pipeline\artifacts\t05_label_report.json
```

Artifacts:

- `ml_pipeline/artifacts/t05_labeled_features.parquet`
- `ml_pipeline/artifacts/t05_label_report.json`

## T15 Label Engine V2 (Path-Aware Outcomes)

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
$env:LOCAL_HISTORICAL_BASE = "C:\Users\amits\Downloads\archive\banknifty_data"
python -m ml_pipeline.label_engine --features ml_pipeline\artifacts\t04_features.parquet --out ml_pipeline\artifacts\t05_labeled_features.parquet --report-out ml_pipeline\artifacts\t05_label_report.json --path-report-out ml_pipeline\artifacts\t15_label_path_report.json --stop-loss-pct 0.12 --take-profit-pct 0.24
```

Artifact:

- `ml_pipeline/artifacts/t15_label_path_report.json`

## T06 Baseline Training

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.train_baseline --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --model-out ml_pipeline\artifacts\t06_baseline_model.joblib --report-out ml_pipeline\artifacts\t06_train_report.json
```

Artifacts:

- `ml_pipeline/artifacts/t06_baseline_model.joblib`
- `ml_pipeline/artifacts/t06_train_report.json`

## T07 Walk-Forward Validation

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.walk_forward --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --report-out ml_pipeline\artifacts\t07_walk_forward_report.json --train-days 3 --valid-days 1 --test-days 1 --step-days 1
```

Artifact:

- `ml_pipeline/artifacts/t07_walk_forward_report.json`

## T08 Threshold Optimization

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.threshold_optimization --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --report-out ml_pipeline\artifacts\t08_threshold_report.json --train-days 3 --valid-days 1 --test-days 1 --step-days 1
```

Artifact:

- `ml_pipeline/artifacts/t08_threshold_report.json`

## T09 Backtest Engine

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.backtest_engine --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --trades-out ml_pipeline\artifacts\t09_backtest_trades.parquet --report-out ml_pipeline\artifacts\t09_backtest_report.json
```

Artifacts:

- `ml_pipeline/artifacts/t09_backtest_trades.parquet`
- `ml_pipeline/artifacts/t09_backtest_report.json`

## T10 Strategy Comparison Harness

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.strategy_comparison --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --report-out ml_pipeline\artifacts\t10_strategy_comparison_report.json --cost-grid default,0.001,0.002
```

Artifact:

- `ml_pipeline/artifacts/t10_strategy_comparison_report.json`

## T11 Live Inference Adapter (Paper Mode)

Replay dry-run:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode replay-dry-run --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --feature-parquet ml_pipeline\artifacts\t04_features.parquet --output-jsonl ml_pipeline\artifacts\t11_paper_decisions.jsonl --limit 200
```

Live API polling mode:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode live-api --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --instrument BANKNIFTY-I --market-api-base http://127.0.0.1:8004 --dashboard-api-base http://127.0.0.1:8002 --output-jsonl ml_pipeline\artifacts\t11_live_paper_decisions.jsonl --poll-seconds 5 --max-iterations 60
```

## T12 Monitoring and Drift Checks

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.monitoring_drift --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --reference-features ml_pipeline\artifacts\t04_features.parquet --current-features ml_pipeline\artifacts\t04_features.parquet --current-decisions ml_pipeline\artifacts\t11_paper_decisions.jsonl --report-out ml_pipeline\artifacts\t12_drift_report.json --summary-out ml_pipeline\artifacts\t12_drift_summary.md
```

Artifacts:

- `ml_pipeline/artifacts/t12_drift_report.json`
- `ml_pipeline/artifacts/t12_drift_summary.md`

## T13 Documentation and Reproducibility Run

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.reproducibility_runner --base-path C:\Users\amits\Downloads\archive\banknifty_data --workdir ml_pipeline\artifacts\t13_reproducibility --report-out ml_pipeline\artifacts\t13_reproducibility_report.json --summary-out ml_pipeline\artifacts\t13_reproducibility_summary.md
```

Artifacts:

- `ml_pipeline/artifacts/t13_reproducibility_report.json`
- `ml_pipeline/artifacts/t13_reproducibility_summary.md`

## T14 Exit Policy Contract and Validation

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.exit_policy --report-out ml_pipeline\artifacts\t14_exit_policy_validation_report.json --normalized-out ml_pipeline\artifacts\t14_exit_policy_config.json
```

Artifacts:

- `ml_pipeline/artifacts/t14_exit_policy_validation_report.json`
- `ml_pipeline/artifacts/t14_exit_policy_config.json`

## T16 Backtest Engine V2 (Intrabar Exit Simulation)

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.backtest_engine --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --execution-mode path_v2 --intrabar-tie-break sl --slippage-per-trade 0.0002 --trades-out ml_pipeline\artifacts\t16_backtest_trades.parquet --report-out ml_pipeline\artifacts\t16_backtest_report.json
```

Artifacts:

- `ml_pipeline/artifacts/t16_backtest_trades.parquet`
- `ml_pipeline/artifacts/t16_backtest_report.json`

## T17 Dynamic Exit Policy V1

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.dynamic_exit_policy --out ml_pipeline\artifacts\t17_dynamic_exit_policy_report.json
```

Artifact:

- `ml_pipeline/artifacts/t17_dynamic_exit_policy_report.json`

## T18 Exit Policy Optimization Harness

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.exit_policy_optimization --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --tie-break-grid sl,tp --slippage-grid 0.0,0.0002,0.0005 --forced-eod-grid 15:24 --report-out ml_pipeline\artifacts\t18_exit_policy_optimization_report.json
```

Artifact:

- `ml_pipeline/artifacts/t18_exit_policy_optimization_report.json`

## T19 Strategy Comparison V2

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.strategy_comparison_v2 --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --t18-report ml_pipeline\artifacts\t18_exit_policy_optimization_report.json --report-out ml_pipeline\artifacts\t19_strategy_comparison_v2_report.json
```

Artifact:

- `ml_pipeline/artifacts/t19_strategy_comparison_v2_report.json`

## T20 Slippage + Fill Model V1

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.backtest_engine --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --execution-mode path_v2 --fill-model liquidity_adjusted --fill-spread-fraction 0.5 --fill-volume-impact 0.02 --fill-min 0.0 --fill-max 0.01 --slippage-per-trade 0.0002 --trades-out ml_pipeline\artifacts\t20_backtest_trades.parquet --report-out ml_pipeline\artifacts\t20_backtest_report.json
```

Artifacts:

- `ml_pipeline/artifacts/t20_backtest_trades.parquet`
- `ml_pipeline/artifacts/t20_backtest_report.json`

## T21 Paper Replay Evaluation Harness

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.paper_replay_evaluation --decisions-jsonl ml_pipeline\artifacts\t11_paper_decisions.jsonl --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --t19-report ml_pipeline\artifacts\t19_strategy_comparison_v2_report.json --trades-out ml_pipeline\artifacts\t21_replay_evaluation_trades.parquet --report-out ml_pipeline\artifacts\t21_replay_evaluation_report.json
```

Artifacts:

- `ml_pipeline/artifacts/t21_replay_evaluation_trades.parquet`
- `ml_pipeline/artifacts/t21_replay_evaluation_report.json`

## T22 Live Inference Adapter V2 (Exit-Aware Paper Mode)

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode replay-dry-run-v2 --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --feature-parquet ml_pipeline\artifacts\t04_features.parquet --output-jsonl ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --limit 300 --max-hold-minutes 5 --confidence-buffer 0.05
```

Artifact:

- `ml_pipeline/artifacts/t22_exit_aware_paper_events.jsonl`

Live API exit-aware mode:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode live-api-v2 --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --instrument BANKNIFTY-I --market-api-base http://127.0.0.1:8004 --dashboard-api-base http://127.0.0.1:8002 --output-jsonl ml_pipeline\artifacts\t30_live_api_v2_events.jsonl --poll-seconds 2 --max-iterations 40 --max-hold-minutes 5 --confidence-buffer 0.05
```

Artifact:

- `ml_pipeline/artifacts/t30_live_api_v2_events.jsonl`

Redis pub/sub exit-aware mode (no REST):

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode live-redis-v2 --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --instrument BANKNIFTY-I --redis-host localhost --redis-port 6380 --redis-db 0 --depth-channel market:depth:BANKNIFTY-I --output-jsonl ml_pipeline\artifacts\t30_live_redis_v2_events.jsonl --max-iterations 200 --max-hold-minutes 5 --confidence-buffer 0.05 --max-idle-seconds 120
```

Artifact:

- `ml_pipeline/artifacts/t30_live_redis_v2_events.jsonl`

## T33 Order Intent, Shadow Reconciliation, and Runtime Guards

Builds an idempotent order-intent stream from decision events, reconciles intents vs fills, and evaluates runtime kill-switch guards.

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.order_intent_runtime --decisions-jsonl ml_pipeline\artifacts\t33_paper_capital_events_actual.jsonl --report-out ml_pipeline\artifacts\t33_order_runtime_report.json --summary-out ml_pipeline\artifacts\t33_order_runtime_summary.md --intents-out ml_pipeline\artifacts\t33_order_intents.parquet --fills-out ml_pipeline\artifacts\t33_order_fills.parquet
```

Artifacts:

- `ml_pipeline/artifacts/t33_order_runtime_report.json`
- `ml_pipeline/artifacts/t33_order_runtime_summary.md`
- `ml_pipeline/artifacts/t33_order_intents.parquet`
- `ml_pipeline/artifacts/t33_order_fills.parquet`

## Paper Capital Runner (Console MTM)

Print CE/PE/Total mark-to-market capital on every new bar emitted from Redis:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.paper_capital_runner --mode dual --instrument BANKNIFTY-I --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --redis-host localhost --redis-port 6379 --redis-db 0 --initial-ce-capital 1000 --initial-pe-capital 1000 --fee-bps 0 --max-iterations 240 --max-hold-minutes 5 --confidence-buffer 0.05 --max-idle-seconds 120 --output-jsonl ml_pipeline\artifacts\t33_paper_capital_events.jsonl
```

Risk/exit controls:

- `--stop-loss-pct 8` (hard SL at -8% from entry)
- `--trailing-enabled --trailing-activation-pct 10 --trailing-offset-pct 5` (trail after +10% move, keep 5% gap)
- `--model-exit-policy signal_only` (ignore `time_stop`/`confidence_fade`; keep exits only on signal flip or stop logic)
- `--model-exit-policy training_parity` (suppress `signal_flip`/`confidence_fade`; keep `time_stop` + risk exits to match training-path semantics)
- `--runtime-guard-max-consecutive-losses 4` (halt new entries after 4 losing exits in a row)
- `--runtime-guard-max-drawdown-pct 30` (halt new entries after 30% MTM drawdown)

Artifact:

- `ml_pipeline/artifacts/t33_paper_capital_events.jsonl`

## T34 Documentation Consolidation + Reproducibility V3

Run deterministic Phase 3 reproducibility (T32 + T33) over frozen model/data inputs:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.phase3_reproducibility_runner --labeled-data ml_pipeline\artifacts\t29_2y_auto_t05_labeled_features.parquet --model-package ml_pipeline\artifacts\t29_2y_auto_best_model.joblib --threshold-report ml_pipeline\artifacts\t31_calibration_threshold_report.json --decisions-jsonl ml_pipeline\artifacts\t33_paper_capital_events_actual.jsonl --workdir ml_pipeline\artifacts\t34_phase3_reproducibility --report-out ml_pipeline\artifacts\t34_phase3_reproducibility_report.json --summary-out ml_pipeline\artifacts\t34_phase3_reproducibility_summary.md
```

Artifacts:

- `ml_pipeline/artifacts/t34_phase3_reproducibility_report.json`
- `ml_pipeline/artifacts/t34_phase3_reproducibility_summary.md`

## T23 Monitoring V2 (Exit Drift + Execution Quality)

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.monitoring_execution --reference-events ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --current-events ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --report-out ml_pipeline\artifacts\t23_execution_monitoring_report.json --summary-out ml_pipeline\artifacts\t23_execution_monitoring_summary.md
```

Artifacts:

- `ml_pipeline/artifacts/t23_execution_monitoring_report.json`
- `ml_pipeline/artifacts/t23_execution_monitoring_summary.md`

## T32 Overfit/Underfit Diagnostics + Cost/Slippage Stress

Runs chronological train/valid/test diagnostics for CE/PE:
- train-vs-valid gap checks
- learning-curve checkpoints
- post-cost and slippage sensitivity matrix

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.diagnostics_stress --labeled-data ml_pipeline\artifacts\t29_2y_auto_t05_labeled_features.parquet --model-package ml_pipeline\artifacts\t29_2y_auto_best_model.joblib --threshold-report ml_pipeline\artifacts\t31_calibration_threshold_report.json --report-out ml_pipeline\artifacts\t32_diagnostics_stress_report.json --summary-out ml_pipeline\artifacts\t32_diagnostics_stress_summary.md
```

Artifacts:

- `ml_pipeline/artifacts/t32_diagnostics_stress_report.json`
- `ml_pipeline/artifacts/t32_diagnostics_stress_summary.md`

## T24 Documentation and Reproducibility Flow V2

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.phase2_reproducibility_runner --base-path C:\Users\amits\Downloads\archive\banknifty_data --workdir ml_pipeline\artifacts\t24_phase2_reproducibility --bootstrap-phase1 --report-out ml_pipeline\artifacts\t24_phase2_reproducibility_report.json --summary-out ml_pipeline\artifacts\t24_phase2_reproducibility_summary.md
```

Artifacts:

- `ml_pipeline/artifacts/t24_phase2_reproducibility_report.json`
- `ml_pipeline/artifacts/t24_phase2_reproducibility_summary.md`

## T30 Execution Simulator V2 (Latency + Partial Fill)

Parquet replay source (deterministic):

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.execution_simulator_v2 --events-jsonl ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --market-source parquet --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --order-latency-ms 350 --exchange-latency-ms 250 --max-participation-rate 0.2 --fee-per-fill-return 0.0003 --fill-model spread_fraction --fill-spread-fraction 0.5 --events-out ml_pipeline\artifacts\t30_execution_events.parquet --report-out ml_pipeline\artifacts\t30_execution_report.json --force-liquidate-end
```

API source (RUN_MODES integration):

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.execution_simulator_v2 --events-jsonl ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --market-source api --instrument BANKNIFTY-I --market-api-base http://127.0.0.1:8004 --dashboard-api-base http://127.0.0.1:8002 --timeout-seconds 5 --order-latency-ms 350 --exchange-latency-ms 250 --max-participation-rate 0.2 --fee-per-fill-return 0.0003 --fill-model spread_fraction --fill-spread-fraction 0.5 --events-out ml_pipeline\artifacts\t30_execution_events_api.parquet --report-out ml_pipeline\artifacts\t30_execution_report_api.json --force-liquidate-end
```

Artifacts:

- `ml_pipeline/artifacts/t30_execution_events.parquet`
- `ml_pipeline/artifacts/t30_execution_report.json`
- `ml_pipeline/artifacts/t30_execution_events_api.parquet`
- `ml_pipeline/artifacts/t30_execution_report_api.json`
- `ml_pipeline/artifacts/t30_execution_events_live_api_v2.parquet`
- `ml_pipeline/artifacts/t30_execution_report_live_api_v2.json`
