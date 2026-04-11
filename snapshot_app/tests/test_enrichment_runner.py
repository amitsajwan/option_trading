"""Integration tests for snapshot_app.historical.enrichment_runner."""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from snapshot_app.core.velocity_features import VELOCITY_COLUMNS
from snapshot_app.historical.enrichment_runner import (
    DEFAULT_OUTPUT_DATASET,
    SCHEMA_VERSION_V2,
    EnrichmentBatchRunner,
    EnrichmentResult,
    RunSummary,
    _attach_velocity_to_day,
    _find_midday_row,
    _process_one_date,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_full_day_df(trade_date: str = "2023-06-15") -> pd.DataFrame:
    """Build a synthetic full-day ml_flat DataFrame with one row per 15 min 9:15–15:30."""
    rows: List[Dict[str, Any]] = []
    for hour in range(9, 16):
        start_min = 15 if hour == 9 else 0
        end_min = 30 if hour == 15 else 60
        for minute in range(start_min, end_min, 15):
            rows.append({
                "trade_date": trade_date,
                "timestamp": pd.Timestamp(f"{trade_date} {hour:02d}:{minute:02d}:00"),
                "snapshot_id": f"{trade_date}-{hour:02d}{minute:02d}",
                "schema_name": "SnapshotMLFlat",
                "schema_version": "3.0",
                "instrument": "BANKNIFTY-I",
                "build_source": "historical",
                "build_run_id": "test-run",
                "year": int(trade_date[:4]),
                "px_fut_open": 44000.0,
                "px_fut_high": 44050.0,
                "px_fut_low": 43970.0,
                "px_fut_close": 44020.0 + hour * 10.0,
                "opt_flow_ce_oi_total": 500_000.0 + hour * 10_000.0,
                "opt_flow_pe_oi_total": 400_000.0 + hour * 5_000.0,
                "opt_flow_pcr_oi": 1.2 - hour * 0.01,
                "atm_oi_ratio": 1.1 + hour * 0.02,
                "opt_flow_ce_volume_total": 50_000.0,
                "opt_flow_pe_volume_total": 40_000.0,
                "vwap_fut": 44100.0,
                "pcr_change_15m": -0.05,
                "ctx_opening_range_breakout_up": 0,
                "ctx_opening_range_breakout_down": 0,
                "opt_flow_rows": 100,
                "opt_flow_atm_strike": 44000,
                "opt_flow_pe_oi_total": 400_000.0,
                "opt_flow_ce_pe_oi_diff": 100_000.0,
                "opt_flow_ce_pe_volume_diff": 10_000.0,
                "opt_flow_options_volume_total": 90_000.0,
                "opt_flow_rel_volume_20": 1.1,
                "time_minute_of_day": hour * 60 + minute,
                "time_day_of_week": 3,
                "time_minute_index": (hour - 9) * 4 + minute // 15,
            })
    return pd.DataFrame(rows)


# ── test: _attach_velocity_to_day ─────────────────────────────────────────────

def test_attach_velocity_to_day_populates_midday_only() -> None:
    """Velocity values populated only on 11:30 row; all other rows remain NaN."""
    full_df = _make_full_day_df()
    velocity = {col: 42.0 for col in VELOCITY_COLUMNS}

    enriched = _attach_velocity_to_day(full_df, velocity)

    ts = pd.to_datetime(enriched["timestamp"])
    midday_mask = (ts.dt.hour == 11) & (ts.dt.minute == 30)
    assert midday_mask.any(), "expected at least one 11:30 row"

    # 11:30 row: velocity cols populated
    midday_row = enriched[midday_mask].iloc[0]
    for col in VELOCITY_COLUMNS:
        assert col in enriched.columns, f"velocity column {col} missing"
        val = midday_row[col]
        assert not math.isnan(float(val)) if pd.notna(val) else True

    # non-11:30 rows: velocity cols are NaN
    non_midday = enriched[~midday_mask]
    for col in VELOCITY_COLUMNS:
        non_nan = non_midday[col].notna().sum()
        assert non_nan == 0, f"column {col} should be NaN on non-11:30 rows, found {non_nan} non-NaN"


def test_attach_velocity_to_day_schema_version_bumped() -> None:
    """Only the 11:30 row gets schema_version='4.0'; others stay '3.0'."""
    full_df = _make_full_day_df()
    velocity = {col: 1.0 for col in VELOCITY_COLUMNS}

    enriched = _attach_velocity_to_day(full_df, velocity, schema_version=SCHEMA_VERSION_V2)

    ts = pd.to_datetime(enriched["timestamp"])
    midday_mask = (ts.dt.hour == 11) & (ts.dt.minute == 30)

    midday_versions = enriched.loc[midday_mask, "schema_version"].unique()
    assert SCHEMA_VERSION_V2 in midday_versions, "11:30 row should have schema_version=4.0"

    non_midday_versions = enriched.loc[~midday_mask, "schema_version"].unique()
    assert SCHEMA_VERSION_V2 not in non_midday_versions, "non-11:30 rows should stay at 3.0"


# ── test: dry-run produces no files ───────────────────────────────────────────

def test_enrichment_runner_dry_run_no_writes() -> None:
    """Dry-run mode: enrichment completes but zero parquet files are written."""
    full_df = _make_full_day_df("2023-06-15")
    dates = ["2023-06-15"]

    with tempfile.TemporaryDirectory() as tmpdir:
        parquet_root = Path(tmpdir)
        output_root = parquet_root / DEFAULT_OUTPUT_DATASET

        # patch the internal loader so no real parquet is needed
        with (
            patch(
                "snapshot_app.historical.enrichment_runner._enumerate_source_dates",
                return_value=dates,
            ),
            patch(
                "snapshot_app.historical.enrichment_runner._load_full_day_ml_flat",
                return_value=full_df,
            ),
            patch(
                "snapshot_app.historical.enrichment_runner._get_already_processed_dates",
                return_value=set(),
            ),
            patch(
                "snapshot_app.historical.enrichment_runner._get_prev_day_close",
                return_value=43_900.0,
            ),
            patch(
                "snapshot_app.historical.morning_session.MorningSessionLoader.load",
                return_value=full_df[
                    pd.to_datetime(full_df["timestamp"]).dt.hour.between(10, 11)
                ].reset_index(drop=True),
            ),
        ):
            runner = EnrichmentBatchRunner(
                parquet_root=parquet_root,
                start_date="2023-06-15",
                end_date="2023-06-15",
                dry_run=True,
            )
            summary = runner.run()

        # dry-run: no files written
        parquet_files = list(output_root.rglob("*.parquet"))
        assert len(parquet_files) == 0, f"dry-run should not write files, found: {parquet_files}"

        assert summary.enriched == 1, f"expected 1 enriched, got {summary.enriched}"
        assert summary.errors == 0


# ── test: idempotency ──────────────────────────────────────────────────────────

def test_enrichment_runner_idempotent() -> None:
    """Running twice on the same date produces identical output (resume skips re-processing)."""
    full_df = _make_full_day_df("2023-06-15")
    dates = ["2023-06-15"]

    with tempfile.TemporaryDirectory() as tmpdir:
        parquet_root = Path(tmpdir)

        common_patches = dict(
            enumerate=patch(
                "snapshot_app.historical.enrichment_runner._enumerate_source_dates",
                return_value=dates,
            ),
            full_day=patch(
                "snapshot_app.historical.enrichment_runner._load_full_day_ml_flat",
                return_value=full_df,
            ),
            already=patch(
                "snapshot_app.historical.enrichment_runner._get_already_processed_dates",
                return_value=set(),
            ),
            prev=patch(
                "snapshot_app.historical.enrichment_runner._get_prev_day_close",
                return_value=43_900.0,
            ),
            morning=patch(
                "snapshot_app.historical.morning_session.MorningSessionLoader.load",
                return_value=full_df[
                    pd.to_datetime(full_df["timestamp"]).dt.hour.between(10, 11)
                ].reset_index(drop=True),
            ),
        )

        # first run
        with (
            common_patches["enumerate"],
            common_patches["full_day"],
            common_patches["already"],
            common_patches["prev"],
            common_patches["morning"],
        ):
            runner = EnrichmentBatchRunner(
                parquet_root=parquet_root,
                start_date="2023-06-15",
                end_date="2023-06-15",
                dry_run=False,
            )
            summary1 = runner.run()

        assert summary1.enriched == 1

        # second run — simulate that first run's output exists
        with (
            patch(
                "snapshot_app.historical.enrichment_runner._enumerate_source_dates",
                return_value=dates,
            ),
            patch(
                "snapshot_app.historical.enrichment_runner._load_full_day_ml_flat",
                return_value=full_df,
            ),
            patch(
                "snapshot_app.historical.enrichment_runner._get_already_processed_dates",
                return_value={"2023-06-15"},   # ← already processed
            ),
            patch(
                "snapshot_app.historical.enrichment_runner._get_prev_day_close",
                return_value=43_900.0,
            ),
            patch(
                "snapshot_app.historical.morning_session.MorningSessionLoader.load",
                return_value=full_df[
                    pd.to_datetime(full_df["timestamp"]).dt.hour.between(10, 11)
                ].reset_index(drop=True),
            ),
        ):
            runner2 = EnrichmentBatchRunner(
                parquet_root=parquet_root,
                start_date="2023-06-15",
                end_date="2023-06-15",
                dry_run=False,
            )
            summary2 = runner2.run()

        # second run skips already-processed date
        assert summary2.enriched == 0
        assert summary2.skipped >= 1


# ── test: no 11:30 row → status no_midday_row ─────────────────────────────────

def test_enrichment_result_no_midday_row() -> None:
    """Date with no 11:30 row in ml_flat → status = 'no_midday_row'."""
    full_df = _make_full_day_df("2023-06-16")
    # remove 11:30 row
    ts = pd.to_datetime(full_df["timestamp"])
    full_df = full_df[~((ts.dt.hour == 11) & (ts.dt.minute == 30))].reset_index(drop=True)
    assert not any((pd.to_datetime(full_df["timestamp"]).dt.hour == 11) & (pd.to_datetime(full_df["timestamp"]).dt.minute == 30))

    with tempfile.TemporaryDirectory() as tmpdir:
        parquet_root = Path(tmpdir)

        with (
            patch(
                "snapshot_app.historical.enrichment_runner._load_full_day_ml_flat",
                return_value=full_df,
            ),
            patch(
                "snapshot_app.historical.enrichment_runner._get_prev_day_close",
                return_value=None,
            ),
            patch(
                "snapshot_app.historical.morning_session.MorningSessionLoader.load",
                return_value=pd.DataFrame(),
            ),
        ):
            result = _process_one_date(
                "2023-06-16",
                parquet_root=parquet_root,
                output_root=parquet_root / DEFAULT_OUTPUT_DATASET,
                ml_flat_dataset="snapshots_ml_flat",
                raw_dataset="snapshots",
                all_dates_sorted=["2023-06-16"],
                dry_run=True,
                already_processed=set(),
            )

    assert result.status == "no_midday_row", f"expected no_midday_row, got {result.status}"
