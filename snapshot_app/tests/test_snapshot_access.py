from __future__ import annotations

from pathlib import Path
import warnings

import pandas as pd

from snapshot_app.historical.snapshot_access import (
    SNAPSHOT_INPUT_MODE_ML_FLAT,
    SNAPSHOT_INPUT_MODE_SUPPORT_V2,
    require_snapshot_access,
)


def test_require_snapshot_access_accepts_chunked_support_v2_layout(tmp_path: Path) -> None:
    parquet_base = tmp_path / "parquet"
    dataset_root = parquet_base / "snapshots_ml_flat_v2" / "year=2020" / "chunk=202001_202006_m6"
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
        mode=SNAPSHOT_INPUT_MODE_SUPPORT_V2,
        context="chunked_test",
        parquet_base=parquet_base,
    )

    assert info.dataset_name == "snapshots_ml_flat_v2"
    assert info.snapshot_trading_days == 1
    assert info.snapshot_min_trade_date == "2020-01-02"


def test_require_snapshot_access_maps_deprecated_ml_flat_alias_to_support_v2(tmp_path: Path) -> None:
    parquet_base = tmp_path / "parquet"
    dataset_root = parquet_base / "snapshots_ml_flat_v2" / "year=2020" / "chunk=202001_202006_m6"
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

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        info = require_snapshot_access(
            mode=SNAPSHOT_INPUT_MODE_ML_FLAT,
            context="deprecated_alias_test",
            parquet_base=parquet_base,
        )

    assert info.dataset_name == "snapshots_ml_flat_v2"
    assert any("deprecated" in str(item.message).lower() for item in caught)
