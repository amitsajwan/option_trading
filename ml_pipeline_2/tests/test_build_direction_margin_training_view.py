"""End-to-end test of build_direction_margin_training_view.py against
synthetic parquet fixtures (no real VM data needed) -- same
`{parquet_base}/snapshots/trade_date=<date>/data.parquet` /
`snapshot_raw_json` format test_build_option_pnl_training_view.py and
test_build_entry_quality_direction_dataset.py already exercise."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.direction_margin import LEG_RAW_FEATURE_COLUMNS
from ml_pipeline_2.scripts.build_direction_margin_training_view import (
    build_margin_label_frame,
    main,
)
from ml_pipeline_2.scripts.build_option_pnl_training_view import (
    build_payoff_label_frame,
    read_day_snapshots,
)

LOT_SIZE = 65


def _snapshot_payload(
    *, minute: int, ce_ltp: float, pe_ltp: float, strike: int = 24500,
    ce_oi: float = 50000.0, pe_oi: float = 50000.0,
) -> dict:
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
            "atm_ce_oi": ce_oi, "atm_pe_oi": pe_oi,
            "atm_ce_iv": 14.0, "atm_pe_iv": 15.0,
            "atm_ce_volume": 10000.0 + minute, "atm_pe_volume": 8000.0 + minute,
            "atm_ce_oi_change_30m": 100.0, "atm_pe_oi_change_30m": -50.0,
            "atm_ce_vol_ratio": 1.1, "atm_pe_vol_ratio": 0.95,
        },
        "futures_bar": {"fut_close": 24500.0 + minute},
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
    # CE rallies, PE decays -- straightforward asymmetric-but-both-liquid day.
    payloads = [
        _snapshot_payload(minute=i, ce_ltp=100.0 + i * 2.0, pe_ltp=max(5.0, 100.0 - i * 1.0))
        for i in range(60)
    ]
    _write_day_parquet(base, "2026-01-05", payloads)
    return base


class TestBuildMarginLabelFrame:
    def test_produces_net_ce_and_pe_returns_separately(self, synthetic_parquet_base: Path) -> None:
        df = build_margin_label_frame(
            str(synthetic_parquet_base), ["2026-01-05"], lot_size=LOT_SIZE, horizon_bars=15,
        )
        assert len(df) > 0
        assert "net_ce_return" in df.columns
        assert "net_pe_return" in df.columns
        assert set(LEG_RAW_FEATURE_COLUMNS).issubset(df.columns)
        # CE rallies strongly in this fixture -- CE leg should net-profit for
        # at least some bars, PE should net-lose for at least some.
        assert (df["net_ce_return"] > 0).any()

    def test_keeps_rows_where_only_one_leg_is_usable(self, tmp_path: Path) -> None:
        base = tmp_path / "parquet_data"
        # PE OI below MIN_ENTRY_OI (1000) -- PE leg unusable, CE still fine.
        payloads = [
            _snapshot_payload(minute=i, ce_ltp=100.0 + i, pe_ltp=50.0, pe_oi=500.0)
            for i in range(60)
        ]
        _write_day_parquet(base, "2026-01-05", payloads)
        df = build_margin_label_frame(str(base), ["2026-01-05"], lot_size=LOT_SIZE, horizon_bars=15)
        assert len(df) > 0
        assert df["net_ce_return"].notna().any()
        assert df["net_pe_return"].isna().all()

    def test_missing_day_is_skipped_with_warning_not_fatal(self, synthetic_parquet_base: Path) -> None:
        df = build_margin_label_frame(
            str(synthetic_parquet_base), ["2026-01-05", "2099-01-01"], lot_size=LOT_SIZE, horizon_bars=15,
        )
        assert len(df) > 0

    def test_raises_if_no_requested_date_has_any_data(self, tmp_path: Path) -> None:
        empty_base = tmp_path / "empty"
        empty_base.mkdir()
        with pytest.raises(RuntimeError, match="no snapshots found"):
            build_margin_label_frame(str(empty_base), ["2026-01-05"], lot_size=LOT_SIZE, horizon_bars=15)


class TestMainEndToEnd:
    def _write_base_training_view(self, synthetic_parquet_base: Path, tmp_path: Path) -> Path:
        """Simulate build_option_pnl_training_view.py's real output shape --
        including payoff_score/payoff_winning_side computed the SAME way
        (via build_payoff_label_frame, reused directly) so the consistency
        check inside build_direction_margin_training_view.main() has a real,
        matching baseline to compare against."""
        label_df = build_payoff_label_frame(
            str(synthetic_parquet_base), ["2026-01-05"], lot_size=LOT_SIZE, horizon_bars=15,
        )
        label_df = label_df.rename(columns={"timestamp": "_ts"})
        rows = []
        for _, r in label_df.iterrows():
            rows.append({
                "trade_date": r["trade_date"],
                "timestamp": r["_ts"],
                "some_feature": 1.0,
                "atm_ce_return_1m": 0.01,
                "atm_pe_return_1m": -0.01,
                "payoff_score": r["payoff_score"],
                "payoff_winning_side": r["payoff_winning_side"],
                "payoff_strike": r["payoff_strike"],
            })
        training_view_csv = tmp_path / "training_view_nifty_option_pnl.csv.gz"
        pd.DataFrame(rows).to_csv(training_view_csv, index=False, compression="gzip")
        return training_view_csv

    def test_joins_per_leg_returns_onto_base_training_view(
        self, synthetic_parquet_base: Path, tmp_path: Path,
    ) -> None:
        training_view_csv = self._write_base_training_view(synthetic_parquet_base, tmp_path)
        output_csv = tmp_path / "training_view_nifty_direction_margin.csv.gz"
        rc = main([
            "--training-view-csv", str(training_view_csv),
            "--snapshot-parquet-base", str(synthetic_parquet_base),
            "--lot-size", str(LOT_SIZE),
            "--horizon-bars", "15",
            "--output", str(output_csv),
        ])
        assert rc == 0
        result = pd.read_csv(output_csv)
        assert len(result) > 0
        assert "net_ce_return" in result.columns
        assert "net_pe_return" in result.columns
        assert "some_feature" in result.columns
        assert "payoff_score" in result.columns
        # Consistency: max(net_ce, net_pe) should equal the pre-existing
        # payoff_score column almost exactly (same compute_bar_payoff call).
        computed_max = result[["net_ce_return", "net_pe_return"]].max(axis=1, skipna=True)
        np.testing.assert_allclose(computed_max.to_numpy(), result["payoff_score"].to_numpy(), atol=1e-6)

    def test_rejects_a_training_view_that_already_has_a_colliding_column(
        self, synthetic_parquet_base: Path, tmp_path: Path,
    ) -> None:
        training_view_csv = self._write_base_training_view(synthetic_parquet_base, tmp_path)
        df = pd.read_csv(training_view_csv)
        df["atm_ce_oi"] = 1.0  # collides with a LEG_RAW_FEATURE_COLUMNS entry
        df.to_csv(training_view_csv, index=False, compression="gzip")
        with pytest.raises(ValueError, match="already has column"):
            main([
                "--training-view-csv", str(training_view_csv),
                "--snapshot-parquet-base", str(synthetic_parquet_base),
                "--lot-size", str(LOT_SIZE),
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
                "--lot-size", str(LOT_SIZE),
                "--output", str(tmp_path / "out.csv.gz"),
            ])
