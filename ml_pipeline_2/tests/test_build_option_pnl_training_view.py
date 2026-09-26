"""End-to-end test of build_option_pnl_training_view.py against synthetic
parquet fixtures (no real Mongo/VM data needed) -- writes the exact
`{parquet_base}/snapshots/trade_date=<date>/data.parquet` /
`snapshot_raw_json` format ops/export_mongo_snapshots_to_parquet.py
produces, and the same format backtest_runner._read_sample_instrument and
replay_engine.replay_day consume, so this test doubles as a check that the
join script reads real-shaped data correctly."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ml_pipeline_2.scripts.build_option_pnl_training_view import (
    build_payoff_label_frame,
    join_features_to_labels,
    main,
    read_day_snapshots,
)

LOT_SIZE = 65


def _snapshot_payload(*, minute: int, ce_ltp: float, pe_ltp: float, strike: int = 24500) -> dict:
    # 09:45 IST = 04:15 UTC
    ts_ist_minute = 9 * 60 + 45 + minute
    hour, minute_of_hour = divmod(ts_ist_minute, 60)
    ts_ist = f"2026-01-05T{hour:02d}:{minute_of_hour:02d}:00"
    # convert to UTC (IST = UTC+5:30) for the raw payload's own timestamp field
    ts = pd.Timestamp(ts_ist) - pd.Timedelta(hours=5, minutes=30)
    return {
        "snapshot_id": f"s{minute}",
        "timestamp": ts.isoformat() + "+00:00",
        "trade_date": "2026-01-05",
        "session_context": {"date": "2026-01-05"},
        "chain_aggregates": {"atm_strike": strike},
        "atm_options": {
            "atm_ce_close": ce_ltp,
            "atm_pe_close": pe_ltp,
            "atm_ce_oi": 50000.0,
            "atm_pe_oi": 50000.0,
        },
        "strikes": [{"strike": strike, "ce_ltp": ce_ltp, "pe_ltp": pe_ltp}],
    }


def _write_day_parquet(parquet_base: Path, trade_date: str, payloads: list[dict]) -> None:
    day_dir = parquet_base / "snapshots" / f"trade_date={trade_date}"
    day_dir.mkdir(parents=True, exist_ok=True)
    rows = [{"snapshot_raw_json": json.dumps(p, default=str)} for p in payloads]
    pd.DataFrame(rows).to_parquet(day_dir / "data.parquet", index=False)


@pytest.fixture
def synthetic_parquet_base(tmp_path: Path) -> Path:
    base = tmp_path / "parquet_data"
    # A rallying day: CE premium climbs steadily, PE falls -- CE should win
    # the payoff label at every bar with enough runway left in the horizon.
    payloads = [
        _snapshot_payload(minute=i, ce_ltp=100.0 + i * 2.0, pe_ltp=max(5.0, 100.0 - i * 2.0))
        for i in range(60)
    ]
    _write_day_parquet(base, "2026-01-05", payloads)
    return base


class TestReadDaySnapshots:
    def test_reads_and_sorts_by_timestamp(self, synthetic_parquet_base: Path) -> None:
        snaps = read_day_snapshots(str(synthetic_parquet_base), "2026-01-05")
        assert len(snaps) == 60
        timestamps = [s.timestamp for s in snaps]
        assert timestamps == sorted(timestamps)

    def test_missing_day_returns_empty_list(self, synthetic_parquet_base: Path) -> None:
        assert read_day_snapshots(str(synthetic_parquet_base), "2099-01-01") == []


class TestBuildPayoffLabelFrame:
    def test_produces_positive_ce_labels_on_a_rallying_day(self, synthetic_parquet_base: Path) -> None:
        df = build_payoff_label_frame(
            str(synthetic_parquet_base), ["2026-01-05"], lot_size=LOT_SIZE, horizon_bars=5,
        )
        assert len(df) > 0
        assert (df["payoff_winning_side"] == "CE").all()
        assert (df["payoff_score"] > 0).mean() > 0.5

    def test_raises_if_no_trade_date_has_any_data(self, tmp_path: Path) -> None:
        empty_base = tmp_path / "empty"
        empty_base.mkdir()
        with pytest.raises(RuntimeError, match="no snapshots found"):
            build_payoff_label_frame(str(empty_base), ["2026-01-05"], lot_size=LOT_SIZE, horizon_bars=5)


class TestJoinFeaturesToLabels:
    def test_joins_on_normalized_timestamp_despite_string_format_difference(self) -> None:
        # Feature CSV's timestamp string differs in format (space separator,
        # no explicit offset) from the label frame's ISO-with-offset string
        # -- the join must still succeed via parsed-datetime comparison.
        feature_df = pd.DataFrame({
            "trade_date": ["2026-01-05", "2026-01-05"],
            "timestamp": ["2026-01-05 04:15:00+00:00", "2026-01-05 04:16:00+00:00"],
            "some_feature": [1.0, 2.0],
        })
        label_df = pd.DataFrame({
            "trade_date": ["2026-01-05", "2026-01-05"],
            "timestamp": ["2026-01-05T04:15:00+00:00", "2026-01-05T04:16:00+00:00"],
            "payoff_score": [0.05, -0.02],
            "payoff_winning_side": ["CE", "PE"],
            "payoff_strike": [24500, 24500],
        })
        merged = join_features_to_labels(feature_df, label_df)
        assert len(merged) == 2
        assert set(merged["payoff_score"]) == {0.05, -0.02}
        assert "some_feature" in merged.columns

    def test_rows_with_no_matching_label_are_dropped(self) -> None:
        feature_df = pd.DataFrame({
            "trade_date": ["2026-01-05", "2026-01-05"],
            "timestamp": ["2026-01-05T04:15:00+00:00", "2026-01-05T04:16:00+00:00"],
            "some_feature": [1.0, 2.0],
        })
        label_df = pd.DataFrame({
            "trade_date": ["2026-01-05"],
            "timestamp": ["2026-01-05T04:15:00+00:00"],
            "payoff_score": [0.05],
            "payoff_winning_side": ["CE"],
            "payoff_strike": [24500],
        })
        merged = join_features_to_labels(feature_df, label_df)
        assert len(merged) == 1


class TestMainEndToEnd:
    def test_full_pipeline_writes_augmented_csv(self, synthetic_parquet_base: Path, tmp_path: Path) -> None:
        # Build a minimal "existing feature training view" whose timestamps
        # line up with the synthetic snapshots' own raw timestamp strings.
        snaps = read_day_snapshots(str(synthetic_parquet_base), "2026-01-05")
        feature_rows = [
            {"trade_date": s.trade_date, "timestamp": s.raw_payload["timestamp"], "some_feature": i}
            for i, s in enumerate(snaps)
        ]
        training_view_csv = tmp_path / "training_view_nifty.csv.gz"
        pd.DataFrame(feature_rows).to_csv(training_view_csv, index=False, compression="gzip")

        output_csv = tmp_path / "training_view_nifty_option_pnl.csv.gz"
        rc = main([
            "--training-view-csv", str(training_view_csv),
            "--snapshot-parquet-base", str(synthetic_parquet_base),
            "--lot-size", str(LOT_SIZE),
            "--horizon-bars", "5",
            "--output", str(output_csv),
        ])
        assert rc == 0
        assert output_csv.exists()
        result = pd.read_csv(output_csv)
        assert "payoff_score" in result.columns
        assert "some_feature" in result.columns
        assert len(result) > 0
