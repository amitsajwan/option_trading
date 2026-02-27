# Model Card V1 (T13)

> Status: Historical (`v1`).
> Current active model documentation: `ml_pipeline/docs/model_card_v2_addendum.md`.

## 1. Model Overview

- Name: `BankNifty Intraday CE/PE Timing Baseline`
- Version: `v1` (T06-T12 pipeline)
- Model family: Gradient boosted trees (`XGBoost`) with median imputation
- Output:
  - `ce_prob`: probability CE trade qualifies
  - `pe_prob`: probability PE trade qualifies
  - decision logic uses thresholds from T08

## 2. Intended Use

- Intraday timing signal for BankNifty option buy decisions (paper/live adapters).
- Primary deployment mode in this phase: paper trading / decision simulation.
- Not intended for autonomous capital allocation without risk layer approval.

## 3. Data and Features

- Data source: local historical archive from `banknifty_fut`, `banknifty_spot`, `banknifty_options`.
- Canonical frequency: 1-minute.
- Core feature groups:
  - futures trend/volatility (`ret`, EMA slopes, RSI, ATR, VWAP distance)
  - intraday context (distance from day high/low, opening-range signals, minute-of-day)
  - options microstructure proxies (ATM/near strikes, OI/volume differentials)
- Feature count in latest run: `89`.

## 4. Label Definition

- Trade-aligned label from option symbol fixed at decision minute.
- Entry: `t+1` open, Exit: `t+H` close (default `H=3` minutes).
- Positive class when forward return crosses configured threshold (default `0.20%`).
- Separate labels for CE and PE.

## 5. Training and Validation

- Baseline split: chronological train/valid/test.
- Walk-forward validation: day-based rolling folds (default `3/1/1`, step `1`).
- Threshold optimization objective: expected net return per trade after cost.

## 6. Latest Observed Performance (Current Artifacts)

From `ml_pipeline/artifacts`:

- T06 validation ROC-AUC:
  - CE: `0.4830`
  - PE: `0.4483`
- T09 backtest summary:
  - trades: `176`
  - win rate: `0.5455`
  - net return sum: `1.1586`
  - max drawdown: `-0.3287`
- T10 default-cost best mode: `ce_only`

These values are dataset/config dependent and must be re-evaluated after retraining.

## 7. Risks and Limitations

- Model is short-horizon and sensitive to regime shifts.
- Feature/label quality depends on minute alignment and option symbol consistency.
- Fees/slippage assumptions are simplified via `cost_per_trade`.
- Paper-mode inference does not guarantee production execution parity.

## 8. Monitoring and Safeguards

- Drift checks (T12): feature PSI, prediction PSI, and action-share shift.
- Promotion gate: no deployment promotion without walk-forward + threshold + backtest review.
- Required artifacts for any promoted model:
  - train report
  - walk-forward report
  - threshold report
  - backtest report
  - drift baseline
