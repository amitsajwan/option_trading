"""End-to-end test of build_entry_quality_direction_dataset.py against
synthetic parquet fixtures (no real VM data needed) -- same
`{parquet_base}/snapshots/trade_date=<date>/data.parquet` /
`snapshot_raw_json` format test_build_option_pnl_training_view.py already
exercises."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ml_pipeline_2.scripts.build_entry_quality_direction_dataset import (
    build_raw_signal_dataset,
    main,
)
from ml_pipeline_2.scripts.build_option_pnl_training_view import read_day_snapshots

LOT_SIZE = 65


def _snapshot_payload(*, minute: int, ce_ltp: float, pe_ltp: float, strike: int = 24500) -> dict:
    ts_ist_minute = 9 * 60 + 45 + minute
    hour, minute_of_hour = divmod(ts_ist_minute, 60)
    ts_ist = f"2026-01-05T{hour:02d}:{minute_of_hour:02d}:00"
    ts = pd.Timestamp(ts_ist) - pd.Timedelta(hours=5, minutes=30)
    return {
        "snapshot_id": f"s{minute}",
        "timestamp": ts.isoformat() + "+00:00",
        "trade_date": "2026-01-05",
        "session_context": {"date": "2026-01-05"},
        "chain_aggregates": {"atm_strike": strike, "pcr": 1.0 + 0.001 * minute},
        "atm_options": {
            "atm_ce_close": ce_ltp, "atm_pe_close": pe_ltp,
            "atm_ce_oi": 50000.0, "atm_pe_oi": 50000.0,
            "atm_ce_iv": 14.0, "atm_pe_iv": 15.0,
        },
        "futures_derived": {"fut_return_5m": 1.0, "fut_return_15m": 1.5, "price_vs_vwap": 0.3},
        "futures_bar": {"fut_close": 24500.0 + minute},
        "opening_range": {"orh": 24600.0, "orl": 24400.0, "orh_broken": False, "orl_broken": False},
        "vix_context": {"vix_intraday_chg": -0.5},
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
    payloads = [
        _snapshot_payload(minute=i, ce_ltp=100.0 + i * 2.0, pe_ltp=max(5.0, 100.0 - i * 2.0))
        for i in range(60)
    ]
    _write_day_parquet(base, "2026-01-05", payloads)
    return base


class TestBuildRawSignalDataset:
    def test_produces_one_row_per_bar_with_raw_signal_columns(self, synthetic_parquet_base: Path) -> None:
        df = build_raw_signal_dataset(str(synthetic_parquet_base), ["2026-01-05"])
        assert len(df) == 60
        assert "price_vs_vwap" in df.columns
        assert "orh" in df.columns
        assert list(df["bar_index"]) == list(range(60))
        # pcr rises steadily across the synthetic day -- pcr_change_5m should
        # be derived (non-null) once there's 5 bars of lookback.
        assert df["pcr_change_5m"].iloc[:5].isna().all()
        assert df["pcr_change_5m"].iloc[10] == pytest.approx(0.005)

    def test_missing_day_is_skipped_with_warning_not_fatal(self, synthetic_parquet_base: Path) -> None:
        df = build_raw_signal_dataset(str(synthetic_parquet_base), ["2026-01-05", "2099-01-01"])
        assert len(df) == 60

    def test_raises_if_no_requested_date_has_any_data(self, tmp_path: Path) -> None:
        empty_base = tmp_path / "empty"
        empty_base.mkdir()
        with pytest.raises(RuntimeError, match="no snapshots found"):
            build_raw_signal_dataset(str(empty_base), ["2026-01-05"])


class TestMainEndToEnd:
    def test_joins_raw_signals_onto_a_payoff_labeled_training_view(
        self, synthetic_parquet_base: Path, tmp_path: Path,
    ) -> None:
        snaps = read_day_snapshots(str(synthetic_parquet_base), "2026-01-05")
        # Simulate build_option_pnl_training_view.py's OUTPUT shape: feature
        # columns + payoff_score/payoff_winning_side, keyed by trade_date+timestamp.
        rows = []
        for i, s in enumerate(snaps):
            rows.append({
                "trade_date": s.trade_date,
                "timestamp": s.raw_payload["timestamp"],
                "some_feature": float(i),
                "payoff_score": 0.05,
                "payoff_winning_side": "CE",
                "payoff_strike": 24500,
            })
        training_view_csv = tmp_path / "training_view_nifty_option_pnl.csv.gz"
        pd.DataFrame(rows).to_csv(training_view_csv, index=False, compression="gzip")

        output_csv = tmp_path / "training_view_nifty_direction_condition.csv.gz"
        rc = main([
            "--training-view-csv", str(training_view_csv),
            "--snapshot-parquet-base", str(synthetic_parquet_base),
            "--output", str(output_csv),
        ])
        assert rc == 0
        result = pd.read_csv(output_csv)
        assert "payoff_winning_side" in result.columns
        assert "price_vs_vwap" in result.columns
        assert "some_feature" in result.columns
        assert len(result) > 0

    def test_rejects_a_training_view_that_already_has_a_raw_signal_column(
        self, synthetic_parquet_base: Path, tmp_path: Path,
    ) -> None:
        # Mirrors the real 2026-09-27 incident: training_view_nifty_option_pnl
        # .csv.gz already carries fut_return_5m/15m/fut_close/vix_intraday_chg
        # under those exact names -- joining a freshly-extracted copy under
        # the same name would silently pandas-suffix both instead of erroring.
        snaps = read_day_snapshots(str(synthetic_parquet_base), "2026-01-05")
        rows = [{
            "trade_date": s.trade_date, "timestamp": s.raw_payload["timestamp"],
            "price_vs_vwap": 0.1,  # collides with a RAW_SIGNAL_COLUMNS entry
            "payoff_score": 0.05, "payoff_winning_side": "CE", "payoff_strike": 24500,
        } for s in snaps]
        training_view_csv = tmp_path / "training_view_nifty_option_pnl.csv.gz"
        pd.DataFrame(rows).to_csv(training_view_csv, index=False, compression="gzip")
        with pytest.raises(ValueError, match="already has column"):
            main([
                "--training-view-csv", str(training_view_csv),
                "--snapshot-parquet-base", str(synthetic_parquet_base),
                "--output", str(tmp_path / "out.csv.gz"),
            ])

    def test_rejects_a_feature_only_training_view_missing_payoff_columns(self, tmp_path: Path) -> None:
        training_view_csv = tmp_path / "training_view_nifty.csv.gz"
        pd.DataFrame({"trade_date": ["2026-01-05"], "timestamp": ["x"], "f": [1.0]}).to_csv(
            training_view_csv, index=False, compression="gzip",
        )
        with pytest.raises(ValueError, match="payoff_winning_side"):
            main([
                "--training-view-csv", str(training_view_csv),
                "--snapshot-parquet-base", str(tmp_path),
                "--output", str(tmp_path / "out.csv.gz"),
            ])
