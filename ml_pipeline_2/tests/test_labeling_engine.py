from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd

from ml_pipeline_2.labeling.engine import EffectiveLabelConfig, label_day_futures
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
