# Execution Simulator Spec V2 (T30)

T30 adds event-driven execution realism with latency and partial fills.

## Objective

Bridge model decision events to executable outcomes by simulating:

1. decision-to-exchange latency
2. liquidity-constrained fills
3. partial fills and rejections
4. forced end-of-session liquidation

## Inputs

- Decision/event stream JSONL (typically exit-aware events):
  - `ml_pipeline/artifacts/t22_exit_aware_paper_events.jsonl`
- Market snapshot source:
  - `parquet`: labeled dataset (`t05_labeled_features.parquet`)
  - `api`: live dashboard endpoints (`/api/market-data/options/{instrument}`, `/api/market-data/depth/{instrument}`)

## Core Parameters

- `order_latency_ms`
- `exchange_latency_ms`
- `max_participation_rate`
- `fallback_volume`
- `fee_per_fill_return`
- `default_order_qty`
- `force_liquidate_end`
- fill model config (`constant|spread_fraction|liquidity_adjusted`)

## Fill Logic

1. Available quantity is constrained by:
   - option minute volume x participation rate
   - top-of-book depth quantity (bid/ask side), when available
2. `filled_qty = min(requested_qty, available_qty)` -> supports partial fills.
3. Fill price is adjusted by:
   - model slippage (`fill_model.py`)
   - latency impact proxy from next-bar movement (parquet mode)

## Outputs

- Event-level simulation parquet:
  - `ml_pipeline/artifacts/t30_execution_events.parquet`
- Summary report:
  - `ml_pipeline/artifacts/t30_execution_report.json`

Key report fields:

- `fills_total`, `partial_fills_total`, `rejects_total`
- `mean_fill_ratio`
- `closed_trades`, `net_return_sum`, `win_rate`
- `open_position_end_qty`

## CLI

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.execution_simulator_v2 --events-jsonl ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --market-source parquet --labeled-data ml_pipeline\artifacts\t05_labeled_features.parquet --order-latency-ms 350 --exchange-latency-ms 250 --max-participation-rate 0.2 --fee-per-fill-return 0.0003 --fill-model spread_fraction --fill-spread-fraction 0.5 --events-out ml_pipeline\artifacts\t30_execution_events.parquet --report-out ml_pipeline\artifacts\t30_execution_report.json --force-liquidate-end
```

## RUN_MODES Integration

For run-mode driven simulation:

1. Start system with historical/local replay as documented in `PROCESS_TOPOLOGY.md`.
2. Emit decision events via `live_inference_adapter` (live API or replay modes).
3. Run T30 with `--market-source api` to consume current dashboard options/depth snapshots.

Note: API mode is intended for runtime realism; parquet mode remains preferred for deterministic testing.
