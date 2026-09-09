"""Walk-forward fold generation and risk metrics, ported from the legacy
`model_search` package (`walk_forward.py`, `metrics.py::max_drawdown_pct`,
`event_purge.py`) during the 2026-09-09 cleanup -- these functions were
genuinely reusable and schema-agnostic (pure date/number/timestamp math,
no dependency on the legacy Kite-era feature columns), unlike the rest of
that package, which was removed. `profit_factor` was NOT ported --
`backtest_runner.BacktestSummary` already computes it inline from real
rupee P&L.

`build_day_folds` is directly relevant to the in-sample contamination
incident (project_backtest_insample_contamination_finding_2026-09-09.md):
it generates train/valid/test date ranges WITH purge and embargo gaps
built in, by construction -- had this been used to pick this session's
label-target studies' windows instead of manually-chosen dates, the
contamination would not have been possible to introduce in the first
place. `validate_backtest_window` in `validation.py` is the safety net
for when dates are chosen by hand anyway; `build_day_folds` is the
better fix upstream of that -- generate correct windows, don't just
check them after the fact.

`apply_event_overlap_purge` is a finer-grained purge than a plain day
gap: a labeled event (e.g. an option position held for N minutes) can
straddle a train/holdout boundary even when the calendar days are purged
cleanly, if the event's own end timestamp reaches past the boundary. It
drops any training row whose event window overlaps ANY holdout row's
event window, keyed off a generic `timestamp` column plus a configurable
event-end column name -- not yet wired into any live workflow, ported
here so the logic isn't lost, for use whenever a future retrain needs
event-aware (not just day-aware) purging.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd


def build_day_folds(
    days: Sequence[str],
    *,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
    purge_days: int = 0,
    embargo_days: int = 0,
) -> list[dict[str, list[str]]]:
    """Split a sorted list of trading-day strings into walk-forward folds,
    each with train/valid/test segments separated by explicit purge and
    embargo gaps (days belonging to none of the three, existing purely to
    guarantee separation).

    Example: `build_day_folds(days, train_days=90, valid_days=15,
    test_days=15, step_days=30, embargo_days=3)` produces folds where
    each test segment starts at least 3 trading days after its valid
    segment ends -- the walk-forward equivalent of
    `validation.validate_backtest_window`'s `min_gap_days`.
    """
    if train_days <= 0 or valid_days <= 0 or test_days <= 0 or step_days <= 0:
        raise ValueError("train_days, valid_days, test_days, step_days must all be > 0")
    if purge_days < 0 or embargo_days < 0:
        raise ValueError("purge_days and embargo_days must be >= 0")

    n = len(days)
    span = train_days + purge_days + valid_days + embargo_days + test_days
    folds: list[dict[str, list[str]]] = []
    start = 0
    while start + span <= n:
        train_end = start + train_days
        valid_start = train_end + purge_days
        valid_end = valid_start + valid_days
        test_start = valid_end + embargo_days
        test_end = test_start + test_days
        folds.append({
            "train_days": list(days[start:train_end]),
            "purge_days": list(days[train_end:valid_start]),
            "valid_days": list(days[valid_start:valid_end]),
            "embargo_days": list(days[valid_end:test_start]),
            "test_days": list(days[test_start:test_end]),
        })
        start += step_days
    return folds


def max_drawdown_pct(returns: Sequence[float]) -> float:
    """Max drawdown from a geometrically compounded sequence of per-trade
    or per-period returns (fractions, e.g. 0.02 for +2%)."""
    r = np.asarray(list(returns), dtype=float)
    if len(r) == 0:
        return 0.0
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    dd = (equity / np.where(peak == 0.0, 1.0, peak)) - 1.0
    return float(abs(np.nanmin(dd)))


def _coerce_timestamp_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def _resolve_event_end(work: pd.DataFrame, event_end_col: str) -> pd.Series:
    if event_end_col in work.columns:
        end_ts = _coerce_timestamp_series(work[event_end_col])
    else:
        end_ts = pd.Series(pd.NaT, index=work.index, dtype="datetime64[ns]")
    start_ts = _coerce_timestamp_series(work["timestamp"])
    return end_ts.where(end_ts.notna(), start_ts)


def apply_event_overlap_purge(
    train_df: pd.DataFrame,
    *,
    heldout_frames: Sequence[pd.DataFrame],
    event_end_col: str,
    embargo_rows: int = 0,
) -> pd.DataFrame:
    """Drop training rows whose labeled event window [timestamp, event_end]
    overlaps any holdout row's event window -- a finer-grained purge than a
    plain calendar-day gap, for labels whose event can outlast the row's
    own timestamp (e.g. an option position held for N minutes past entry).

    Rows without a `timestamp` are dropped outright (can't be purged
    safely). `event_end_col` missing/NaT falls back to the row's own
    timestamp (a zero-duration event). `embargo_rows` additionally drops
    any training row within that many median-step-sizes of the earliest
    holdout timestamp, as a second, coarser safety margin.
    """
    if len(train_df) == 0:
        return train_df.copy()
    work = train_df.copy()
    work["timestamp"] = _coerce_timestamp_series(work["timestamp"])
    work = work.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    heldout = pd.concat(
        [frame.copy() for frame in heldout_frames if frame is not None and len(frame) > 0],
        ignore_index=True,
    ) if heldout_frames else pd.DataFrame()
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
        observed_train_steps = train_start.dropna().sort_values().reset_index(drop=True)
        median_step = observed_train_steps.diff().dropna().median() if len(observed_train_steps) >= 2 else pd.Timedelta(minutes=1)
        if pd.isna(median_step) or median_step <= pd.Timedelta(0):
            median_step = pd.Timedelta(minutes=1)
        cutoff = heldout_start.min() - (median_step * embargo)
        keep_mask &= (train_end < cutoff).to_numpy(dtype=bool)
    return work.loc[keep_mask].copy().reset_index(drop=True)
