# Depth Training Flow V1

> Status: Deprecated for current production track.
> Use `futures + options` model path in `ml_pipeline/PHASE3_EXECUTION_PLAN.md` unless depth data is explicitly captured and approved again.

This flow adds depth into training/evaluation in a reproducible order.

## 1) Capture depth in live decision events

Run inference in Redis event mode so each emitted event contains `depth`:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode live-redis-v2 --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --instrument BANKNIFTY-I --redis-host localhost --redis-port 6380 --redis-db 0 --depth-channel market:depth:BANKNIFTY-I --output-jsonl ml_pipeline\artifacts\t30_live_redis_v2_events.jsonl --max-iterations 400 --max-hold-minutes 5 --confidence-buffer 0.05 --max-idle-seconds 120
```

## 2) Build minute depth dataset from events

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.depth_dataset --events-jsonl ml_pipeline\artifacts\t30_live_redis_v2_events.jsonl --out ml_pipeline\artifacts\t31_depth_dataset.parquet
```

## 3) Rebuild canonical dataset with optional depth merge

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.dataset_builder --base-path C:\Users\amits\Downloads\archive\banknifty_data --days 2023-06-15,2024-10-10 --depth-parquet ml_pipeline\artifacts\t31_depth_dataset.parquet --out ml_pipeline\artifacts\t31_canonical_with_depth.parquet
```

## 4) Build features and labels

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.feature_engineering --panel ml_pipeline\artifacts\t31_canonical_with_depth.parquet --out ml_pipeline\artifacts\t31_features_with_depth.parquet
python -m ml_pipeline.label_engine --features ml_pipeline\artifacts\t31_features_with_depth.parquet --base-path C:\Users\amits\Downloads\archive\banknifty_data --out ml_pipeline\artifacts\t31_labeled_with_depth.parquet
```

## 5) Run depth ablation (proper train/eval compare)

Compares:
- baseline without depth columns
- with-depth variant (if depth columns are present)

Includes:
- baseline training metrics
- walk-forward validation
- threshold optimization
- backtest summary

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.depth_ablation --labeled-data ml_pipeline\artifacts\t31_labeled_with_depth.parquet --report-out ml_pipeline\artifacts\t32_depth_ablation_report.json --train-days 3 --valid-days 1 --test-days 1 --step-days 1
```

## Notes

- Your local archive currently contains `fut/options/spot` only. Depth is not present there by default.
- With this setup, depth must be captured from runtime events first (Step 1/2), then merged (Step 3).
- If no depth columns are found in labeled data, ablation report marks `with_depth` as `no_depth_columns`.
