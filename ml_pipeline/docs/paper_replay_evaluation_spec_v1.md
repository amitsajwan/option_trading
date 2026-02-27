# Paper Replay Evaluation Spec V1 (T21)

T21 evaluates emitted paper decisions against realized labeled outcomes.

## Objective

Map each replay decision (`BUY_CE`/`BUY_PE`) to a same-timestamp labeled row and compute realized PnL under selected execution profile.

## Inputs

- Decisions JSONL (`t11_paper_decisions.jsonl`)
- Labeled dataset (`t05_labeled_features.parquet`)
- Threshold report (`t08_threshold_report.json`)
- Optional strategy profile report (`t19_strategy_comparison_v2_report.json`)

## Process

1. Parse decisions and normalize timestamps.
2. Join decision timestamp to labeled timestamp.
3. For matched buy decisions:
   - compute trade outcome with selected profile (`fixed_horizon` or `path_v2`)
   - apply cost + base slippage + fill-model slippage
4. Produce:
   - event-level evaluated trades
   - summary report (match rate, returns, exit reasons)

## CLI

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.paper_replay_evaluation --decisions-jsonl ml_pipeline\artifacts\t11_paper_decisions.jsonl --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --t19-report ml_pipeline\artifacts\t19_strategy_comparison_v2_report.json --trades-out ml_pipeline\artifacts\t21_replay_evaluation_trades.parquet --report-out ml_pipeline\artifacts\t21_replay_evaluation_report.json
```

## Artifacts

- `ml_pipeline/artifacts/t21_replay_evaluation_trades.parquet`
- `ml_pipeline/artifacts/t21_replay_evaluation_report.json`
