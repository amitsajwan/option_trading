from __future__ import annotations

import pandas as pd

from ml_pipeline_2.staged.robustness import bootstrap_binary_scores_by_unit


def test_bootstrap_binary_scores_by_unit_reports_metric_distribution() -> None:
    frame = pd.DataFrame(
        {
            "trade_date": [
                "2024-01-01",
                "2024-01-01",
                "2024-01-02",
                "2024-01-02",
                "2024-01-03",
                "2024-01-03",
            ],
            "direction_binary": [1, 0, 1, 0, 1, 0],
            "direction_up_prob": [0.80, 0.20, 0.75, 0.30, 0.70, 0.35],
        }
    )

    report = bootstrap_binary_scores_by_unit(
        frame,
        label_col="direction_binary",
        prob_col="direction_up_prob",
        iterations=40,
        random_seed=11,
        roc_auc_min=0.55,
        brier_max=0.22,
    )

    assert report["resample_unit"] == "trade_date"
    assert report["units_total"] == 3
    assert report["rows_total"] == 6
    assert report["base_quality"]["roc_auc"] == 1.0
    assert report["bootstrap_metrics"]["roc_auc"]["count"] == 40
    assert report["bootstrap_metrics"]["brier"]["count"] == 40
    assert report["gate_pass_rate"] is not None
