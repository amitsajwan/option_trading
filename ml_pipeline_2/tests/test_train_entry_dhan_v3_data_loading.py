"""Regression tests for train_entry_dhan_v3.load_training_view_csv --
added 2026-09-09 after discovering, while trying a real dry-run of the
overnight study runner on the VM, that NONE of BankNifty/NIFTY/FINNIFTY/
SENSEX actually had any `.data/dhan_pipeline/indicators` data (the
convention load_indicators() expects) -- what they DO have is a real,
current `.run/training_view*/training_view_<name>.csv.gz` per instrument,
in a format load_indicators() can't read at all (gzipped CSV, not
per-day parquet; a separate `trade_date`+`time` pair instead of one
`timestamp` column). Fixtures below match the real column shape
confirmed by inspecting training_view_banknifty.csv.gz directly on the
VM: trade_date,time,fut_close,...,vix_current,vix_intraday_chg,...
"""
from __future__ import annotations

import gzip

import pandas as pd
import pytest

from ml_pipeline_2.scripts.train_entry_dhan_v3 import load_training_view_csv


def _write_training_view_csv(path, rows: list[dict]) -> None:
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False, compression="infer")


class TestLoadTrainingViewCsv:
    def test_builds_a_timestamp_column_from_trade_date_and_time(self, tmp_path) -> None:
        path = tmp_path / "training_view_banknifty.csv.gz"
        _write_training_view_csv(path, [
            {"trade_date": "2026-01-05", "time": "09:15:00", "fut_close": 51529.7, "ema_9_slope": 0.01},
            {"trade_date": "2026-01-05", "time": "09:16:00", "fut_close": 51458.55, "ema_9_slope": -0.02},
        ])
        df = load_training_view_csv(path, "2026-01-01", "2026-01-31")
        assert "timestamp" in df.columns
        assert df.iloc[0]["timestamp"] == pd.Timestamp("2026-01-05 09:15:00")
        assert df.iloc[1]["timestamp"] == pd.Timestamp("2026-01-05 09:16:00")

    def test_filters_to_the_requested_date_window(self, tmp_path) -> None:
        path = tmp_path / "training_view_nifty.csv.gz"
        _write_training_view_csv(path, [
            {"trade_date": "2025-12-31", "time": "09:15:00", "fut_close": 100.0},
            {"trade_date": "2026-01-15", "time": "09:15:00", "fut_close": 200.0},
            {"trade_date": "2026-02-01", "time": "09:15:00", "fut_close": 300.0},
        ])
        df = load_training_view_csv(path, "2026-01-01", "2026-01-31")
        assert len(df) == 1
        assert df.iloc[0]["fut_close"] == 200.0

    def test_gzip_compression_is_transparently_handled(self, tmp_path) -> None:
        # .to_csv(compression="infer") writes real gzip when the path ends
        # in .csv.gz -- confirm the file really is gzip (matching every real
        # training_view file on the VM) and the loader reads it back fine.
        path = tmp_path / "training_view_finnifty.csv.gz"
        _write_training_view_csv(path, [{"trade_date": "2026-01-05", "time": "09:15:00", "fut_close": 1.0}])
        with gzip.open(path, "rb") as f:
            f.read()  # raises BadGzipFile if this isn't real gzip
        df = load_training_view_csv(path, "2026-01-01", "2026-01-31")
        assert len(df) == 1

    def test_existing_timestamp_column_is_left_alone(self, tmp_path) -> None:
        path = tmp_path / "training_view_sensex.csv.gz"
        _write_training_view_csv(path, [
            {"trade_date": "2026-01-05", "timestamp": "2026-01-05 09:15:00", "fut_close": 1.0},
        ])
        df = load_training_view_csv(path, "2026-01-01", "2026-01-31")
        assert df.iloc[0]["timestamp"] == "2026-01-05 09:15:00"  # untouched, not reconstructed

    def test_missing_trade_date_column_raises_clearly(self, tmp_path) -> None:
        path = tmp_path / "bad.csv.gz"
        _write_training_view_csv(path, [{"time": "09:15:00", "fut_close": 1.0}])
        with pytest.raises(ValueError, match="missing trade_date"):
            load_training_view_csv(path, "2026-01-01", "2026-01-31")

    def test_neither_timestamp_nor_time_column_raises_clearly(self, tmp_path) -> None:
        path = tmp_path / "bad.csv.gz"
        _write_training_view_csv(path, [{"trade_date": "2026-01-05", "fut_close": 1.0}])
        with pytest.raises(ValueError, match="neither 'timestamp' nor 'time'"):
            load_training_view_csv(path, "2026-01-01", "2026-01-31")

    def test_preserves_all_feature_columns_untouched(self, tmp_path) -> None:
        # add_labels/build_feature_matrix consume whatever columns are
        # present downstream -- this loader must not drop or rename any
        # of the real ENTRY_FEATURES_V3-style columns.
        path = tmp_path / "training_view_banknifty.csv.gz"
        _write_training_view_csv(path, [
            {"trade_date": "2026-01-05", "time": "09:15:00", "fut_close": 51529.7,
             "ema_9_slope": 0.01, "vel_ce_oi_delta_open": 100.0, "vix_current": 16.68},
        ])
        df = load_training_view_csv(path, "2026-01-01", "2026-01-31")
        assert df.iloc[0]["ema_9_slope"] == 0.01
        assert df.iloc[0]["vel_ce_oi_delta_open"] == 100.0
        assert df.iloc[0]["vix_current"] == 16.68
