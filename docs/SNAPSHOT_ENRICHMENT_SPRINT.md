# Snapshot Enrichment Sprint — Intraday Velocity Features
> **Status**: Planned — ready for independent implementation  
> **Created**: 2026-04-12  
> **Estimated effort**: 5–7 days  
> **Team**: Data Engineering (works independently on dedicated VM)  
> **Depends on**: Nothing — self-contained, does not touch ML training pipeline  
> **ML team picks up**: After enrichment is complete and validated

---

## Why This Exists

Our ML model currently uses only the **11:30 snapshot** for Stage 2 direction prediction.  
A discretionary trader reads the same data but as a **movie** — rate of change, velocity, acceleration.  
We have been reading a single frame and calling it information.

```
What trader sees at 11:30:
  "OI has been BUILDING on CE side for 90 minutes (up 80k contracts since open)
   PCR has been FALLING from 1.4 to 0.9 (put covering, call adding)
   Price has ACCELERATED upward in last 30 minutes"
  → Strong directional signal: bullish, CE premium expanding

What our model sees at 11:30:
  atm_oi_ratio = 1.35
  pcr = 0.9
  ema_21_slope = 0.4
  → No context, no direction, no velocity
```

We collect 15-minute snapshots from 09:15 to 15:30 every trading day.  
We use only one of them for training. The rest are discarded.  
This sprint enriches the 11:30 snapshot with derived features from the morning session.

---

## Scope

```
IN SCOPE:
  ✓ Add velocity/delta features to snapshots_ml_flat parquet dataset
  ✓ Backfill for 2020–2024 (all historical data)
  ✓ Live pipeline: compute same features in real-time from 10:00–11:30 session
  ✓ Contract validation for new columns
  ✓ Tests

OUT OF SCOPE:
  ✗ ML model training (ML team picks up after enrichment)
  ✗ Feature set registration in feature_sets.py (ML team)
  ✗ Live oracle rolling stats (separate workstream, Track C)
  ✗ Changing any existing column (additive only)
```

---

## Infrastructure

### New VM (dedicated to this sprint)

```
Name:      option-trading-snapshot-enrichment-01
Type:      e2-standard-4  (4 vCPUs, 16 GB RAM)
Zone:      asia-south1-b
Project:   amittrading
Disk:      50 GB (data lives on shared path, not local)
Cost:      ~$0.13/hr, ~$3/day
Lifetime:  shut down after sprint, do not leave running

Why separate VM:
  - Does not interfere with training campaign running on build-01
  - Backfill job is CPU-heavy (4 years × ~250 days × 20 snapshots/day)
  - Can be deleted after work is complete
```

### Shared data path (same as training VM)
```
/home/savitasajwan03/option_trading/.data/ml_pipeline/parquet_data/
  snapshots/                  ← source: all 15-min snapshots (raw)
  snapshots_ml_flat/          ← target: enriched ml_flat (add new columns here)
```

---

## New Features — Full Specification

All features are computed from snapshots **at or before 11:30** on the same `trade_date`.  
All are **additive** — no existing columns are changed.  
Naming convention: `vel_` prefix for velocity/delta features, `ctx_am_` for morning session context.

---

### Group 1 — OI Velocity (highest priority)

| Column | Formula | What it captures |
|--------|---------|-----------------|
| `vel_ce_oi_delta_open` | `ce_oi_1130 - ce_oi_1000` | Total CE OI build from open to midday |
| `vel_pe_oi_delta_open` | `pe_oi_1130 - pe_oi_1000` | Total PE OI build from open to midday |
| `vel_ce_oi_delta_30m` | `ce_oi_1130 - ce_oi_1100` | CE OI build in last 30 min before midday |
| `vel_pe_oi_delta_30m` | `pe_oi_1130 - pe_oi_1100` | PE OI build in last 30 min before midday |
| `vel_oi_ratio_delta_open` | `atm_oi_ratio_1130 - atm_oi_ratio_1000` | Shift in CE/PE balance since open |
| `vel_oi_ratio_delta_30m` | `atm_oi_ratio_1130 - atm_oi_ratio_1100` | Shift in CE/PE balance last 30 min |
| `vel_ce_oi_build_rate` | `vel_ce_oi_delta_open / minutes_elapsed` | Normalised build speed (per minute) |
| `vel_pe_oi_build_rate` | `vel_pe_oi_delta_open / minutes_elapsed` | Normalised build speed (per minute) |

**Signal interpretation**:
- `vel_ce_oi_delta_open > 0` + large → fresh CE shorts being added → bearish pressure on CE buyers
- `vel_ce_oi_delta_open < 0` → CE shorts being covered → potential squeeze
- `vel_oi_ratio_delta_open` shifting CE-ward → directional positioning building

---

### Group 2 — PCR Velocity

| Column | Formula | What it captures |
|--------|---------|-----------------|
| `vel_pcr_delta_open` | `pcr_1130 - pcr_1000` | PCR trend from open to midday |
| `vel_pcr_delta_30m` | `pcr_1130 - pcr_1100` | PCR change in last 30 min |
| `vel_pcr_acceleration` | `pcr_change_15m_now - pcr_change_15m_prev` | Is PCR moving faster or slower? |
| `vel_pcr_trend_direction` | `sign(slope of pcr over 10:00–11:30)` | -1 / 0 / +1 |

Note: `pcr_change_5m` and `pcr_change_15m` already exist. These extend to longer horizons.

---

### Group 3 — Price Velocity

| Column | Formula | What it captures |
|--------|---------|-----------------|
| `vel_price_delta_open` | `px_fut_close_1130 - px_fut_open_1000` | Price move from open to midday |
| `vel_price_delta_30m` | `px_fut_close_1130 - px_fut_close_1100` | Price move in last 30 min |
| `vel_price_delta_60m` | `px_fut_close_1130 - px_fut_close_1030` | Price move in last 60 min |
| `vel_price_acceleration` | `vel_price_delta_30m - price_delta_prev_30m` | Is momentum accelerating? |
| `ctx_am_range_high` | `max(px_fut_high) from 10:00–11:30` | Morning session high |
| `ctx_am_range_low` | `min(px_fut_low) from 10:00–11:30` | Morning session low |
| `ctx_am_range_size` | `ctx_am_range_high - ctx_am_range_low` | Morning range width |
| `ctx_am_price_position` | `(price_1130 - range_low) / range_size` | Where is price in morning range? 0=bottom, 1=top |
| `ctx_am_gap_from_yday` | `px_fut_open_1000 - px_fut_close_yday` | Opening gap vs yesterday close |
| `ctx_am_gap_filled` | `1 if gap direction reversed by 11:30 else 0` | Was gap filled? |

---

### Group 4 — IV Velocity

| Column | Formula | What it captures |
|--------|---------|-----------------|
| `vel_atm_ce_iv_delta_open` | `atm_ce_iv_1130 - atm_ce_iv_1000` | IV build/compression since open |
| `vel_atm_pe_iv_delta_open` | `atm_pe_iv_1130 - atm_pe_iv_1000` | IV build/compression since open |
| `vel_iv_skew_delta_open` | `iv_skew_1130 - iv_skew_1000` | Skew shift (CE IV premium expanding?) |
| `vel_iv_compression_rate` | `vel_atm_ce_iv_delta_open / minutes_elapsed` | How fast IV is moving |

**Signal interpretation**:
- IV compressing fast → expected move being priced out → good for premium sellers
- IV expanding fast → event risk or large order flow entering

---

### Group 5 — Volume Velocity

| Column | Formula | What it captures |
|--------|---------|-----------------|
| `vel_ce_vol_delta_30m` | `ce_volume_1130 - ce_volume_1100` | CE option volume spike in last 30 min |
| `vel_pe_vol_delta_30m` | `pe_volume_1130 - pe_volume_1100` | PE option volume spike in last 30 min |
| `vel_options_vol_acceleration` | `vol_last_30m / vol_prev_30m` | Is options activity accelerating? |
| `ctx_am_vol_vs_yday` | `total_vol_by_1130 / yday_total_vol_by_1130` | Volume vs same time yesterday |

---

### Group 6 — Morning Session Summary (boolean/categorical)

| Column | Type | What it captures |
|--------|------|-----------------|
| `ctx_am_trend` | int (-1/0/1) | Dominant trend 10:00–11:30: -1=down, 0=flat, 1=up |
| `ctx_am_trend_strength` | float | Absolute slope magnitude (0=flat, 1=strong) |
| `ctx_am_reversal` | int (0/1) | Did price reverse direction in last 30 min? |
| `ctx_am_oi_direction` | int (-1/0/1) | Net OI delta direction: CE building=1, PE building=-1, flat=0 |
| `ctx_am_vwap_side` | int (-1/0/1) | Is price above (1) or below (-1) VWAP at 11:30? |
| `ctx_am_breakout_confirmed` | int (0/1) | Opening range breakout confirmed by 11:30? |

---

## Implementation Plan

### Day 1 — Setup + Data Access Layer

```
1. Spin up enrichment VM (e2-standard-4)
2. Clone repo, install dependencies
3. Verify access to parquet data paths
4. Write MorningSessionLoader class:
   - Input:  trade_date, parquet_root
   - Output: DataFrame of all snapshots from 10:00–11:30 for that date
   - Key:    sorted by timestamp, validated for completeness
   - Handle: missing snapshots (market holidays, early close)
```

**File to create**: `snapshot_app/historical/morning_session.py`

```python
class MorningSessionLoader:
    """
    Loads all snapshots from 10:00 to 11:30 for a given trade_date.
    Used by the enrichment pipeline to compute velocity features.
    """
    def load(self, trade_date: str, parquet_root: Path) -> pd.DataFrame:
        """Returns sorted DataFrame of snapshots, or empty DataFrame if date unavailable."""
        ...

    def load_range(self, start_date: str, end_date: str, parquet_root: Path) -> Dict[str, pd.DataFrame]:
        """Returns {trade_date: DataFrame} for all dates in range."""
        ...
```

---

### Day 2 — Velocity Feature Computation

```
Write VelocityFeatureBuilder:
  - Input:  DataFrame of morning session snapshots for one trade_date
  - Output: Dict of {feature_name: value} for all 30+ new features
  - Design: pure function, no side effects, fully testable
  - Handle: NaN for dates with < 3 morning snapshots available
```

**File to create**: `snapshot_app/core/velocity_features.py`

```python
def compute_velocity_features(
    morning_df: pd.DataFrame,
    *,
    midday_snapshot: pd.Series,
    prev_day_close: Optional[float] = None,
) -> Dict[str, float]:
    """
    Compute all velocity/delta features from morning session data.

    Args:
        morning_df:       All snapshots 10:00–11:30, sorted by timestamp
        midday_snapshot:  The 11:30 snapshot row (single row as Series)
        prev_day_close:   Previous day's closing price (for gap features)

    Returns:
        Dict of {column_name: float_value} — all NaN if insufficient data
    """
    ...
```

**Key implementation notes**:
- Always use `.shift(1)` or explicit prior-snapshot references — never use 11:30 to compute its own velocity
- `minutes_elapsed = (timestamp_1130 - timestamp_1000).total_seconds() / 60` — use actual timestamps, not assumed
- If fewer than 3 morning snapshots: return all NaN (do not interpolate or guess)
- `ctx_am_price_position`: clip to [0, 1] if range_size < 10 points (flat days)

---

### Day 3 — Schema Extension + Contract Update

```
1. Add all new columns to snapshot_ml_flat_contract.py:
   - REQUIRED_COLUMNS list (append new column names)
   - FIELD_TYPES dict (all new columns are "number" except ctx_am_* flags which are "integer")

2. Bump SCHEMA_VERSION in market_snapshot_contract.py
   (follow existing versioning pattern)

3. Update feature_groups.json to add new groups:
   "velocity_oi": [...],
   "velocity_pcr": [...],
   "velocity_price": [...],
   "velocity_iv": [...],
   "velocity_volume": [...],
   "morning_context": [...]

4. Update validation_rules.yaml:
   - Range checks for delta features (can be negative — no min_value=0)
   - ctx_am_trend: allowed values [-1, 0, 1]
   - ctx_am_price_position: range [0, 1]
```

---

### Day 4 — Backfill Pipeline

```
Write EnrichmentBatchRunner:
  - Iterates over all trade_dates in 2020–2024
  - For each date:
    a. Load morning session snapshots
    b. Load existing ml_flat row for that date
    c. Compute velocity features
    d. Append new columns to the ml_flat row
    e. Write back to parquet (year-partitioned)
  - Resume-safe: skip dates already processed
  - Logging: progress every 50 dates, errors logged and skipped
```

**File to create**: `snapshot_app/historical/enrichment_runner.py`

**CLI entrypoint**:
```bash
python -m snapshot_app.historical.enrichment_runner \
    --parquet-root /path/to/.data/ml_pipeline/parquet_data \
    --start-date 2020-01-01 \
    --end-date 2024-12-31 \
    --output-dataset snapshots_ml_flat_v2 \
    --dry-run  # validate first 10 dates without writing
```

**Important**: write to `snapshots_ml_flat_v2` initially, not `snapshots_ml_flat`.  
ML team will validate and rename after QA.

---

### Day 5 — Tests + QA

**Unit tests** (`snapshot_app/tests/test_velocity_features.py`):
```
test_velocity_features_normal_day           — standard inputs, verify all columns present
test_velocity_features_missing_snapshots    — only 2 morning snapshots → all NaN
test_velocity_features_flat_market          — price flat all morning → trend=0, range_size small
test_velocity_features_gap_filled           — gap up at open, price below open by 11:30 → gap_filled=1
test_velocity_features_oi_building_ce       — CE OI increasing → vel_ce_oi_delta_open > 0
test_velocity_features_no_prev_day_close    — prev_day_close=None → ctx_am_gap features = NaN
```

**Integration test** (`snapshot_app/tests/test_enrichment_runner.py`):
```
test_enrichment_runner_dry_run              — 5 dates, verify schema correct, no write
test_enrichment_runner_idempotent           — run twice on same dates, same output
test_enrichment_runner_resume               — simulate partial run, verify resume skips done dates
```

**QA checklist** (manual, on 10 sampled dates):
```
□ vel_ce_oi_delta_open is not NaN for normal trading days
□ ctx_am_price_position is always in [0, 1]
□ ctx_am_trend matches visual inspection of price chart
□ Velocity features for 2020-03-20 (COVID crash) are extreme but not NaN
□ Schema validation passes on enriched parquet
□ ML flat row count unchanged (additive only — no rows dropped)
```

---

### Day 6-7 — Live Pipeline (real-time enrichment)

```
Modify: snapshot_app/core/live_ml_flat.py
  When the 11:30 snapshot is being published to ml_flat:
  1. Load all snapshots from the same day since 10:00 from in-memory buffer
  2. Call compute_velocity_features(morning_df, midday_snapshot)
  3. Merge result into the ml_flat row before publishing

In-memory buffer exists already (snapshot_batch.py line ~40: LOOKBACK_DAYS=30)
This is the same buffer used for IV history and chain history.
Just needs a morning_session_buffer alongside it.
```

---

## Data Flow Diagram

```
RAW SNAPSHOTS (15-min intervals, all day)
  snapshots/year=YYYY/data.parquet
         │
         │  10:00–11:30 snapshots extracted per trade_date
         ▼
  MorningSessionLoader
         │
         │  morning_df (6–10 rows per date)
         ▼
  VelocityFeatureBuilder
         │
         │  ~30 new feature values
         ▼
  Merged into 11:30 ml_flat row
         │
         ├── BACKFILL PATH ──►  snapshots_ml_flat_v2/year=YYYY/data.parquet
         │
         └── LIVE PATH ──────►  Kafka/event stream → ml_pure lane → strategy_app
```

---

## What ML Team Gets After This Sprint

A new parquet dataset `snapshots_ml_flat_v2` with all existing columns intact plus ~30 new velocity columns.

ML team then:
1. Register new feature set `fo_midday_velocity_v1` in `feature_sets.py`
2. Run one campaign lane with the new feature set
3. Expected: Stage 2 ROC improvement from ~0.54 → 0.58–0.64

**ML team does NOT need to touch snapshot_app at all.**

---

## Handoff Checklist (Data Engineering → ML)

```
□ snapshots_ml_flat_v2 written for 2020–2024 (full backfill)
□ Schema validation passes for all years
□ New column list documented (see feature spec above)
□ QA checklist completed and signed off
□ Live pipeline tested on paper trading day
□ enrichment_runner.py committed and pushed
□ velocity_features.py committed and pushed
□ Updated snapshot_ml_flat_contract.py committed and pushed
□ Tests passing (unit + integration)
□ ML team notified with dataset path and new column names
```

---

## Risk / Edge Cases

| Risk | Mitigation |
|------|-----------|
| Market holiday — no morning snapshots | Return all NaN, training pipeline already handles NaN via `max_missing_rate=0.35` |
| Early close day | Use whatever snapshots exist up to 11:30 |
| 2020 COVID period — extreme values | Do not clip. Let the model see extreme regime. Add to QA checklist. |
| Schema mismatch breaks existing ML training | Write to `snapshots_ml_flat_v2`, not existing dataset. ML team renames after validation. |
| Live buffer has < 3 morning snapshots at 11:30 | Return NaN for velocity features, model falls back to level features only |
| Backfill takes too long on 4-core VM | Parallelise by year (4 years × 1 core each) using Python multiprocessing |

---

## Estimated GCP Cost

```
e2-standard-4 VM:   $0.13/hr
Sprint duration:    7 days
Daily runtime:      8 hrs/day (shut down at night)

Total VM cost:      7 × 8 × $0.13  =  ~$7.30

Backfill compute:
  4 years × 250 days × 20 snapshots = 20,000 date-level joins
  Estimated: 2–3 hrs on 4-core VM

Total estimated cost:  < $10
```

---

## Quick Reference — Files to Create / Modify

| File | Action | Owner |
|------|--------|-------|
| `snapshot_app/core/velocity_features.py` | CREATE | Data Engineering |
| `snapshot_app/historical/morning_session.py` | CREATE | Data Engineering |
| `snapshot_app/historical/enrichment_runner.py` | CREATE | Data Engineering |
| `snapshot_app/core/snapshot_ml_flat_contract.py` | MODIFY (add columns) | Data Engineering |
| `snapshot_app/core/market_snapshot_contract.py` | MODIFY (bump version) | Data Engineering |
| `snapshot_app/contracts/snapshot_ml_flat/feature_groups.json` | MODIFY | Data Engineering |
| `snapshot_app/contracts/snapshot_ml_flat/validation_rules.yaml` | MODIFY | Data Engineering |
| `snapshot_app/core/live_ml_flat.py` | MODIFY (live enrichment) | Data Engineering |
| `snapshot_app/tests/test_velocity_features.py` | CREATE | Data Engineering |
| `snapshot_app/tests/test_enrichment_runner.py` | CREATE | Data Engineering |
| `ml_pipeline_2/src/.../catalog/feature_sets.py` | MODIFY (add fo_midday_velocity_v1) | **ML team (after handoff)** |
