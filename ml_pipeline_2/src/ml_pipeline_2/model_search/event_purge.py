from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd


PURGE_MODE_DAYS = "days"
PURGE_MODE_EVENT_OVERLAP = "event_overlap"
PURGE_MODE_CHOICES = (PURGE_MODE_DAYS, PURGE_MODE_EVENT_OVERLAP)


def normalize_purge_mode(value: object) -> str:
    mode = str(value or PURGE_MODE_DAYS).strip().lower() or PURGE_MODE_DAYS
    if mode not in PURGE_MODE_CHOICES:
        raise ValueError(f"unsupported purge_mode: {value}")
    return mode


def _coerce_timestamp_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def _resolve_event_end(work: pd.DataFrame, event_end_col: str) -> pd.Series:
    if event_end_col in work.columns:
        end_ts = _coerce_timestamp_series(work[event_end_col])
    else:
        end_ts = pd.Series(pd.NaT, index=work.index, dtype="datetime64[ns]")
    start_ts = _coerce_timestamp_series(work["timestamp"])
    return end_ts.where(end_ts.notna(), start_ts)


def apply_event_overlap_purge(train_df: pd.DataFrame, *, heldout_frames: Sequence[pd.DataFrame], event_end_col: str, embargo_rows: int = 0) -> pd.DataFrame:
    if len(train_df) == 0:
        return train_df.copy()
    work = train_df.copy()
    work["timestamp"] = _coerce_timestamp_series(work["timestamp"])
    work = work.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    heldout = pd.concat([frame.copy() for frame in heldout_frames if frame is not None and len(frame) > 0], ignore_index=True)
    if len(heldout) == 0:
        return work
    heldout["timestamp"] = _coerce_timestamp_series(heldout["timestamp"])
    heldout = heldout.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    train_start = _coerce_timestamp_series(work["timestamp"])
    train_end = _resolve_event_end(work, event_end_col=event_end_col)
    heldout_start = _coerce_timestamp_series(heldout["timestamp"])
    heldout_end = _resolve_event_end(heldout, event_end_col=event_end_col)
    keep_mask = np.ones(len(work), dtype=bool)
    for idx in range(len(heldout)):
        overlap = (train_start <= heldout_end.iloc[idx]) & (train_end >= heldout_start.iloc[idx])
        keep_mask &= ~overlap.to_numpy(dtype=bool)
    embargo = max(0, int(embargo_rows))
    if embargo > 0 and len(heldout_start) > 0:
        combined_ts = pd.concat([train_start, heldout_start], ignore_index=True).dropna().sort_values().reset_index(drop=True)
        median_step = combined_ts.diff().dropna().median() if len(combined_ts) >= 2 else pd.Timedelta(minutes=1)
        if pd.isna(median_step) or median_step <= pd.Timedelta(0):
            median_step = pd.Timedelta(minutes=1)
        cutoff = heldout_start.min() - (median_step * embargo)
        keep_mask &= (train_end < cutoff).to_numpy(dtype=bool)
    return work.loc[keep_mask].copy().reset_index(drop=True)


def infer_side_event_end_col(df: pd.DataFrame, *, side: str, fallback: str | None = None) -> str:
    side_col = f"{str(side).lower()}_event_end_ts"
    if side_col in df.columns:
        return side_col
    if fallback and fallback in df.columns:
        return str(fallback)
    return side_col
