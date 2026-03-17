from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from snapshot_app.pipeline.normalize import normalize_raw_to_parquet


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def test_normalize_raw_to_parquet_writes_partitioned_inputs_and_vix(tmp_path: Path) -> None:
    raw_root = tmp_path / "banknifty_data"
    parquet_base = tmp_path / "parquet_data"

    _write(
        raw_root / "banknifty_fut" / "2024" / "1" / "banknifty_fut_01_01_2024.csv",
        """
        date,time,symbol,open,high,low,close,oi,volume
        2024-01-01,09:15:00,BANKNIFTY-I,100,101,99,100.5,123,456
        2024-01-01,09:16:00,BANKNIFTY-I,100.5,102,100,101.5,124,460
        """,
    )
    _write(
        raw_root / "banknifty_spot" / "2024" / "1" / "banknifty_spot01_01_2024.csv",
        """
        date,time,symbol,open,high,low,close
        2024-01-01,09:15:00,BANKNIFTY,95,96,94,95.5
        """,
    )
    _write(
        raw_root / "banknifty_options" / "2024" / "1" / "banknifty_options_01_01_2024.csv",
        """
        date,time,symbol,open,high,low,close,oi,volume
        2024-01-01,09:15:00,BANKNIFTY04JAN2447800CE,10,11,9,10.5,50,100
        2024-01-01,09:15:00,BANKNIFTY04JAN2447800PE,8,8.5,7.5,8.1,60,90
        """,
    )
    _write(
        raw_root / "VIX" / "hist_india_vix_-01-01-2024-to-31-12-2024.csv",
        """
        Date ,Open ,High ,Low ,Close ,Prev. Close ,Change ,% Change
        01-JAN-2024,14.5,14.8,14.2,14.6,14.4,0.2,1.3
        02-JAN-2024,14.6,15.0,14.3,14.7,14.6,0.1,0.7
        """,
    )

    result = normalize_raw_to_parquet(raw_root=raw_root, parquet_base=parquet_base, jobs=1)

    assert result["status"] == "complete"
    assert result["tasks_written"] == 3
    assert result["vix_result"]["status"] == "written"

    fut = pd.read_parquet(parquet_base / "futures" / "year=2024" / "month=01" / "data.parquet")
    assert list(fut.columns) == ["timestamp", "trade_date", "symbol", "open", "high", "low", "close", "volume", "oi"]
    assert fut["trade_date"].tolist() == ["2024-01-01", "2024-01-01"]

    opt = pd.read_parquet(parquet_base / "options" / "year=2024" / "month=01" / "data.parquet")
    assert sorted(opt["option_type"].unique().tolist()) == ["CE", "PE"]
    assert opt["strike"].tolist() == [47800.0, 47800.0]
    assert opt["expiry_str"].tolist() == ["04JAN24", "04JAN24"]

    spot = pd.read_parquet(parquet_base / "spot" / "year=2024" / "month=01" / "data.parquet")
    assert len(spot) == 1
    assert float(spot.iloc[0]["close"]) == 95.5

    vix = pd.read_parquet(parquet_base / "vix" / "vix.parquet")
    assert vix["trade_date"].tolist() == ["2024-01-01", "2024-01-02"]
    assert float(vix.iloc[0]["vix_prev_close"]) == 14.4
