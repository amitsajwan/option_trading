# Data Contract (Schema Freeze v1)

Version: `v1`
Status: `frozen for T01`
Date: `2026-02-21`

This document defines the canonical raw-input contract for the ML pipeline.
All downstream dataset building, feature engineering, and labeling depend on this contract.

## Archive Root

Expected root directory:

- `C:\Users\amits\Downloads\archive\banknifty_data` (current local archive)

Alternate root can be provided by env:

- `LOCAL_HISTORICAL_BASE`

## Directory Layout

Required subdirectories:

- `banknifty_fut\{YYYY}\{M}\banknifty_fut_{DD}_{MM}_{YYYY}.csv`
- `banknifty_options\{YYYY}\{M}\banknifty_options_{DD}_{MM}_{YYYY}.csv`
- `banknifty_spot\{YYYY}\{M}\banknifty_spot{DD}_{MM}_{YYYY}.csv`

Example date `2023-06-15`:

- `banknifty_fut\2023\6\banknifty_fut_15_06_2023.csv`
- `banknifty_options\2023\6\banknifty_options_15_06_2023.csv`
- `banknifty_spot\2023\6\banknifty_spot15_06_2023.csv`

## CSV Schemas

### Futures (`banknifty_fut`)

Required columns:

- `date`
- `time`
- `symbol`
- `open`
- `high`
- `low`
- `close`
- `oi`
- `volume`

### Options (`banknifty_options`)

Required columns:

- `date`
- `time`
- `symbol`
- `open`
- `high`
- `low`
- `close`
- `oi`
- `volume`

Expected symbol pattern (warning-level check):

- `BANKNIFTY<expiry><strike><CE|PE>`
- Example: `BANKNIFTY15JUN2337500PE`

### Spot (`banknifty_spot`)

Required columns:

- `date`
- `time`
- `symbol`
- `open`
- `high`
- `low`
- `close`

## Data Type Rules

Across all datasets:

- `date` must parse as `%Y-%m-%d`
- `time` must parse as `%H:%M:%S` or `%H:%M`
- `open`, `high`, `low`, `close` must be numeric

Additional rules:

- `oi` numeric for futures/options
- `volume` numeric for futures/options

## Structural Invariants

Futures/Spot:

- One row per minute expected for the trading session
- Duplicate timestamp rows are contract violations

Options:

- Multiple rows per minute expected (different option symbols)
- Timestamp + symbol should be unique

## Validation Outputs

Schema validator produces:

- Per-file row counts
- Error list (contract violations)
- Warning list (non-blocking anomalies)
- Aggregate summary (`pass_count`, `fail_count`)

## Representative Day Set for Contract Verification

Validation baseline day set:

- `2020-01-03`
- `2021-06-15`
- `2022-12-01`
- `2023-06-15`
- `2024-10-10`

These dates intentionally span multiple years and expiry formats used in archive files.
