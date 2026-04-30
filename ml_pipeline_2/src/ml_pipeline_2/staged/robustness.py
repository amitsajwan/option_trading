from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score


def _safe_metric_summary(values: list[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "std": None,
            "min": None,
            "p05": None,
            "p50": None,
            "p95": None,
            "max": None,
        }
    arr = np.asarray(values, dtype=float)
    return {
        "count": int(len(arr)),
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr, ddof=0)),
        "min": float(np.min(arr)),
        "p05": float(np.quantile(arr, 0.05)),
        "p50": float(np.quantile(arr, 0.50)),
        "p95": float(np.quantile(arr, 0.95)),
        "max": float(np.max(arr)),
    }


def _binary_quality(y_true: np.ndarray, y_prob: np.ndarray) -> Dict[str, Optional[float]]:
    if len(y_true) == 0:
        return {"roc_auc": None, "brier": None}
    roc_auc = None
    if len(np.unique(y_true)) >= 2:
        roc_auc = float(roc_auc_score(y_true, y_prob))
    return {
        "roc_auc": roc_auc,
        "brier": float(brier_score_loss(y_true, y_prob)),
    }


def bootstrap_binary_scores_by_unit(
    frame: pd.DataFrame,
    *,
    label_col: str,
    prob_col: str,
    unit_col: str = "trade_date",
    iterations: int = 200,
    random_seed: int = 42,
    roc_auc_min: Optional[float] = None,
    brier_max: Optional[float] = None,
) -> Dict[str, Any]:
    usable = frame.loc[:, [unit_col, label_col, prob_col]].copy()
    usable[label_col] = pd.to_numeric(usable[label_col], errors="coerce")
    usable[prob_col] = pd.to_numeric(usable[prob_col], errors="coerce")
    usable = usable.dropna(subset=[unit_col, label_col, prob_col]).reset_index(drop=True)
    if usable.empty:
        return {
            "resample_unit": unit_col,
            "iterations": int(iterations),
            "units_total": 0,
            "rows_total": 0,
            "base_quality": {"roc_auc": None, "brier": None},
            "bootstrap_metrics": {
                "roc_auc": _safe_metric_summary([]),
                "brier": _safe_metric_summary([]),
            },
            "gate_pass_rate": None,
        }

    grouped = {str(unit): group.index.to_numpy(dtype=int) for unit, group in usable.groupby(unit_col, sort=True)}
    units = np.array(list(grouped.keys()), dtype=object)
    y = usable[label_col].to_numpy(dtype=float).astype(int)
    p = usable[prob_col].to_numpy(dtype=float)
    base_quality = _binary_quality(y, p)
    rng = np.random.default_rng(int(random_seed))
    roc_samples: list[float] = []
    brier_samples: list[float] = []
    gate_passes = 0
    gate_trials = 0

    for _ in range(int(iterations)):
        sampled_units = rng.choice(units, size=len(units), replace=True)
        sampled_indices = np.concatenate([grouped[str(unit)] for unit in sampled_units])
        sample_quality = _binary_quality(y[sampled_indices], p[sampled_indices])
        roc_auc = sample_quality["roc_auc"]
        brier = sample_quality["brier"]
        if roc_auc is not None:
            roc_samples.append(float(roc_auc))
        if brier is not None:
            brier_samples.append(float(brier))
        if roc_auc_min is not None and brier_max is not None and roc_auc is not None and brier is not None:
            gate_trials += 1
            if float(roc_auc) >= float(roc_auc_min) and float(brier) <= float(brier_max):
                gate_passes += 1

    return {
        "resample_unit": unit_col,
        "iterations": int(iterations),
        "units_total": int(len(units)),
        "rows_total": int(len(usable)),
        "base_quality": base_quality,
        "bootstrap_metrics": {
            "roc_auc": _safe_metric_summary(roc_samples),
            "brier": _safe_metric_summary(brier_samples),
        },
        "gate_pass_rate": (float(gate_passes / gate_trials) if gate_trials > 0 else None),
    }


def bootstrap_stage2_scores_from_parquet(
    score_path: str | Path,
    *,
    iterations: int = 200,
    random_seed: int = 42,
    roc_auc_min: Optional[float] = None,
    brier_max: Optional[float] = None,
) -> Dict[str, Any]:
    frame = pd.read_parquet(Path(score_path))
    return bootstrap_binary_scores_by_unit(
        frame,
        label_col="direction_binary",
        prob_col="direction_up_prob",
        unit_col="trade_date",
        iterations=iterations,
        random_seed=random_seed,
        roc_auc_min=roc_auc_min,
        brier_max=brier_max,
    )


__all__ = [
    "bootstrap_binary_scores_by_unit",
    "bootstrap_stage2_scores_from_parquet",
]
