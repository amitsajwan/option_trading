from __future__ import annotations

import pandas as pd
import pytest

from ml_pipeline_2.model_search.features import select_feature_columns
from ml_pipeline_2.staged.entry_move_oracle import build_entry_bn_move_oracle


def _synthetic_day() -> pd.DataFrame:
    """Flat 50000, then rally so +120 pts within 5 forward bars → label=1 at t0."""
    rows = []
    base = 50_000.0
    for i in range(8):
        px = base + (24.0 * i if i > 0 else 0.0)
        rows.append(
            {
                "trade_date": "2024-06-03",
                "timestamp": f"2024-06-03 10:{15 + i:02d}:00",
                "snapshot_id": f"20240603_10{15 + i:02d}",
                "px_fut_close": px,
                "px_fut_high": px + 5.0,
                "px_fut_low": px - 5.0,
                "px_fut_open": px,
            }
        )
    return pd.DataFrame(rows)


def test_entry_bn_move_labels_positive_on_120pt_rally() -> None:
    oracle = build_entry_bn_move_oracle(_synthetic_day(), horizon_minutes=5, min_points=100.0)
    first = oracle.iloc[0]
    assert int(first["entry_label"]) == 1
    assert int(first["entry_label_valid"]) == 1
    assert float(first["entry_threshold_pct"]) == pytest.approx(100.0 / 50_000.0, rel=1e-6)


def test_entry_bn_move_insufficient_forward_bars_invalid() -> None:
    day = _synthetic_day().iloc[-2:].copy()
    oracle = build_entry_bn_move_oracle(day, horizon_minutes=5, min_points=100.0)
    assert int(oracle.iloc[-1]["entry_label_valid"]) == 0


def test_entry_bn_oracle_columns_excluded_from_model_features() -> None:
    oracle = build_entry_bn_move_oracle(_synthetic_day(), horizon_minutes=5, min_points=100.0)
    frame = oracle.assign(ema_9_slope=0.1)
    selected = select_feature_columns(frame, feature_profile="all")
    for col in (
        "entry_label",
        "entry_label_valid",
        "entry_up_move_pct",
        "entry_down_move_pct",
        "entry_threshold_pct",
    ):
        assert col not in selected
    assert "ema_9_slope" in selected
