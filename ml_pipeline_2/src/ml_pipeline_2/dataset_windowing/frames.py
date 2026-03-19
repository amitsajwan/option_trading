from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pandas as pd


def normalize_trade_date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.strftime("%Y-%m-%d")


def load_feature_frame(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    if "timestamp" not in frame.columns:
        raise ValueError(f"feature parquet missing timestamp column: {path}")
    out = frame.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    if "trade_date" not in out.columns:
        out["trade_date"] = out["timestamp"].dt.strftime("%Y-%m-%d")
    else:
        out["trade_date"] = normalize_trade_date(out["trade_date"])
    return out


def filter_trade_dates(frame: pd.DataFrame, start_day: str, end_day: str) -> pd.DataFrame:
    out = frame.copy()
    # Defensive normalization: most callers already pass normalized frames, but legacy entrypoints do not.
    out["trade_date"] = normalize_trade_date(out["trade_date"])
    mask = out["trade_date"].notna() & (out["trade_date"] >= str(start_day)) & (out["trade_date"] <= str(end_day))
    return out.loc[mask].copy().sort_values("timestamp").reset_index(drop=True)


def window_metadata(frame: pd.DataFrame, *, start_day: str, end_day: str) -> Dict[str, Any]:
    return {
        "start": str(start_day),
        "end": str(end_day),
        "rows": int(len(frame)),
        "days": int(frame["trade_date"].nunique()) if "trade_date" in frame.columns else 0,
        "timestamp_min": (str(frame["timestamp"].min()) if len(frame) else None),
        "timestamp_max": (str(frame["timestamp"].max()) if len(frame) else None),
    }


def path_contains(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def paths_overlap(a: Path, b: Path) -> bool:
    ra = a.resolve()
    rb = b.resolve()
    return ra == rb or path_contains(ra, rb) or path_contains(rb, ra)

