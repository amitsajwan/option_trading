from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CusumSamplingConfig:
    signal_cols: tuple[str, ...] = ("opt_flow_ce_pe_oi_diff", "fut_flow_oi_change_1m")
    rolling_window: int = 30
    warmup_bars: int = 20
    collapse_bars: int = 3


def _resolve_signal_column(frame: pd.DataFrame, signal_cols: Sequence[str]) -> str | None:
    for name in signal_cols:
        if str(name) in frame.columns:
            return str(name)
    return None


def annotate_cusum_events(frame: pd.DataFrame, *, config: CusumSamplingConfig | None = None) -> tuple[pd.DataFrame, dict[str, object]]:
    cfg = config or CusumSamplingConfig()
    out = frame.copy()
    if "timestamp" in out.columns:
        out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
        out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    signal_col = _resolve_signal_column(out, cfg.signal_cols)
    out["event_sampled"] = 1.0
    out["event_sample_direction"] = 0.0
    if signal_col is None or len(out) == 0:
        return out, {"event_sampling_mode": "none", "event_signal_col": None, "event_rows": int(len(out))}
    raw_signal = pd.to_numeric(out[signal_col], errors="coerce")
    delta = raw_signal.diff()
    rolling_std = delta.rolling(int(cfg.rolling_window), min_periods=int(cfg.warmup_bars)).std(ddof=0)
    s_pos = 0.0
    s_neg = 0.0
    event_mask = np.zeros(len(out), dtype=float)
    event_direction = np.zeros(len(out), dtype=float)
    last_pos_idx = -10**9
    last_neg_idx = -10**9
    for idx, value in enumerate(delta.to_numpy(dtype=float, copy=False)):
        threshold = rolling_std.iloc[idx]
        if not np.isfinite(value) or not np.isfinite(threshold) or threshold <= 0.0:
            continue
        s_pos = max(0.0, s_pos + float(value))
        s_neg = min(0.0, s_neg + float(value))
        if s_pos >= float(threshold):
            if (idx - last_pos_idx) > int(cfg.collapse_bars):
                event_mask[idx] = 1.0
                event_direction[idx] = 1.0
                last_pos_idx = idx
            s_pos = 0.0
            s_neg = 0.0
            continue
        if s_neg <= -float(threshold):
            if (idx - last_neg_idx) > int(cfg.collapse_bars):
                event_mask[idx] = 1.0
                event_direction[idx] = -1.0
                last_neg_idx = idx
            s_pos = 0.0
            s_neg = 0.0
    out["event_sampled"] = event_mask
    out["event_sample_direction"] = event_direction
    return out, {"event_sampling_mode": "cusum", "event_signal_col": str(signal_col), "event_rows": int(np.sum(event_mask)), "rows_total": int(len(out))}
