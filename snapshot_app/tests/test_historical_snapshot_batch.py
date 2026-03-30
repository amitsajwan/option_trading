from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from snapshot_app.historical.snapshot_batch import (
    _build_all_chains,
    _build_spot_map,
    _chain_totals,
    _find_atm_row,
    _project_rows_to_ml_flat,
    run_snapshot_batch,
    write_days_to_parquet,
)


class _FakeParquetStore:
    def __init__(self, base_path: Path, *, snapshots_dataset: str = "snapshots") -> None:
        self.base_path = Path(base_path)
        self.snapshots_dataset = snapshots_dataset
        self.has_options_calls = 0
        self.futures_window_for_days_calls: list[list[str]] = []

    def available_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        return ["2020-01-29", "2020-01-30"]

    def available_snapshot_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        return []

    def has_options_for_day(self, trade_date: str) -> bool:
        self.has_options_calls += 1
        return True

    def all_days_with_options(self, *, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        return ["2020-01-29", "2020-01-30"]

    def vix(self) -> pd.DataFrame:
        return pd.DataFrame()

    def futures_window_for_days(self, trade_dates: list[str]) -> pd.DataFrame:
        self.futures_window_for_days_calls.append(list(trade_dates))
        rows = []
        for trade_date in trade_dates:
            rows.append(
                {
                    "timestamp": f"{trade_date} 09:15:00",
                    "trade_date": trade_date,
                    "symbol": "BANKNIFTY-I",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1000.0,
                    "oi": 2000.0,
                }
            )
        return pd.DataFrame(rows)


def test_run_snapshot_batch_flushes_canonical_and_market_base(monkeypatch, tmp_path: Path) -> None:
    emit_calls: list[tuple[str, bool]] = []
    futures_window_calls: list[tuple[str, list[str] | None]] = []
    preloaded_window_rows: list[tuple[str, int]] = []

    def _fake_process_day(**kwargs):
        trade_date = str(kwargs["trade_date"])
        emit_calls.append((trade_date, bool(kwargs.get("emit_outputs", True))))
        futures_window_calls.append((trade_date, list(kwargs.get("futures_window_days") or [])))
        preloaded_window = kwargs.get("preloaded_fut_window")
        preloaded_window_rows.append((trade_date, 0 if preloaded_window is None else int(len(preloaded_window))))
        if not bool(kwargs.get("emit_outputs", True)):
            return {
                "snapshot_rows": [],
                "market_base_rows": [],
            }
        return {
            "snapshot_rows": [
                {
                    "trade_date": trade_date,
                    "timestamp": f"{trade_date} 09:15:00",
                    "snapshot_id": f"{trade_date}_001",
                    "snapshot_raw_json": "{}",
                    "build_source": "historical",
                    "build_run_id": "test_run",
                }
            ],
            "market_base_rows": [
                {
                    "trade_date": trade_date,
                    "year": 2020,
                    "timestamp": f"{trade_date} 09:15:00",
                    "snapshot_id": f"{trade_date}_001",
                    "schema_name": "MarketSnapshot",
                    "schema_version": "3.0",
                    "instrument": "BANKNIFTY-I",
                    "build_source": "historical",
                    "build_run_id": "test_run",
                    "opt_flow_rows": 3,
                }
            ],
        }

    monkeypatch.setattr("snapshot_app.historical.snapshot_batch.ParquetStore", _FakeParquetStore)
    monkeypatch.setattr("snapshot_app.historical.snapshot_batch.process_day", _fake_process_day)

    result = run_snapshot_batch(
        parquet_base=tmp_path,
        instrument="BANKNIFTY-I",
        min_day="2020-01-30",
        max_day="2020-01-30",
        planned_days=["2020-01-29", "2020-01-30"],
        emit_days=["2020-01-30"],
        resume=False,
        write_batch_days=1,
        build_source="historical",
        build_run_id="test_run",
        validate_ml_flat_contract=False,
        partition_key="202001_202001_m1",
    )

    snapshots_path = tmp_path / "snapshots" / "year=2020" / "chunk=202001_202001_m1" / "data.parquet"
    market_base_path = tmp_path / "market_base" / "year=2020" / "chunk=202001_202001_m1" / "data.parquet"

    assert result["status"] == "complete"
    assert result["days_processed"] == 1
    assert result["warmup_days_processed"] == 1
    assert result["total_snapshot_rows"] == 1
    assert result["total_rows"] == 1
    assert result["total_market_base_rows"] == 1
    assert snapshots_path.exists()
    assert market_base_path.exists()
    assert not list((snapshots_path.parent).glob("data.tmp_*.parquet"))
    assert not list((market_base_path.parent).glob("data.tmp_*.parquet"))

    snapshots_df = pd.read_parquet(snapshots_path)
    market_base_df = pd.read_parquet(market_base_path)

    assert snapshots_df["trade_date"].astype(str).tolist() == ["2020-01-30"]
    assert market_base_df["trade_date"].astype(str).tolist() == ["2020-01-30"]
    assert emit_calls == [("2020-01-29", False), ("2020-01-30", True)]
    assert futures_window_calls == [
        ("2020-01-29", ["2020-01-29"]),
        ("2020-01-30", ["2020-01-29", "2020-01-30"]),
    ]
    assert preloaded_window_rows == [("2020-01-29", 1), ("2020-01-30", 2)]


def test_build_all_chains_exposes_cached_lookup_and_totals() -> None:
    options_day = pd.DataFrame(
        {
            "timestamp": ["2020-01-30 09:15:00", "2020-01-30 09:15:00"],
            "strike": [30000, 30100],
            "option_type": ["CE", "PE"],
            "close": [100.0, 90.0],
            "oi": [1000.0, 900.0],
            "volume": [10.0, 20.0],
            "expiry_str": ["30JAN20", "30JAN20"],
        }
    )

    chains = _build_all_chains(options_day)
    chain = chains["2020-01-30 09:15:00"]

    assert _find_atm_row(chain, 30000)["strike"] == 30000.0
    assert _chain_totals(chain) == (10.0, 20.0, 2.0)


def test_run_snapshot_batch_marks_missing_input_days_as_partial_incomplete(monkeypatch, tmp_path: Path) -> None:
    class _MissingOptionsStore(_FakeParquetStore):
        def all_days_with_options(self, *, min_day: str | None = None, max_day: str | None = None) -> list[str]:
            return ["2020-01-29"]

    def _fake_process_day(**kwargs):
        trade_date = str(kwargs["trade_date"])
        return {
            "snapshot_rows": [
                {
                    "trade_date": trade_date,
                    "timestamp": f"{trade_date} 09:15:00",
                    "snapshot_id": f"{trade_date}_001",
                    "snapshot_raw_json": "{}",
                    "build_source": "historical",
                    "build_run_id": "test_run",
                }
            ],
            "market_base_rows": [
                {
                    "trade_date": trade_date,
                    "year": 2020,
                    "timestamp": f"{trade_date} 09:15:00",
                    "snapshot_id": f"{trade_date}_001",
                    "schema_name": "MarketSnapshot",
                    "schema_version": "3.0",
                    "instrument": "BANKNIFTY-I",
                    "build_source": "historical",
                    "build_run_id": "test_run",
                    "opt_flow_rows": 3,
                }
            ],
        }

    monkeypatch.setattr("snapshot_app.historical.snapshot_batch.ParquetStore", _MissingOptionsStore)
    monkeypatch.setattr("snapshot_app.historical.snapshot_batch.process_day", _fake_process_day)

    result = run_snapshot_batch(
        parquet_base=tmp_path,
        instrument="BANKNIFTY-I",
        min_day="2020-01-29",
        max_day="2020-01-30",
        resume=False,
        write_batch_days=1,
        build_source="historical",
        build_run_id="test_run",
        validate_ml_flat_contract=False,
        partition_key="202001_202001_m1",
    )

    assert result["status"] == "partial_incomplete"
    assert result["days_processed"] == 1
    assert result["days_skipped_missing_inputs"] == 1
    assert result["missing_input_days"] == ["2020-01-30"]


def test_write_days_to_parquet_preserves_existing_file_when_atomic_replace_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    partition_key = "202001_202001_m1"
    out_path = tmp_path / "snapshots" / "year=2020" / f"chunk={partition_key}" / "data.parquet"
    initial_rows = [
        {
            "trade_date": "2020-01-29",
            "timestamp": "2020-01-29 09:15:00",
            "snapshot_id": "20200129_0915",
            "snapshot_raw_json": "{}",
        }
    ]
    replacement_rows = [
        {
            "trade_date": "2020-01-30",
            "timestamp": "2020-01-30 09:15:00",
            "snapshot_id": "20200130_0915",
            "snapshot_raw_json": "{}",
        }
    ]

    write_days_to_parquet(
        initial_rows,
        out_base=tmp_path,
        year=2020,
        output_dataset="snapshots",
        replace_trade_dates={"2020-01-29"},
        partition_key=partition_key,
    )
    original = pd.read_parquet(out_path)
    original_to_parquet = pd.DataFrame.to_parquet

    def _failing_to_parquet(self, path, *args, **kwargs):
        candidate = Path(path)
        if candidate.parent == out_path.parent and candidate.name.startswith("data.tmp_"):
            candidate.write_bytes(b"partial parquet temp")
            raise RuntimeError("simulated parquet write failure")
        return original_to_parquet(self, path, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", _failing_to_parquet)

    with pytest.raises(RuntimeError, match="simulated parquet write failure"):
        write_days_to_parquet(
            replacement_rows,
            out_base=tmp_path,
            year=2020,
            output_dataset="snapshots",
            replace_trade_dates={"2020-01-30"},
            partition_key=partition_key,
        )

    pd.testing.assert_frame_equal(pd.read_parquet(out_path), original)
    assert not list(out_path.parent.glob("data.tmp_*.parquet"))


def test_project_rows_to_ml_flat_prefers_canonical_same_strike_atm_fields() -> None:
    rows = [
        {
            "trade_date": "2020-01-30",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-01-30T09:15:00+05:30",
            "snapshot_id": "20200130_0915",
            "atm_ce_close": 100.0,
            "atm_pe_close": 90.0,
            "atm_ce_oi": 1000.0,
            "atm_pe_oi": 900.0,
            "opt_flow_atm_strike": 50000.0,
            "opt_flow_rows": 3.0,
        },
        {
            "trade_date": "2020-01-30",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-01-30T09:16:00+05:30",
            "snapshot_id": "20200130_0916",
            "atm_ce_close": 80.0,
            "atm_pe_close": 110.0,
            "atm_ce_oi": 1015.0,
            "atm_pe_oi": 920.0,
            "atm_ce_return_1m": 0.123,
            "atm_pe_return_1m": -0.234,
            "atm_ce_oi_change_1m": 10.0,
            "atm_pe_oi_change_1m": 20.0,
            "opt_flow_atm_strike": 50100.0,
            "opt_flow_rows": 3.0,
        },
    ]

    projected = _project_rows_to_ml_flat(rows, build_source="historical", build_run_id="test_run")

    assert projected[1]["opt_flow_atm_call_return_1m"] == 0.123
    assert projected[1]["opt_flow_atm_put_return_1m"] == -0.234
    assert projected[1]["opt_flow_atm_oi_change_1m"] == 30.0


def test_build_spot_map_uses_asof_alignment_for_futures_minutes() -> None:
    spot_day = pd.DataFrame(
        {
            "timestamp": ["2021-02-24 09:15:00", "2021-02-24 09:17:00"],
            "open": [100.0, 102.0],
            "high": [101.0, 103.0],
            "low": [99.0, 101.0],
            "close": [100.5, 102.5],
        }
    )

    mapped = _build_spot_map(
        spot_day,
        fut_timestamps=pd.Series(
            pd.to_datetime(
                [
                    "2021-02-24 09:15:00",
                    "2021-02-24 09:16:00",
                    "2021-02-24 09:17:00",
                ]
            )
        ),
    )

    assert mapped["2021-02-24 09:15:00"]["spot_close"] == 100.5
    assert mapped["2021-02-24 09:16:00"]["spot_close"] == 100.5
    assert mapped["2021-02-24 09:17:00"]["spot_close"] == 102.5


def test_project_rows_to_ml_flat_uses_session_bar_index_for_short_sessions() -> None:
    rows = []
    for idx, minute in enumerate([18 * 60 + 15, 18 * 60 + 16, 18 * 60 + 17]):
        rows.append(
            {
                "trade_date": "2023-11-12",
                "year": 2023,
                "instrument": "BANKNIFTY-I",
                "timestamp": f"2023-11-12T{minute // 60:02d}:{minute % 60:02d}:00+05:30",
                "snapshot_id": f"20231112_{idx}",
                "fut_open": 100.0 + idx,
                "fut_high": 101.0 + idx,
                "fut_low": 99.0 + idx,
                "fut_close": 100.5 + idx,
                "spot_open": 100.0 + idx,
                "spot_high": 101.0 + idx,
                "spot_low": 99.0 + idx,
                "spot_close": 100.25 + idx,
                "fut_volume": 1000.0 + idx,
                "fut_oi": 2000.0 + idx,
                "strike_count": 3.0,
                "total_ce_oi": 100.0,
                "total_pe_oi": 100.0,
                "total_ce_volume": 10.0,
                "total_pe_volume": 10.0,
                "pcr": 1.0,
                "days_to_expiry": 2.0,
                "is_expiry_day": 0.0,
                "minutes_since_open": 540.0 + idx,
            }
        )

    projected = _project_rows_to_ml_flat(rows, build_source="historical", build_run_id="test_run")

    assert [row["time_minute_index"] for row in projected] == [0.0, 1.0, 2.0]


def test_project_rows_to_ml_flat_zero_denominator_flow_features_fill_zero() -> None:
    rows = []
    for idx in range(25):
        rows.append(
            {
                "trade_date": "2020-06-04",
                "year": 2020,
                "instrument": "BANKNIFTY-I",
                "timestamp": f"2020-06-04T09:{15 + idx:02d}:00+05:30",
                "snapshot_id": f"20200604_{idx}",
                "fut_open": 100.0,
                "fut_high": 101.0,
                "fut_low": 99.0,
                "fut_close": 100.0,
                "spot_open": 99.5,
                "spot_high": 100.5,
                "spot_low": 99.0,
                "spot_close": 99.8,
                "fut_volume": 0.0,
                "fut_oi": 0.0,
                "strike_count": 0.0,
                "total_ce_oi": None,
                "total_pe_oi": None,
                "total_ce_volume": None,
                "total_pe_volume": None,
                "pcr": None,
                "days_to_expiry": 2.0,
                "is_expiry_day": 0.0,
            }
        )

    projected = _project_rows_to_ml_flat(rows, build_source="historical", build_run_id="test_run")

    assert projected[-1]["fut_flow_rel_volume_20"] == 0.0
    assert projected[-1]["fut_flow_oi_rel_20"] == 0.0
    assert projected[-1]["fut_flow_oi_zscore_20"] == 0.0
    assert projected[-1]["opt_flow_rel_volume_20"] == 0.0


def test_project_rows_to_ml_flat_pcr_change_fallback_resets_by_trade_date() -> None:
    rows = []
    for idx in range(6):
        rows.append(
            {
                "trade_date": "2020-06-04",
                "year": 2020,
                "instrument": "BANKNIFTY-I",
                "timestamp": f"2020-06-04T09:{15 + idx:02d}:00+05:30",
                "snapshot_id": f"20200604_{idx}",
                "fut_open": 100.0,
                "fut_high": 101.0,
                "fut_low": 99.0,
                "fut_close": 100.0,
                "pcr": 1.00 + (0.01 * idx),
            }
        )
    rows.append(
        {
            "trade_date": "2020-06-05",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-06-05T09:15:00+05:30",
            "snapshot_id": "20200605_0",
            "fut_open": 101.0,
            "fut_high": 102.0,
            "fut_low": 100.0,
            "fut_close": 101.0,
            "pcr": 1.50,
        }
    )

    projected = _project_rows_to_ml_flat(rows, build_source="historical", build_run_id="test_run")

    assert projected[0]["pcr_change_5m"] is None
    assert projected[4]["pcr_change_5m"] is None
    assert projected[5]["pcr_change_5m"] == 0.05
    assert projected[6]["pcr_change_5m"] is None
    assert projected[6]["pcr_change_15m"] is None


def test_project_rows_to_ml_flat_atm_fallbacks_reset_by_trade_date() -> None:
    rows = [
        {
            "trade_date": "2020-06-04",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-06-04T09:15:00+05:30",
            "snapshot_id": "20200604_0",
            "fut_open": 100.0,
            "fut_high": 101.0,
            "fut_low": 99.0,
            "fut_close": 100.0,
            "atm_ce_close": 100.0,
            "atm_pe_close": 200.0,
            "atm_ce_oi": 1000.0,
            "atm_pe_oi": 900.0,
        },
        {
            "trade_date": "2020-06-04",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-06-04T09:16:00+05:30",
            "snapshot_id": "20200604_1",
            "fut_open": 101.0,
            "fut_high": 102.0,
            "fut_low": 100.0,
            "fut_close": 101.0,
            "atm_ce_close": 110.0,
            "atm_pe_close": 180.0,
            "atm_ce_oi": 1010.0,
            "atm_pe_oi": 920.0,
        },
        {
            "trade_date": "2020-06-05",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-06-05T09:15:00+05:30",
            "snapshot_id": "20200605_0",
            "fut_open": 102.0,
            "fut_high": 103.0,
            "fut_low": 101.0,
            "fut_close": 102.0,
            "atm_ce_close": 150.0,
            "atm_pe_close": 250.0,
            "atm_ce_oi": 1100.0,
            "atm_pe_oi": 980.0,
        },
    ]

    projected = _project_rows_to_ml_flat(rows, build_source="historical", build_run_id="test_run")

    assert projected[0]["opt_flow_atm_call_return_1m"] is None
    assert projected[1]["opt_flow_atm_call_return_1m"] == 0.10
    assert projected[1]["opt_flow_atm_put_return_1m"] == -0.10
    assert projected[1]["opt_flow_atm_oi_change_1m"] == 30.0
    assert projected[2]["opt_flow_atm_call_return_1m"] is None
    assert projected[2]["opt_flow_atm_put_return_1m"] is None
    assert projected[2]["opt_flow_atm_oi_change_1m"] is None


def test_project_rows_to_ml_flat_normalizes_ema_slope_fallbacks() -> None:
    rows = [
        {
            "trade_date": "2020-01-30",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-01-30T09:15:00+05:30",
            "snapshot_id": "20200130_0915",
            "fut_open": 100.0,
            "fut_high": 101.0,
            "fut_low": 99.0,
            "fut_close": 100.0,
        },
        {
            "trade_date": "2020-01-30",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-01-30T09:16:00+05:30",
            "snapshot_id": "20200130_0916",
            "fut_open": 110.0,
            "fut_high": 111.0,
            "fut_low": 109.0,
            "fut_close": 110.0,
        },
    ]

    projected = _project_rows_to_ml_flat(rows, build_source="historical", build_run_id="test_run")

    assert abs(projected[1]["ema_9_slope"] - (2.0 / 110.0)) < 1e-9
    assert projected[1]["ema_9_slope"] < 0.1


def test_project_rows_to_ml_flat_uses_daily_atr_percentile_as_fallback() -> None:
    rows = [
        {
            "trade_date": "2020-01-30",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-01-30T09:15:00+05:30",
            "snapshot_id": "20200130_0915",
            "fut_open": 100.0,
            "fut_high": 101.0,
            "fut_low": 99.0,
            "fut_close": 100.0,
            "atr_daily_percentile": 0.42,
        },
        {
            "trade_date": "2020-01-30",
            "year": 2020,
            "instrument": "BANKNIFTY-I",
            "timestamp": "2020-01-30T09:16:00+05:30",
            "snapshot_id": "20200130_0916",
            "fut_open": 101.0,
            "fut_high": 102.0,
            "fut_low": 100.0,
            "fut_close": 101.0,
            "atr_daily_percentile": 0.42,
        },
    ]

    projected = _project_rows_to_ml_flat(rows, build_source="historical", build_run_id="test_run")

    assert projected[0]["osc_atr_percentile"] == 0.42
    assert projected[1]["osc_atr_percentile"] == 0.42
    assert projected[0]["osc_atr_daily_percentile"] == 0.42
    assert projected[1]["osc_atr_daily_percentile"] == 0.42
