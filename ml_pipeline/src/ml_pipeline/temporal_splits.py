from dataclasses import dataclass
from typing import List, Optional, Sequence

import pandas as pd


@dataclass(frozen=True)
class TemporalPartition:
    model_days: List[str]
    holdout_days: List[str]
    evaluation_end_day: str
    model_window_start: str
    model_window_end: str


def partition_days_with_reserve(
    available_days: Sequence[str],
    *,
    lookback_years: int,
    evaluation_end_day: Optional[str] = None,
    reserve_months: int = 0,
) -> TemporalPartition:
    days = sorted(str(x) for x in available_days)
    if not days:
        raise ValueError("available_days is empty")
    if int(lookback_years) <= 0:
        raise ValueError("lookback_years must be > 0")
    if int(reserve_months) < 0:
        raise ValueError("reserve_months must be >= 0")

    latest_ts = pd.Timestamp(days[-1])
    if evaluation_end_day is not None:
        eval_end_ts = pd.Timestamp(str(evaluation_end_day))
    elif int(reserve_months) > 0:
        eval_end_ts = latest_ts - pd.DateOffset(months=int(reserve_months))
    else:
        eval_end_ts = latest_ts

    start_ts = eval_end_ts - pd.DateOffset(years=int(lookback_years)) + pd.Timedelta(days=1)
    model_days = [d for d in days if start_ts <= pd.Timestamp(d) <= eval_end_ts]
    if not model_days:
        raise ValueError("no model_days found for requested lookback/evaluation window")

    holdout_days: List[str]
    if int(reserve_months) <= 0:
        holdout_days = []
    elif evaluation_end_day is None:
        holdout_days = [d for d in days if pd.Timestamp(d) > eval_end_ts]
    else:
        holdout_end_ts = eval_end_ts + pd.DateOffset(months=int(reserve_months))
        holdout_days = [d for d in days if eval_end_ts < pd.Timestamp(d) <= holdout_end_ts]

    return TemporalPartition(
        model_days=model_days,
        holdout_days=holdout_days,
        evaluation_end_day=str(eval_end_ts.date()),
        model_window_start=str(model_days[0]),
        model_window_end=str(model_days[-1]),
    )

