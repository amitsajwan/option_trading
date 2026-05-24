# Ticket E2-S8 — Parquet backfill for secondary OOS (2023-05 → 2023-07)

| Field | Value |
|-------|--------|
| **ID** | E2-S8 |
| **Type** | Data / ML pipeline |
| **Priority** | P2 |
| **Owner** | Ops/GCP + ML data |
| **Status** | Open |
| **Blocked story** | OOS secondary window `2023-05-01` → `2023-07-31` |

## Problem

Historical replay for secondary OOS returns **`emitted=0`** on `option-trading-runtime-01` because `snapshots_ml_flat_v2` has no partitions for May–Jul 2023.

`ops/gcp/check_parquet_coverage.py` should show gaps for `oos_secondary`.

## Acceptance criteria

- [ ] Parquet partitions exist for every trading day in 2023-05-01 … 2023-07-31 (or documented exceptions: holidays)
- [ ] `queue_replay.py 2023-05-01 2023-07-31` → `emitted > 0`
- [ ] Secondary OOS run analyzed; row added to `docs/SCRUM_BOARD_ML_ENTRY_DIRECTION.md` results log

## Suggested work

1. Confirm source: snapshot builder / ingestion archive for 2023 H1.
2. Backfill into `/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2/trade_date=YYYY-MM-DD/`.
3. Align with ML pipeline support window in `direction_s2_only_hpo_v2.json` if extending research range.
4. Re-run: `sudo bash ops/gcp/run_oos_validation_replay.sh oos_secondary`

## Verification

```bash
python ops/gcp/check_parquet_coverage.py
sudo bash ops/gcp/run_oos_validation_replay.sh oos_secondary
```

## Notes

- Primary (2024) and in-sample (2024 Aug–Oct) partitions exist; only **2023** secondary is blocked.
- Do not block Engine direction A/B on this ticket.
