from __future__ import annotations

import pandas as pd
import pytest

from ml_pipeline_2.model_search.features import select_feature_columns
from ml_pipeline_2.staged.entry_move_oracle import (
    build_entry_bn_clean_move_oracle,
    build_entry_bn_move_oracle,
    stage1_clean_move_config,
)


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


def test_entry_bn_move_min_pct_is_level_invariant() -> None:
    """min_pct defines the threshold directly and ignores min_points/level."""
    day = _synthetic_day()
    # 0.20% of 50000 == 100 pts; the +120pt rally clears it at t0.
    oracle = build_entry_bn_move_oracle(day, horizon_minutes=5, min_pct=0.0020)
    first = oracle.iloc[0]
    assert int(first["entry_label"]) == 1
    assert float(first["entry_threshold_pct"]) == pytest.approx(0.0020, rel=1e-9)
    # A threshold far above the realised excursion stays negative regardless of level.
    oracle_hi = build_entry_bn_move_oracle(day, horizon_minutes=5, min_pct=0.05)
    assert int(oracle_hi.iloc[0]["entry_label"]) == 0


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


# ---------------------------------------------------------------------------
# Clean-move oracle tests
# ---------------------------------------------------------------------------


def _clean_rally_day() -> pd.DataFrame:
    """Monotone rally: each bar closes exactly 25 pts higher than the previous.

    t0 = 50000, t1..t7 = 50025, 50050, ..., 50175
    Net move over 5 bars from t0: (50125 - 50000) / 50000 = 0.25% → clears 0.20%.
    All 3 first forward bars are up → strict label = 1.
    """
    rows = []
    base = 50_000.0
    for i in range(8):
        px = base + 25.0 * i
        rows.append(
            {
                "trade_date": "2024-06-04",
                "timestamp": f"2024-06-04 10:{15 + i:02d}:00",
                "snapshot_id": f"20240604_10{15 + i:02d}",
                "px_fut_close": px,
                "px_fut_high": px + 5.0,
                "px_fut_low": px - 5.0,
                "px_fut_open": px,
            }
        )
    return pd.DataFrame(rows)


def _choppy_day() -> pd.DataFrame:
    """Up-down alternating: net move is large but no clean start.

    t0=50000, t1=50200, t2=49900, t3=50200, t4=49900, t5=50200, t6=49900, t7=50200
    Net over 5 bars (t1..t5) ends at 50200 → +0.40% but bars alternate → not clean.
    """
    closes = [50_000.0, 50_200.0, 49_900.0, 50_200.0, 49_900.0, 50_200.0, 49_900.0, 50_200.0]
    rows = []
    for i, px in enumerate(closes):
        rows.append(
            {
                "trade_date": "2024-06-05",
                "timestamp": f"2024-06-05 10:{15 + i:02d}:00",
                "snapshot_id": f"20240605_10{15 + i:02d}",
                "px_fut_close": px,
                "px_fut_high": px + 5.0,
                "px_fut_low": px - 5.0,
                "px_fut_open": px,
            }
        )
    return pd.DataFrame(rows)


def test_clean_move_strict_positive_on_clean_rally() -> None:
    oracle = build_entry_bn_clean_move_oracle(
        _clean_rally_day(), horizon_minutes=5, min_pct=0.0020, n_clean_bars=3, mode="strict"
    )
    first = oracle.iloc[0]
    assert int(first["entry_label"]) == 1
    assert int(first["entry_label_valid"]) == 1


def test_clean_move_strict_direction_agnostic() -> None:
    """A clean DOWN move must also be labelled 1."""
    rows = []
    base = 50_000.0
    for i in range(8):
        px = base - 25.0 * i
        rows.append(
            {
                "trade_date": "2024-06-06",
                "timestamp": f"2024-06-06 10:{15 + i:02d}:00",
                "snapshot_id": f"20240606_10{15 + i:02d}",
                "px_fut_close": px,
                "px_fut_high": px + 5.0,
                "px_fut_low": px - 5.0,
                "px_fut_open": px,
            }
        )
    day = pd.DataFrame(rows)
    oracle = build_entry_bn_clean_move_oracle(
        day, horizon_minutes=5, min_pct=0.0020, n_clean_bars=3, mode="strict"
    )
    assert int(oracle.iloc[0]["entry_label"]) == 1


def test_clean_move_strict_rejects_chop() -> None:
    """Choppy day: net move clears threshold but bars are not clean → label = 0 at t0."""
    oracle = build_entry_bn_clean_move_oracle(
        _choppy_day(), horizon_minutes=5, min_pct=0.0020, n_clean_bars=3, mode="strict"
    )
    assert int(oracle.iloc[0]["entry_label"]) == 0


def test_clean_move_soft_more_permissive_than_strict() -> None:
    """Soft labels >= strict labels on identical data (soft ⊇ strict)."""
    day = _choppy_day()
    oracle_strict = build_entry_bn_clean_move_oracle(
        day, horizon_minutes=5, min_pct=0.0020, n_clean_bars=3, mode="strict"
    )
    oracle_soft = build_entry_bn_clean_move_oracle(
        day, horizon_minutes=5, min_pct=0.0020, n_clean_bars=3, mode="soft"
    )
    strict_sum = oracle_strict["entry_label"].sum()
    soft_sum = oracle_soft["entry_label"].sum()
    assert soft_sum >= strict_sum


def test_clean_move_threshold_too_high_gives_zero_labels() -> None:
    oracle = build_entry_bn_clean_move_oracle(
        _clean_rally_day(), horizon_minutes=5, min_pct=0.10, n_clean_bars=3, mode="strict"
    )
    assert oracle["entry_label"].sum() == 0


def test_clean_move_invalid_rows_at_end_of_day() -> None:
    day = _clean_rally_day().iloc[-1:].copy()
    oracle = build_entry_bn_clean_move_oracle(
        day, horizon_minutes=5, min_pct=0.0020, n_clean_bars=3, mode="strict"
    )
    assert int(oracle.iloc[-1]["entry_label_valid"]) == 0


def test_stage1_clean_move_config_reads_manifest() -> None:
    manifest = {
        "labels": {
            "stage1_labeler_id": "entry_bn_clean_move_strict_v1",
            "stage1_entry_move": {"horizon_minutes": 10, "min_pct": 0.0015, "n_clean_bars": 4},
        }
    }
    cfg = stage1_clean_move_config(manifest)
    assert cfg["horizon_minutes"] == 10
    assert cfg["min_pct"] == pytest.approx(0.0015)
    assert cfg["n_clean_bars"] == 4


def test_stage1_clean_move_config_raises_without_min_pct() -> None:
    with pytest.raises(ValueError, match="min_pct"):
        stage1_clean_move_config({"labels": {"stage1_entry_move": {"horizon_minutes": 5}}})
