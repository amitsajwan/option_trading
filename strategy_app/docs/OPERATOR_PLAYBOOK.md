# Operator Playbook

As-of: `2026-04-27`

## Core Rules

1. Trust `run_id`-scoped views over date-only views.
2. Read `Range Trades` and summary before individual votes.
3. Read `exit_reason` together with `exit_mechanism`.
4. Treat stale-data alerts as monitoring degradation, not automatic strategy failure.

## Historical Replay Workflow

### Step 1: Confirm scope

Check:

- `Latest Run ID`
- Selected date range
- Run note on the replay page

If the run ID is wrong, every downstream interpretation is wrong.

### Step 2: Confirm replay health

Check:

- Replay status
- Emitted event count
- Collection counts
- Whether the page is bound to the intended `run_id`

### Step 3: Read the product in this order

1. `Range Trades`
2. `Latest Closed Trade`
3. `Recent Signals`
4. `Recent Votes`
5. `Decision Diagnostics`

Reading in this order keeps realized behavior ahead of candidate noise.

## How to Read Exits

### `TRAILING_STOP`

Use `exit_mechanism` to identify the actual trail owner:

- `GENERIC_TRAIL`
- `ORB_TRAIL`
- `OI_TRAIL`

### `REGIME_SHIFT`

Possible causes:

- Thesis cracked
- Exit logic too sensitive
- Regime confirmation too weak

### `TIME_STOP`

Trade did not close naturally within the allowed hold window.

### `STOP_LOSS`

Hard protection activated. Inspect:

- Stop placement
- Lot sizing
- Clustering by strategy and regime

## How to Read Alerts

### `data_stale`

Monitoring freshness is degraded. Do not trust fresh operational inference until stream health is confirmed.

### `ml_pure_monitoring_unavailable`

ML monitoring inputs are incomplete. Treat as a monitoring gap, not a model failure.

### `risk_halt` / `risk_pause`

Risk layer is controlling participation. Inspect drawdown and risk limits before any override decision.

## Minimum Clean-Run Checklist

- Correct `run_id`
- Correct date range
- Non-zero emitted events
- Summary, trades, and session are in agreement
- No mixed-profile leakage
- Readable `exit_mechanism` on each closed trade
- Explainable strategy and regime contribution

If any item fails, the run is not clean enough for decision-making.

## ml_pure Runtime Checks

Before switching to a new model bundle, confirm:

- `publish_decision.decision == PUBLISH` or `publish_status == published` in the run report
- `published_paths.model_package` and `published_paths.threshold_report` both exist
- Guard file (for `capped_live`): `approved_for_runtime=true`, `offline_strict_positive_passed=true`, `paper_days_observed >= 10`, `shadow_days_observed >= 10`

After switching:

- Confirm `ml_pure_model_package` and `ml_pure_threshold_report` in `runtime_config.json` match the intended paths
- Confirm `strategy_profile_id = ml_pure_staged_v1` in live signal records
- Watch `decision_reason_code` distribution for unexpected spikes in `feature_stale`, `feature_incomplete`, or `risk_halt`

## Key Decision Reason Codes (ml_pure)

| Code | Meaning |
|---|---|
| `entry_below_threshold` | Stage 1 gate rejected |
| `direction_below_threshold` | Stage 2 neither CE nor PE cleared threshold |
| `direction_low_edge_conflict` | Both CE and PE cleared but margin too small |
| `recipe_below_threshold` | Stage 3 top recipe below threshold |
| `recipe_low_margin` | Stage 3 top recipe margin too small |
| `feature_stale` | Snapshot older than `max_feature_age_sec` |
| `feature_incomplete` | Too many NaN required features |
| `risk_halt` | Session risk manager halted |
| `risk_pause` | Session risk manager paused |
| `regime_avoid` | Regime classified as AVOID |
| `regime_sideways` | Regime classified as SIDEWAYS (blocked by gate) |
| `regime_expiry` | Expiry regime blocked when `block_expiry=true` |
| `liquidity_gate_block` | OI or volume below minimums |
