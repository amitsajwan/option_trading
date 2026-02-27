from typing import List, Sequence, Tuple

import numpy as np
import pandas as pd

from ..train_baseline import IDENTITY_COLUMNS, LABEL_COLUMNS


_LABEL_LEAK_PREFIXES = (
    # Base label columns
    "ce_label", "pe_label",
    "ce_forward_return", "pe_forward_return",
    "ce_path_exit_reason", "pe_path_exit_reason",
    # Horizon-suffixed variants (e.g. ce_label_h15m, ce_forward_return_h15m)
    # Matched by prefix so any _hNm suffix is caught automatically.
)


def _is_label_leak_column(col: str) -> bool:
    """Return True if the column is a label or forward-return variant that must
    never appear in the model feature set (including horizon-suffixed forms)."""
    c = str(col)
    return any(c == prefix or c.startswith(f"{prefix}_") for prefix in _LABEL_LEAK_PREFIXES)


def prepare_model_frame(frame: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    out = frame.copy()
    if "timestamp" in out.columns:
        out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
        out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    # Exclude identity/label columns AND any horizon-suffixed label/return variants.
    # Without the prefix check, columns like ce_label_h15m or ce_forward_return_h15m
    # would pass through as numeric features → severe target leakage.
    base_excluded = set(IDENTITY_COLUMNS) | set(LABEL_COLUMNS)
    selected = [
        c for c in out.select_dtypes(include=[np.number]).columns
        if c not in base_excluded and not _is_label_leak_column(c)
    ]
    if not selected:
        raise ValueError(
            "no model feature columns found in input frame. "
            "Run feature stage first: python -m ml_pipeline.feature.stage"
        )
    return out, selected


def build_xy(
    frame: pd.DataFrame,
    *,
    feature_columns: Sequence[str],
    label_col: str,
    label_valid_col: str,
) -> Tuple[pd.DataFrame, np.ndarray]:
    if label_col not in frame.columns or label_valid_col not in frame.columns:
        raise ValueError(
            f"missing label columns: {label_col}/{label_valid_col}. "
            "Run feature stage on labeled splits first: python -m ml_pipeline.feature.stage"
        )
    work = frame[(pd.to_numeric(frame[label_valid_col], errors="coerce") == 1.0) & frame[label_col].notna()].copy()
    if len(work) == 0:
        raise ValueError(f"empty training rows for {label_col}")
    x = work.loc[:, list(feature_columns)].copy()
    y = pd.to_numeric(work[label_col], errors="coerce").fillna(0).astype(int).to_numpy()
    return x, y
