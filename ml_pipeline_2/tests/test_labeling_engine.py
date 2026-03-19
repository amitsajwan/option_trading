from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd

from ml_pipeline_2.labeling.engine import (
    EffectiveLabelConfig,
    _compute_futures_trade_metrics,
    _move_first_hit_side,
    label_day_futures,
)
from ml_pipeline_2.tests.helpers import build_synthetic_feature_frames


def test_label_day_futures_avoids_fragmentation_warning(tmp_path: Path) -> None:
    model_window_path, _ = build_synthetic_feature_frames(tmp_path)
    features = pd.read_parquet(model_window_path)
    one_day = features[features["trade_date"] == "2024-01-01"].copy().reset_index(drop=True)
    for idx in range(80):
        one_day[f"extra_feature_{idx}"] = float(idx)
    cfg = EffectiveLabelConfig(
        horizon_minutes=2,
        return_threshold=0.0,
        use_excursion_gate=False,
        min_favorable_excursion=0.0,
        max_adverse_excursion=0.0,
        take_profit_pct=0.0010,
        stop_loss_pct=0.0005,
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        labeled = label_day_futures(one_day, cfg)

    perf_warnings = [warning for warning in caught if issubclass(warning.category, pd.errors.PerformanceWarning)]
    assert not perf_warnings
    for col in ("long_label", "short_label", "ce_label", "pe_label", "move_label", "best_side_label"):
        assert col in labeled.columns


def test_compute_futures_trade_metrics_prefers_same_bar_resolution() -> None:
    timestamps = pd.date_range("2024-01-01 09:15:00", periods=2, freq="min")
    symbol_table = pd.DataFrame(
        {
            "timestamp": timestamps,
            "fut_open": [100.0, 100.0],
            "fut_high": [101.5, 100.5],
            "fut_low": [98.5, 99.5],
            "fut_close": [100.0, 100.0],
        }
    ).set_index("timestamp", drop=False)
    cfg = EffectiveLabelConfig(
        horizon_minutes=1,
        return_threshold=0.0,
        use_excursion_gate=False,
        min_favorable_excursion=0.0,
        max_adverse_excursion=0.0,
        take_profit_pct=0.01,
        stop_loss_pct=0.01,
    )

    metrics = _compute_futures_trade_metrics(
        symbol_table,
        timestamps[0] - pd.Timedelta(minutes=1),
        cfg.horizon_minutes,
        cfg,
        side="long",
        feature_row={},
    )

    assert metrics["tp_hit"] == 1.0
    assert metrics["sl_hit"] == 1.0
    assert metrics["first_hit"] == "tp_sl_same_bar"
    assert metrics["first_hit_offset_min"] == 0.0
    assert metrics["event_end_ts"] == timestamps[0]


def test_move_first_hit_side_maps_barrier_hits_to_price_direction() -> None:
    assert _move_first_hit_side("tp", "time_stop") == "up"
    assert _move_first_hit_side("time_stop", "sl") == "up"
    assert _move_first_hit_side("sl", "time_stop") == "down"
    assert _move_first_hit_side("time_stop", "tp") == "down"
