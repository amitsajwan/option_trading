"""Stage-1 entry oracle from underlying (BankNifty futures) short-horizon move.

Label = 1 when within ``horizon_minutes`` the futures price moves at least
``min_points`` in *either* direction, expressed as a fraction of entry price:

    threshold_pct = min_points / entry_price
    up_move_pct   = (max_high - entry) / entry
    down_move_pct = (entry - min_low) / entry
    entry_label   = 1 if max(up_move_pct, down_move_pct) >= threshold_pct
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..dataset_windowing import normalize_trade_date

KEY_COLUMNS = ["trade_date", "timestamp", "snapshot_id"]


def _ensure_fut_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    mapping = {
        "fut_open": "px_fut_open",
        "fut_high": "px_fut_high",
        "fut_low": "px_fut_low",
        "fut_close": "px_fut_close",
    }
    for legacy, snapshot in mapping.items():
        if legacy not in out.columns and snapshot in out.columns:
            out[legacy] = pd.to_numeric(out[snapshot], errors="coerce")
    return out


def _label_day_moves(
    day: pd.DataFrame,
    *,
    horizon_minutes: int,
    min_points: float,
    min_pct: float | None = None,
) -> pd.DataFrame:
    work = day.sort_values("timestamp").reset_index(drop=True)
    n = len(work)
    entry = pd.to_numeric(work["fut_close"], errors="coerce").to_numpy(dtype=float, copy=False)
    highs = pd.to_numeric(work["fut_high"], errors="coerce").to_numpy(dtype=float, copy=False)
    lows = pd.to_numeric(work["fut_low"], errors="coerce").to_numpy(dtype=float, copy=False)

    labels = np.zeros(n, dtype=np.int8)
    valid = np.zeros(n, dtype=np.int8)
    up_move = np.full(n, np.nan, dtype=float)
    down_move = np.full(n, np.nan, dtype=float)
    threshold_pct = np.full(n, np.nan, dtype=float)

    horizon = max(1, int(horizon_minutes))
    # min_pct (a price fraction, e.g. 0.0010 == 0.10%) takes precedence over
    # min_points when supplied, so the label is level-invariant across the
    # 2022->2024->2026 index drift. min_points stays as the legacy fallback.
    use_pct = min_pct is not None
    pct_thr = float(min_pct) if use_pct else 0.0
    min_pts = float(min_points)
    if use_pct:
        if pct_thr <= 0.0:
            raise ValueError("min_pct must be positive")
    elif min_pts <= 0.0:
        raise ValueError("min_points must be positive")

    for i in range(n):
        px = entry[i]
        if not np.isfinite(px) or px <= 0.0:
            continue
        end = min(n, i + horizon + 1)
        if end <= i + 1:
            continue
        fwd_high = np.nanmax(highs[i + 1 : end])
        fwd_low = np.nanmin(lows[i + 1 : end])
        if not np.isfinite(fwd_high) or not np.isfinite(fwd_low):
            continue
        thr = pct_thr if use_pct else min_pts / px
        up = (fwd_high - px) / px
        down = (px - fwd_low) / px
        valid[i] = 1
        up_move[i] = up
        down_move[i] = down
        threshold_pct[i] = thr
        if max(up, down) >= thr:
            labels[i] = 1

    out = work.loc[:, KEY_COLUMNS].copy()
    out["entry_label"] = labels.astype(int)
    out["entry_label_valid"] = valid.astype(int)
    out["entry_up_move_pct"] = up_move
    out["entry_down_move_pct"] = down_move
    out["entry_threshold_pct"] = threshold_pct
    direction_up = np.where(
        np.isfinite(up_move) & np.isfinite(down_move),
        np.where(up_move >= down_move, 1, 0),
        np.nan,
    )
    out["direction_label"] = np.where(
        labels == 1,
        np.where(direction_up == 1, "CE", "PE"),
        None,
    )
    out["direction_up"] = direction_up
    return out


def build_entry_bn_move_oracle(
    support: pd.DataFrame,
    *,
    horizon_minutes: int = 5,
    min_points: float = 100.0,
    min_pct: float | None = None,
) -> pd.DataFrame:
    """Build stage-1 entry oracle from futures 5m excursion (points → % of price).

    When ``min_pct`` (a price fraction, e.g. 0.0010 == 0.10%) is supplied it
    defines the move threshold directly and ``min_points`` is ignored, keeping
    label difficulty constant across index levels.
    """
    if bool(support.duplicated(subset=KEY_COLUMNS).any()):
        raise ValueError("support frame contains duplicate staged oracle keys")

    required = KEY_COLUMNS + ["fut_close"]
    frame = _ensure_fut_columns(support)
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise ValueError(f"entry move oracle requires columns: {missing}")

    frame = frame.copy()
    frame["trade_date"] = normalize_trade_date(frame["trade_date"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values(["trade_date", "timestamp"])

    day_frames = [
        _label_day_moves(
            day,
            horizon_minutes=horizon_minutes,
            min_points=min_points,
            min_pct=min_pct,
        )
        for _, day in frame.groupby("trade_date", sort=False)
    ]
    if not day_frames:
        return pd.DataFrame(columns=KEY_COLUMNS + ["entry_label", "entry_label_valid"])

    oracle = pd.concat(day_frames, ignore_index=True)
    oracle["recipe_label"] = None
    oracle["best_net_return_after_cost"] = np.nan
    return oracle


def merge_recipe_utility_with_entry_move_oracle(
    recipe_oracle: pd.DataFrame,
    utility: pd.DataFrame,
    move_oracle: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Replace ``entry_label`` with move-based labels; keep recipe returns for economic eval."""
    move_cols = [
        "entry_label",
        "entry_label_valid",
        "entry_up_move_pct",
        "entry_down_move_pct",
        "entry_threshold_pct",
        "direction_label",
        "direction_up",
    ]
    base = recipe_oracle.drop(columns=[c for c in move_cols if c in recipe_oracle.columns], errors="ignore")
    merged_oracle = base.merge(
        move_oracle.loc[:, KEY_COLUMNS + move_cols],
        on=KEY_COLUMNS,
        how="left",
        validate="one_to_one",
    )
    merged_oracle["entry_label"] = (
        pd.to_numeric(merged_oracle["entry_label"], errors="coerce").fillna(0).astype(int)
    )
    merged_oracle["entry_label_valid"] = (
        pd.to_numeric(merged_oracle.get("entry_label_valid"), errors="coerce").fillna(0).astype(int)
    )
    utility_out = utility.copy()
    for col in ("entry_up_move_pct", "entry_down_move_pct", "entry_threshold_pct"):
        if col in move_oracle.columns:
            utility_out = utility_out.merge(
                move_oracle.loc[:, KEY_COLUMNS + [col]],
                on=KEY_COLUMNS,
                how="left",
            )
    return merged_oracle, utility_out


def stage1_entry_move_config(manifest: dict[str, Any]) -> dict[str, Any]:
    raw = dict((manifest.get("labels") or {}).get("stage1_entry_move") or {})
    min_pct_raw = raw.get("min_pct")
    return {
        "horizon_minutes": int(raw.get("horizon_minutes", 5)),
        "min_points": float(raw.get("min_points", 100.0)),
        "min_pct": (float(min_pct_raw) if min_pct_raw is not None else None),
    }
