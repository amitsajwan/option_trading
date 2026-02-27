# Label Spec V1 (T05)

This document defines trade-aligned CE/PE labels for supervised learning.

## Core Principle

Labels are computed on a fixed option symbol selected at decision time:

1. At minute `t`, choose ATM strike from row (`atm_strike`) and expiry (`expiry_code`).
2. Build symbol:
   - CE: `BANKNIFTY{expiry_code}{atm_strike}CE`
   - PE: `BANKNIFTY{expiry_code}{atm_strike}PE`
3. Use this same symbol for entry/exit and excursion calculations.

This avoids invalid labeling caused by shifting dynamic ATM columns across time.

## Timing Convention

Given decision timestamp `t` and horizon `H` (minutes):

- Entry timestamp: `t + 1 minute`
- Exit timestamp: `t + H minutes`

Entry/exit prices:

- `entry_price = option_open(entry_timestamp)`
- `exit_price = option_close(exit_timestamp)`

## Continuous Targets

For each side (`CE`, `PE`):

- `forward_return = (exit_price - entry_price) / entry_price`
- `mfe = (max(high in [entry_ts, exit_ts]) - entry_price) / entry_price`
- `mae = (min(low in [entry_ts, exit_ts]) - entry_price) / entry_price`

## Binary Label Rule

Default positive condition:

- `forward_return >= return_threshold`

Optional excursion gate (`use_excursion_gate=True`):

- `forward_return >= return_threshold`
- `mfe >= min_favorable_excursion`
- `mae >= -max_adverse_excursion`

If any required timestamp/price is missing, label is invalid (`label_valid=0`, label=`NaN`).

## Output Columns

For each side:

- `<side>_symbol`
- `<side>_entry_price`
- `<side>_exit_price`
- `<side>_forward_return`
- `<side>_mfe`
- `<side>_mae`
- `<side>_label_valid`
- `<side>_label`

Shared:

- `label_horizon_minutes`
- `label_return_threshold`
- `best_side_label` (`1`=CE, `-1`=PE, `0`=no-trade by both labels)
