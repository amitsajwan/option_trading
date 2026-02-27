# Feature Spec V1 (T04)

This document defines V1 engineered features built from `t03_canonical_panel.parquet`.
All features are computed per trading day in timestamp order and are designed to avoid lookahead leakage.

## Identity and Base Columns

- `timestamp`
- `trade_date`
- `source_day`
- futures base: `fut_open`, `fut_high`, `fut_low`, `fut_close`, `fut_oi`, `fut_volume`
- spot base: `spot_open`, `spot_high`, `spot_low`, `spot_close`
- options base around ATM neighborhood (`opt_m1_*`, `opt_0_*`, `opt_p1_*`)
- chain aggregates: `ce_oi_total`, `pe_oi_total`, `ce_volume_total`, `pe_volume_total`, `pcr_oi`

## Return and Trend Features

- `ret_1m`
- `ret_3m`
- `ret_5m`
- `ema_9`, `ema_21`, `ema_50`
- `ema_9_slope`, `ema_21_slope`, `ema_50_slope`

## Momentum and Volatility Features

- `rsi_14`
- `atr_14`
- `atr_ratio`
- `atr_percentile`

## Session Context Features

- `fut_vwap`
- `vwap_distance`
- `distance_from_day_high`
- `distance_from_day_low`
- `minute_of_day`
- `day_of_week`

## Basis and Option-Structure Features

- `basis` (futures minus spot)
- `basis_change_1m`
- `atm_call_return_1m`
- `atm_put_return_1m`
- `atm_oi_change_1m`
- `ce_pe_oi_diff`
- `ce_pe_volume_diff`

## Opening Range Features (15-minute window)

- `opening_range_high`
- `opening_range_low`
- `opening_range_ready`
- `opening_range_breakout_up`
- `opening_range_breakout_down`

## Leakage Guarding Rules

1. No negative shift operations are used.
2. Rolling/expanding features depend only on current and past bars.
3. Opening range breakout is active only when range is complete (`opening_range_ready=1`).
