# Strategy App Implementation Status

## Implemented

- Deterministic strategy engine with:
  - regime classification
  - regime-based strategy routing
  - position tracking
  - portfolio risk controls
  - vote, signal, and position JSONL logging
- Redis publishing for:
  - strategy votes
  - trade signals
  - strategy position lifecycle events
- Mongo persistence for:
  - `strategy_votes`
  - `trade_signals`
  - `strategy_positions`
- Compose and local launcher wiring for:
  - `strategy_app`
  - `strategy_persistence_app`
- Health checks for:
  - `strategy_app`
  - `strategy_persistence_app`
- Regime-aware telemetry fields persisted for backtest slicing:
  - `regime`
  - `regime_conf`

## Remaining

- Add a lightweight dashboard or API endpoint for live strategy monitoring from Mongo.
- Add tests for:
  - regime classification
  - strategy router selection
  - deterministic engine regime gating
  - Mongo strategy event persistence
  - strategy evaluation trade reconstruction
- Add richer evaluation analytics:
  - per-day equity curve
  - drawdown
  - streaks
  - export-friendly CSV/Parquet outputs

## Current Recommended Next Step

- Use `python -m persistence_app.strategy_evaluation` to inspect trade performance by strategy and regime.
- Backfill snapshot parquet days missing `vwap` / `ema_*`, then replay historical snapshots through `strategy_app`.
