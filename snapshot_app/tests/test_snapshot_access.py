from __future__ import annotations

from pathlib import Path

import pandas as pd

from snapshot_app.historical.snapshot_access import (
    SNAPSHOT_INPUT_MODE_ML_FLAT,
    require_snapshot_access,
)


def test_require_snapshot_access_accepts_chunked_ml_flat_layout(tmp_path: Path) -> None:
    parquet_base = tmp_path / "parquet"
    dataset_root = parquet_base / "snapshots_ml_flat" / "year=2020" / "chunk=202001_202006_m6"
    dataset_root.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "trade_date": "2020-01-02",
                "timestamp": "2020-01-02T09:15:00",
                "snapshot_id": "20200102_0915",
            }
        ]
    ).to_parquet(dataset_root / "data.parquet", index=False)

    info = require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_ML_FLAT,
        context="chunked_test",
        parquet_base=parquet_base,
    )

    assert info.dataset_name == "snapshots_ml_flat"
    assert info.snapshot_trading_days == 1
    assert info.snapshot_min_trade_date == "2020-01-02"
