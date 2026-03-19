from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .regime import summarize_regimes


@dataclass(frozen=True)
class EffectiveLabelConfig:
    horizon_minutes: int
    return_threshold: float
    use_excursion_gate: bool
    min_favorable_excursion: float
    max_adverse_excursion: float
    stop_loss_pct: float = 0.12
    take_profit_pct: float = 0.24
    allow_hold_extension: bool = False
    extension_trigger_profit_pct: float = 0.0
    barrier_mode: str = "fixed"
    atr_reference_col: str = "osc_atr_ratio"
    atr_tp_multiplier: Optional[float] = None
    atr_sl_multiplier: Optional[float] = None
    atr_clip_min_factor: float = 0.5
    atr_clip_max_factor: float = 1.5
    neutral_policy: str = "exclude_from_primary"
    event_sampling_mode: str = "none"
    event_signal_col: Optional[str] = "opt_flow_ce_pe_oi_diff"
    event_end_ts_mode: str = "first_touch_or_vertical"


def _trade_window(decision_ts: pd.Timestamp, horizon_minutes: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    return decision_ts + pd.Timedelta(minutes=1), decision_ts + pd.Timedelta(minutes=int(horizon_minutes))


def _empty_metrics() -> Dict[str, object]:
    return {
        "entry_price": np.nan,
        "exit_price": np.nan,
        "forward_return": np.nan,
        "mfe": np.nan,
        "mae": np.nan,
        "label_valid": 0.0,
        "tp_hit": 0.0,
        "sl_hit": 0.0,
        "first_hit": "none",
        "first_hit_offset_min": np.nan,
        "path_exit_reason": "invalid",
        "tp_price": np.nan,
        "sl_price": np.nan,
        "time_stop_exit": 0.0,
        "hold_extension_eligible": 0.0,
        "triple_barrier_state": np.nan,
        "barrier_upper_return": np.nan,
        "barrier_lower_return": np.nan,
        "event_end_ts": pd.NaT,
    }


def _scalar_numeric(value: object) -> float:
    try:
        return float(value)
    except Exception:
        return float("nan")


def _row_value(row: object, key: str) -> object:
    if isinstance(row, pd.Series):
        return row.get(key)
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _triple_barrier_state(path_exit_reason: object) -> float:
    reason = str(path_exit_reason or "").strip().lower()
    if reason in {"tp", "tp_sl_same_bar"}:
        return 1.0
    if reason == "sl":
        return -1.0
    if reason == "time_stop":
        return 0.0
    return np.nan


def _resolve_atr_reference_return(row: object, *, cfg: EffectiveLabelConfig, entry_price: float) -> float:
    for candidate in (str(cfg.atr_reference_col or "").strip(), "osc_atr_ratio", "atr_ratio"):
        if candidate:
            value = _scalar_numeric(_row_value(row, candidate))
            if np.isfinite(value) and value > 0.0:
                return float(value)
    for candidate in ("osc_atr_14", "atr_14"):
        value = _scalar_numeric(_row_value(row, candidate))
        if np.isfinite(value) and value > 0.0 and entry_price > 0.0:
            return float(value / entry_price)
    return float("nan")


def _resolve_barrier_returns(row: object, *, entry_price: float, cfg: EffectiveLabelConfig) -> tuple[float, float]:
    base_upper = max(0.0, float(cfg.take_profit_pct))
    base_lower = max(0.0, float(cfg.stop_loss_pct))
    if str(cfg.barrier_mode or "fixed").strip().lower() != "atr_scaled":
        return float(base_upper), float(base_lower)
    atr_return = _resolve_atr_reference_return(row, cfg=cfg, entry_price=float(entry_price))
    if not np.isfinite(atr_return) or atr_return <= 0.0:
        return float(base_upper), float(base_lower)
    upper = float(atr_return * (float(cfg.atr_tp_multiplier) if cfg.atr_tp_multiplier is not None else 1.0))
    lower = float(atr_return * (float(cfg.atr_sl_multiplier) if cfg.atr_sl_multiplier is not None else 1.0))
    return (
        float(np.clip(upper, base_upper * float(cfg.atr_clip_min_factor), base_upper * float(cfg.atr_clip_max_factor))) if base_upper > 0.0 else upper,
        float(np.clip(lower, base_lower * float(cfg.atr_clip_min_factor), base_lower * float(cfg.atr_clip_max_factor))) if base_lower > 0.0 else lower,
    )


def _compute_futures_trade_metrics(symbol_table: pd.DataFrame, decision_ts: pd.Timestamp, horizon_minutes: int, cfg: EffectiveLabelConfig, *, side: str, feature_row: object) -> Dict[str, object]:
    entry_ts, exit_ts = _trade_window(decision_ts, horizon_minutes=horizon_minutes)
    if entry_ts not in symbol_table.index or exit_ts not in symbol_table.index:
        return _empty_metrics()
    entry_price = _scalar_numeric(symbol_table.loc[entry_ts, "fut_open"])
    exit_price = _scalar_numeric(symbol_table.loc[exit_ts, "fut_close"])
    if not np.isfinite(entry_price) or entry_price <= 0.0 or not np.isfinite(exit_price):
        return _empty_metrics()
    upper_return, lower_return = _resolve_barrier_returns(feature_row, entry_price=float(entry_price), cfg=cfg)
    window = symbol_table.loc[entry_ts:exit_ts]
    if window.empty:
        return _empty_metrics()
    highs = pd.to_numeric(window["fut_high"], errors="coerce").to_numpy(dtype=float, copy=False)
    lows = pd.to_numeric(window["fut_low"], errors="coerce").to_numpy(dtype=float, copy=False)
    window_timestamps = pd.to_datetime(window["timestamp"], errors="coerce")
    max_high = float(window["fut_high"].max())
    min_low = float(window["fut_low"].min())
    is_long = str(side).lower() == "long"
    if is_long:
        forward_return = (exit_price - entry_price) / entry_price
        mfe = (max_high - entry_price) / entry_price
        mae = (min_low - entry_price) / entry_price
        tp_price = entry_price * (1.0 + float(upper_return))
        sl_price = entry_price * (1.0 - float(lower_return))
        hit_tp = np.isfinite(highs) & (highs >= tp_price)
        hit_sl = np.isfinite(lows) & (lows <= sl_price)
    else:
        forward_return = (entry_price - exit_price) / entry_price
        mfe = (entry_price - min_low) / entry_price
        mae = (entry_price - max_high) / entry_price
        tp_price = entry_price * (1.0 - float(upper_return))
        sl_price = entry_price * (1.0 + float(lower_return))
        hit_tp = np.isfinite(lows) & (lows <= tp_price)
        hit_sl = np.isfinite(highs) & (highs >= sl_price)
    tp_hit = 0.0
    sl_hit = 0.0
    first_hit = "none"
    first_hit_offset = np.nan
    event_end_ts = exit_ts
    first_hit_candidates = np.flatnonzero(hit_tp | hit_sl)
    if first_hit_candidates.size:
        first_idx = int(first_hit_candidates[0])
        first_hit_offset = float(first_idx)
        event_end_ts = pd.Timestamp(window_timestamps.iloc[first_idx])
        first_bar_tp = bool(hit_tp[first_idx])
        first_bar_sl = bool(hit_sl[first_idx])
        if first_bar_tp and first_bar_sl:
            tp_hit = 1.0
            sl_hit = 1.0
            first_hit = "tp_sl_same_bar"
        elif first_bar_tp:
            tp_hit = 1.0
            first_hit = "tp"
        else:
            sl_hit = 1.0
            first_hit = "sl"
    time_stop_exit = 1.0 if first_hit == "none" else 0.0
    path_exit_reason = first_hit if first_hit != "none" else "time_stop"
    return {
        "entry_price": entry_price,
        "exit_price": exit_price,
        "forward_return": float(forward_return),
        "mfe": float(mfe),
        "mae": float(mae),
        "label_valid": 1.0,
        "tp_hit": tp_hit,
        "sl_hit": sl_hit,
        "first_hit": path_exit_reason,
        "first_hit_offset_min": first_hit_offset,
        "path_exit_reason": path_exit_reason,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "time_stop_exit": time_stop_exit,
        "hold_extension_eligible": 0.0,
        "triple_barrier_state": _triple_barrier_state(path_exit_reason),
        "barrier_upper_return": float(upper_return),
        "barrier_lower_return": float(lower_return),
        "event_end_ts": event_end_ts if str(cfg.event_end_ts_mode) == "first_touch_or_vertical" else exit_ts,
    }


def _path_label_from_reason(reason: object) -> float:
    txt = str(reason or "").strip().lower()
    if txt in {"tp", "tp_sl_same_bar"}:
        return 1.0
    if txt in {"sl", "time_stop"}:
        return 0.0
    return np.nan


def _move_label_from_reasons(long_reason: object, short_reason: object) -> float:
    long_txt = str(long_reason or "").strip().lower()
    short_txt = str(short_reason or "").strip().lower()
    if long_txt == "invalid" or short_txt == "invalid":
        return np.nan
    if long_txt == "time_stop" and short_txt == "time_stop":
        return 0.0
    if long_txt in {"tp", "sl", "tp_sl_same_bar"} or short_txt in {"tp", "sl", "tp_sl_same_bar"}:
        return 1.0
    return np.nan


def _move_first_hit_side(long_reason: object, short_reason: object) -> str:
    long_txt = str(long_reason or "").strip().lower()
    short_txt = str(short_reason or "").strip().lower()
    if long_txt == "invalid" or short_txt == "invalid":
        return "invalid"
    # This label tracks futures direction, not option side. A short-path stop means price moved up
    # through the short barrier; a long-path stop means price moved down through the long barrier.
    if long_txt in {"tp", "tp_sl_same_bar"} or short_txt == "sl":
        return "up"
    if short_txt in {"tp", "tp_sl_same_bar"} or long_txt == "sl":
        return "down"
    if long_txt == "time_stop" and short_txt == "time_stop":
        return "none"
    return "invalid"


_RESOLVED_PATH_REASONS = frozenset({"tp", "sl", "tp_sl_same_bar"})
_SIDE_LABEL_COLUMNS = (
    "entry_price",
    "exit_price",
    "forward_return",
    "mfe",
    "mae",
    "label_valid",
    "label",
    "tp_hit",
    "sl_hit",
    "first_hit",
    "first_hit_offset_min",
    "path_exit_reason",
    "tp_price",
    "sl_price",
    "time_stop_exit",
    "hold_extension_eligible",
    "triple_barrier_state",
    "barrier_upper_return",
    "barrier_lower_return",
    "event_end_ts",
    "path_target_valid",
)


def _prepare_side_label_frame(side_df: pd.DataFrame) -> pd.DataFrame:
    out = side_df.copy()
    reason = out["path_exit_reason"].astype(str)
    valid = pd.to_numeric(out["label_valid"], errors="coerce").fillna(0.0)
    out["label"] = reason.map(_path_label_from_reason)
    out["path_target_valid"] = ((valid == 1.0) & reason.str.lower().isin(_RESOLVED_PATH_REASONS)).astype(float)
    return out.loc[:, list(_SIDE_LABEL_COLUMNS)]


def label_day_futures(features_day: pd.DataFrame, cfg: EffectiveLabelConfig) -> pd.DataFrame:
    out = features_day.sort_values("timestamp").copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    for legacy_col, snapshot_col in (("fut_open", "px_fut_open"), ("fut_high", "px_fut_high"), ("fut_low", "px_fut_low"), ("fut_close", "px_fut_close")):
        if legacy_col not in out.columns and snapshot_col in out.columns:
            out[legacy_col] = pd.to_numeric(out[snapshot_col], errors="coerce")
    required = ["fut_close", "timestamp"]
    missing = [name for name in required if name not in out.columns]
    if missing:
        raise ValueError(f"futures labeling requires columns: {missing}")
    if "fut_open" not in out.columns:
        out["fut_open"] = out["fut_close"]
    if "fut_high" not in out.columns:
        out["fut_high"] = out["fut_close"]
    if "fut_low" not in out.columns:
        out["fut_low"] = out["fut_close"]
    table = out.sort_values("timestamp").set_index("timestamp", drop=False)
    long_rows: List[Dict[str, object]] = []
    short_rows: List[Dict[str, object]] = []
    for row in out.itertuples(index=False):
        decision_ts = pd.Timestamp(row.timestamp)
        long_rows.append(_compute_futures_trade_metrics(table, decision_ts, cfg.horizon_minutes, cfg, side="long", feature_row=row))
        short_rows.append(_compute_futures_trade_metrics(table, decision_ts, cfg.horizon_minutes, cfg, side="short", feature_row=row))
    long_df = _prepare_side_label_frame(pd.DataFrame(long_rows))
    short_df = _prepare_side_label_frame(pd.DataFrame(short_rows))
    long_prefixed = long_df.add_prefix("long_")
    short_prefixed = short_df.add_prefix("short_")
    ce_prefixed = long_prefixed.rename(columns=lambda col: col.replace("long_", "ce_", 1))
    pe_prefixed = short_prefixed.rename(columns=lambda col: col.replace("short_", "pe_", 1))

    long_reason = long_df["path_exit_reason"].astype(str)
    short_reason = short_df["path_exit_reason"].astype(str)
    move_label = [
        _move_label_from_reasons(long_reason_value, short_reason_value)
        for long_reason_value, short_reason_value in zip(long_reason, short_reason)
    ]
    move_path_exit_reason = [
        _move_first_hit_side(long_reason_value, short_reason_value)
        for long_reason_value, short_reason_value in zip(long_reason, short_reason)
    ]
    move_label_valid = (
        (pd.to_numeric(long_df["label_valid"], errors="coerce").fillna(0.0) == 1.0)
        & (pd.to_numeric(short_df["label_valid"], errors="coerce").fillna(0.0) == 1.0)
    ).astype(float)
    ce_candidate = pd.to_numeric(long_df["label"], errors="coerce").fillna(0.0).to_numpy()
    pe_candidate = pd.to_numeric(short_df["label"], errors="coerce").fillna(0.0).to_numpy()

    additions = pd.concat(
        [
            pd.DataFrame(
                {
                    "label_horizon_minutes": np.full(len(out), int(cfg.horizon_minutes), dtype=int),
                    "label_return_threshold": np.full(len(out), float(cfg.return_threshold), dtype=float),
                },
                index=out.index,
            ),
            long_prefixed.set_index(out.index),
            short_prefixed.set_index(out.index),
            ce_prefixed.set_index(out.index),
            pe_prefixed.set_index(out.index),
            pd.DataFrame(
                {
                    "move_label_valid": move_label_valid.to_numpy(),
                    "move_label": move_label,
                    "move_path_exit_reason": move_path_exit_reason,
                    "move_first_hit_side": np.asarray(move_path_exit_reason, dtype=object),
                    "move_event_end_ts": pd.to_datetime(long_df["event_end_ts"], errors="coerce").to_numpy(),
                    "move_barrier_upper_return": pd.to_numeric(long_df["barrier_upper_return"], errors="coerce").to_numpy(),
                    "move_barrier_lower_return": pd.to_numeric(long_df["barrier_lower_return"], errors="coerce").to_numpy(),
                    "best_side_label": np.where((ce_candidate <= 0.0) & (pe_candidate <= 0.0), 0, np.where(ce_candidate >= pe_candidate, 1, -1)),
                },
                index=out.index,
            ),
        ],
        axis=1,
    )
    out = out.drop(columns=[col for col in additions.columns if col in out.columns], errors="ignore")
    out = pd.concat([out, additions], axis=1)
    return out.copy()


def _label_day_frame(day_features: pd.DataFrame, cfg: EffectiveLabelConfig) -> pd.DataFrame:
    return label_day_futures(day_features, cfg).sort_values("timestamp").reset_index(drop=True)


def _resolve_label_workers(day_count: int) -> int:
    if day_count <= 1:
        return 1
    raw = str(os.getenv("ML_PIPELINE_LABEL_WORKERS", "") or "").strip()
    if raw:
        try:
            requested = int(raw)
        except Exception:
            requested = 1
    else:
        requested = min(8, int(os.cpu_count() or 1))
    return max(1, min(int(day_count), int(requested)))


def build_labeled_dataset(features: pd.DataFrame, *, cfg: EffectiveLabelConfig) -> pd.DataFrame:
    frame = features.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    if "trade_date" not in frame.columns:
        frame["trade_date"] = frame["timestamp"].dt.strftime("%Y-%m-%d")
    frame = frame.dropna(subset=["timestamp", "trade_date"]).sort_values("timestamp")
    frame["trade_date"] = frame["trade_date"].astype(str)
    day_frames = [day_frame.copy() for _, day_frame in frame.groupby("trade_date", sort=False)]
    workers = _resolve_label_workers(len(day_frames))
    if workers <= 1:
        labeled_parts = [_label_day_frame(day_features, cfg) for day_features in day_frames]
    else:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            labeled_parts = list(executor.map(_label_day_frame, day_frames, [cfg] * len(day_frames)))
    return pd.concat(labeled_parts, ignore_index=True).sort_values("timestamp").reset_index(drop=True) if labeled_parts else pd.DataFrame()


def build_label_lineage(frame: pd.DataFrame, cfg: EffectiveLabelConfig) -> Dict[str, Any]:
    return {
        "config": asdict(cfg),
        "rows_total": int(len(frame)),
        "days_total": int(frame["trade_date"].nunique()) if "trade_date" in frame.columns else 0,
        "long_positive_rate": float(pd.to_numeric(frame.get("long_label"), errors="coerce").fillna(0.0).mean()) if "long_label" in frame.columns and len(frame) else 0.0,
        "short_positive_rate": float(pd.to_numeric(frame.get("short_label"), errors="coerce").fillna(0.0).mean()) if "short_label" in frame.columns and len(frame) else 0.0,
        "move_positive_rate": float(pd.to_numeric(frame.get("move_label"), errors="coerce").fillna(0.0).mean()) if "move_label" in frame.columns and len(frame) else 0.0,
        "regimes": summarize_regimes(frame),
    }
