# Fixed-Strike Pricing Remediation RFC

## 1. Incident Summary

Observed behavior allowed option premium repricing drift when ATM moved after entry.  
This could misstate entry/mark/exit premiums if runtime priced from rolling ATM instead of held strike.

## 2. Root Cause

- Snapshot payload carried a strike ladder, but downstream runtime paths could still use ATM fields.
- Historical/backtest pipelines had mixed assumptions about structural required fields and snapshot versions.
- Documentation still referenced superseded ATM-path canonical outputs.

## 3. Remediation Scope

In scope:

- fixed-strike premium lookup for entry, mark-to-market, and exit paths
- strict fail-closed behavior when held strike quote is missing
- snapshot contract baseline on `version = "2.0"`
- strict nullable strike-level OHLC fields (no close-value substitution)
- canonical replay/eval/champion promotion under fixed-strike root
- documentation and deletion governance updates

Out of scope (deferred):

- changing strategy execution pricing semantics to intrabar OHLC-based exits

## 4. Contract Baseline

Snapshot contract baseline:

- `schema_name = "MarketSnapshot"`
- `version = "2.0"`
- top-level `strikes` array required
- `chain_aggregates.strike_count` required
- canonical emitted strike fields:
  - `strike, ce_ltp, pe_ltp, ce_oi, pe_oi, ce_volume, pe_volume, ce_iv, pe_iv, ce_open, ce_high, ce_low, pe_open, pe_high, pe_low`
- ATM block OHLC is strict feed-derived nullable values:
  - `atm_ce_open/high/low`, `atm_pe_open/high/low`
  - no fallback to `atm_*_close`

Runtime pricing contract:

- option premium resolution is exact-strike via `option_ltp(direction, strike)`
- no ATM/nearest substitution for held-strike pricing

Batch rebuild gate contract:

- rebuild selection defaults to structural field coverage:
  - `DEFAULT_REQUIRED_FIELDS = ["strike_count"]`
- version gate:
  - `DEFAULT_REQUIRED_SNAPSHOT_VERSION = "2.0"`

## 5. Canonical Layout (Locked)

Canonical fixed-strike root:

- `.run/canonical_eq_e2e_fixed_strike_20200101_20230928`

Snapshot storage layout (required):

- `.run/canonical_eq_e2e_fixed_strike_20200101_20230928/snapshots/year=*/data.parquet`

Required promotion outputs:

- `eval_frozen/evaluation_registry.csv`
- `champions/champion_registry.csv`
- `champions/champion_registry.json`

## 6. Validation Gates

Release-blocking window:

- `2020-01-01` to `2023-09-28`

Blocking validation:

- fixed-strike regression tests pass
- no strike-to-ATM repricing drift in priced trades
- schema comparison has no false drift after deep flatten fix
- replay eval + champion outputs generated in fixed-strike root

## 7. Known Deferred Item

Strategy OHLC usage status:

- snapshot and canonical contracts now expose strict nullable option OHLC
- runtime strategy pricing remains close-only exact-strike (`option_ltp`)
- intrabar OHLC-based stop/exit behavior remains deferred by design

## 8. Governance and Sign-Off

Required approvals before superseded-run deletion:

- architect
- trader
- data_scientist

Sign-off manifest:

- `docs/manifests/fixed_strike_signoff_YYYYMMDD.json`

Delete manifest:

- `docs/manifests/fixed_strike_delete_manifest_YYYYMMDD_HHMMSS.json`

## 9. Deletion Policy

Only after sign-off and validation:

- delete all `.run/canonical_*` roots except:
  - `.run/canonical_eq_e2e_fixed_strike_20200101_20230928`

ATM-path canonical outputs are superseded for promotion use.
