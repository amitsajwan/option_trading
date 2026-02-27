import argparse
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from .config import LabelConfig
from .exit_policy import ExitPolicyConfig, load_exit_policy
from .raw_loader import filter_valid_options, load_day_options
from .schema_validator import resolve_archive_base


@dataclass(frozen=True)
class EffectiveLabelConfig:
    horizon_minutes: int
    return_threshold: float
    use_excursion_gate: bool
    min_favorable_excursion: float
    max_adverse_excursion: float
    ce_return_threshold: Optional[float] = None
    pe_return_threshold: Optional[float] = None
    ce_use_excursion_gate: Optional[bool] = None
    pe_use_excursion_gate: Optional[bool] = None
    ce_min_favorable_excursion: Optional[float] = None
    pe_min_favorable_excursion: Optional[float] = None
    ce_max_adverse_excursion: Optional[float] = None
    pe_max_adverse_excursion: Optional[float] = None
    stop_loss_pct: float = 0.12
    take_profit_pct: float = 0.24
    allow_hold_extension: bool = False
    extension_trigger_profit_pct: float = 0.0
    aux_horizons: tuple[int, ...] = ()


def _normalize_horizons(values: Optional[Sequence[int]], *, exclude: int) -> tuple[int, ...]:
    if values is None:
        return ()
    out: List[int] = []
    seen = set()
    for raw in values:
        try:
            h = int(raw)
        except Exception:
            continue
        if h <= 0 or h == int(exclude) or h in seen:
            continue
        seen.add(h)
        out.append(h)
    return tuple(sorted(out))


def _effective_config(
    horizon_minutes: Optional[int],
    return_threshold: Optional[float],
    use_excursion_gate: Optional[bool],
    min_favorable_excursion: Optional[float],
    max_adverse_excursion: Optional[float],
    ce_return_threshold: Optional[float] = None,
    pe_return_threshold: Optional[float] = None,
    ce_use_excursion_gate: Optional[bool] = None,
    pe_use_excursion_gate: Optional[bool] = None,
    ce_min_favorable_excursion: Optional[float] = None,
    pe_min_favorable_excursion: Optional[float] = None,
    ce_max_adverse_excursion: Optional[float] = None,
    pe_max_adverse_excursion: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    allow_hold_extension: Optional[bool] = None,
    extension_trigger_profit_pct: Optional[float] = None,
    aux_horizons: Optional[Sequence[int]] = None,
) -> EffectiveLabelConfig:
    default = LabelConfig()
    default_exit = ExitPolicyConfig()
    resolved_horizon = int(horizon_minutes if horizon_minutes is not None else default.horizon_minutes)
    return EffectiveLabelConfig(
        horizon_minutes=resolved_horizon,
        return_threshold=return_threshold if return_threshold is not None else default.return_threshold,
        use_excursion_gate=use_excursion_gate if use_excursion_gate is not None else default.use_excursion_gate,
        min_favorable_excursion=(
            min_favorable_excursion if min_favorable_excursion is not None else default.min_favorable_excursion
        ),
        max_adverse_excursion=(
            max_adverse_excursion if max_adverse_excursion is not None else default.max_adverse_excursion
        ),
        ce_return_threshold=ce_return_threshold,
        pe_return_threshold=pe_return_threshold,
        ce_use_excursion_gate=ce_use_excursion_gate,
        pe_use_excursion_gate=pe_use_excursion_gate,
        ce_min_favorable_excursion=ce_min_favorable_excursion,
        pe_min_favorable_excursion=pe_min_favorable_excursion,
        ce_max_adverse_excursion=ce_max_adverse_excursion,
        pe_max_adverse_excursion=pe_max_adverse_excursion,
        stop_loss_pct=stop_loss_pct if stop_loss_pct is not None else float(default_exit.stop_loss_pct or 0.12),
        take_profit_pct=take_profit_pct if take_profit_pct is not None else float(default_exit.take_profit_pct or 0.24),
        allow_hold_extension=(
            allow_hold_extension if allow_hold_extension is not None else bool(default_exit.allow_hold_extension)
        ),
        extension_trigger_profit_pct=(
            extension_trigger_profit_pct
            if extension_trigger_profit_pct is not None
            else float(default_exit.move_to_break_even_at_profit_pct or 0.0)
        ),
        aux_horizons=_normalize_horizons(aux_horizons, exclude=resolved_horizon),
    )


def option_symbol(expiry_code: str, strike: int, side: str) -> str:
    return f"BANKNIFTY{str(expiry_code).upper()}{int(strike)}{str(side).upper()}"


def _option_lookup(options_df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    lookup: Dict[str, pd.DataFrame] = {}
    if options_df.empty:
        return lookup
    for symbol, grp in options_df.groupby("symbol", sort=False):
        slot = grp.sort_values("timestamp").set_index("timestamp", drop=False)
        lookup[str(symbol).upper()] = slot
    return lookup


def _trade_window(decision_ts: pd.Timestamp, horizon_minutes: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    entry_ts = decision_ts + pd.Timedelta(minutes=1)
    exit_ts = decision_ts + pd.Timedelta(minutes=int(horizon_minutes))
    return entry_ts, exit_ts


def _compute_trade_metrics(
    symbol_table: pd.DataFrame,
    decision_ts: pd.Timestamp,
    horizon_minutes: int,
    cfg: EffectiveLabelConfig,
) -> Dict[str, object]:
    entry_ts, exit_ts = _trade_window(decision_ts, horizon_minutes=horizon_minutes)

    if entry_ts not in symbol_table.index or exit_ts not in symbol_table.index:
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
        }

    entry_price = float(symbol_table.at[entry_ts, "open"])
    exit_price = float(symbol_table.at[exit_ts, "close"])
    if not np.isfinite(entry_price) or entry_price <= 0.0 or not np.isfinite(exit_price):
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
        }

    window = symbol_table.loc[(symbol_table.index >= entry_ts) & (symbol_table.index <= exit_ts)]
    if window.empty:
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
        }

    max_high = float(window["high"].max())
    min_low = float(window["low"].min())
    forward_return = (exit_price - entry_price) / entry_price
    mfe = (max_high - entry_price) / entry_price
    mae = (min_low - entry_price) / entry_price
    tp_price = entry_price * (1.0 + float(cfg.take_profit_pct))
    sl_price = entry_price * (1.0 - float(cfg.stop_loss_pct))

    tp_hit = 0.0
    sl_hit = 0.0
    first_hit = "none"
    first_hit_offset = np.nan
    for offset, (_, bar) in enumerate(window.iterrows()):
        high = float(bar["high"])
        low = float(bar["low"])
        hit_tp = np.isfinite(high) and (high >= tp_price)
        hit_sl = np.isfinite(low) and (low <= sl_price)
        if hit_tp and hit_sl:
            tp_hit = 1.0
            sl_hit = 1.0
            first_hit = "tp_sl_same_bar"
            first_hit_offset = float(offset)
            break
        if hit_tp:
            tp_hit = 1.0
            first_hit = "tp"
            first_hit_offset = float(offset)
            break
        if hit_sl:
            sl_hit = 1.0
            first_hit = "sl"
            first_hit_offset = float(offset)
            break

    time_stop_exit = 1.0 if first_hit == "none" else 0.0
    hold_extension_eligible = 1.0 if (
        cfg.allow_hold_extension
        and time_stop_exit == 1.0
        and np.isfinite(mfe)
        and (mfe >= float(cfg.extension_trigger_profit_pct))
        and np.isfinite(forward_return)
        and (forward_return >= 0.0)
        and np.isfinite(mae)
        and (mae > (-float(cfg.stop_loss_pct)))
    ) else 0.0
    path_exit_reason = first_hit if first_hit != "none" else "time_stop"

    return {
        "entry_price": entry_price,
        "exit_price": exit_price,
        "forward_return": forward_return,
        "mfe": mfe,
        "mae": mae,
        "label_valid": 1.0,
        "tp_hit": tp_hit,
        "sl_hit": sl_hit,
        "first_hit": path_exit_reason,
        "first_hit_offset_min": first_hit_offset,
        "path_exit_reason": path_exit_reason,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "time_stop_exit": time_stop_exit,
        "hold_extension_eligible": hold_extension_eligible,
    }


def _side_config(cfg: EffectiveLabelConfig, side: str) -> tuple[float, bool, float, float]:
    s = str(side).lower()
    if s not in {"ce", "pe"}:
        raise ValueError(f"unsupported side for label config: {side}")
    if s == "ce":
        rt = cfg.ce_return_threshold if cfg.ce_return_threshold is not None else cfg.return_threshold
        use_gate = cfg.ce_use_excursion_gate if cfg.ce_use_excursion_gate is not None else cfg.use_excursion_gate
        mfe = (
            cfg.ce_min_favorable_excursion
            if cfg.ce_min_favorable_excursion is not None
            else cfg.min_favorable_excursion
        )
        mae = cfg.ce_max_adverse_excursion if cfg.ce_max_adverse_excursion is not None else cfg.max_adverse_excursion
    else:
        rt = cfg.pe_return_threshold if cfg.pe_return_threshold is not None else cfg.return_threshold
        use_gate = cfg.pe_use_excursion_gate if cfg.pe_use_excursion_gate is not None else cfg.use_excursion_gate
        mfe = (
            cfg.pe_min_favorable_excursion
            if cfg.pe_min_favorable_excursion is not None
            else cfg.min_favorable_excursion
        )
        mae = cfg.pe_max_adverse_excursion if cfg.pe_max_adverse_excursion is not None else cfg.max_adverse_excursion
    return float(rt), bool(use_gate), float(mfe), float(mae)


def _label_from_metrics(
    metrics: Dict[str, object],
    cfg: EffectiveLabelConfig,
    side: str,
) -> float:
    if int(metrics["label_valid"]) != 1:
        return np.nan
    return_threshold, use_excursion_gate, min_favorable_excursion, max_adverse_excursion = _side_config(cfg, side)
    positive = metrics["forward_return"] >= return_threshold
    if not positive:
        return 0.0
    if not use_excursion_gate:
        return 1.0
    gate_ok = (metrics["mfe"] >= min_favorable_excursion) and (metrics["mae"] >= (-max_adverse_excursion))
    return 1.0 if gate_ok else 0.0


def label_day(
    features_day: pd.DataFrame,
    options_day: pd.DataFrame,
    cfg: EffectiveLabelConfig,
) -> pd.DataFrame:
    out = features_day.sort_values("timestamp").copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    options_day = filter_valid_options(options_day)
    options_day["symbol"] = options_day["symbol"].astype(str).str.upper()
    options_day["timestamp"] = pd.to_datetime(options_day["timestamp"], errors="coerce")
    lookup = _option_lookup(options_day.dropna(subset=["timestamp", "symbol"]))

    ce_symbols: List[str] = []
    pe_symbols: List[str] = []
    ce_entry: List[float] = []
    ce_exit: List[float] = []
    ce_ret: List[float] = []
    ce_mfe: List[float] = []
    ce_mae: List[float] = []
    ce_label: List[float] = []
    ce_valid: List[float] = []
    ce_tp_hit: List[float] = []
    ce_sl_hit: List[float] = []
    ce_first_hit: List[str] = []
    ce_first_hit_offset: List[float] = []
    ce_path_exit_reason: List[str] = []
    ce_tp_price: List[float] = []
    ce_sl_price: List[float] = []
    ce_time_stop_exit: List[float] = []
    ce_hold_extension_eligible: List[float] = []
    pe_entry: List[float] = []
    pe_exit: List[float] = []
    pe_ret: List[float] = []
    pe_mfe: List[float] = []
    pe_mae: List[float] = []
    pe_label: List[float] = []
    pe_valid: List[float] = []
    pe_tp_hit: List[float] = []
    pe_sl_hit: List[float] = []
    pe_first_hit: List[str] = []
    pe_first_hit_offset: List[float] = []
    pe_path_exit_reason: List[str] = []
    pe_tp_price: List[float] = []
    pe_sl_price: List[float] = []
    pe_time_stop_exit: List[float] = []
    pe_hold_extension_eligible: List[float] = []

    for row in out.itertuples(index=False):
        decision_ts = pd.Timestamp(row.timestamp)
        strike = int(row.atm_strike)
        expiry_code = str(row.expiry_code)
        ce_sym = option_symbol(expiry_code, strike, "CE")
        pe_sym = option_symbol(expiry_code, strike, "PE")
        ce_symbols.append(ce_sym)
        pe_symbols.append(pe_sym)

        ce_table = lookup.get(ce_sym)
        if ce_table is None:
            ce_metrics = {
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
            }
        else:
            ce_metrics = _compute_trade_metrics(ce_table, decision_ts, cfg.horizon_minutes, cfg)
        pe_table = lookup.get(pe_sym)
        if pe_table is None:
            pe_metrics = {
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
            }
        else:
            pe_metrics = _compute_trade_metrics(pe_table, decision_ts, cfg.horizon_minutes, cfg)

        ce_entry.append(ce_metrics["entry_price"])
        ce_exit.append(ce_metrics["exit_price"])
        ce_ret.append(ce_metrics["forward_return"])
        ce_mfe.append(ce_metrics["mfe"])
        ce_mae.append(ce_metrics["mae"])
        ce_valid.append(ce_metrics["label_valid"])
        ce_label.append(_label_from_metrics(ce_metrics, cfg, side="ce"))
        ce_tp_hit.append(ce_metrics["tp_hit"])
        ce_sl_hit.append(ce_metrics["sl_hit"])
        ce_first_hit.append(str(ce_metrics["first_hit"]))
        ce_first_hit_offset.append(ce_metrics["first_hit_offset_min"])
        ce_path_exit_reason.append(str(ce_metrics["path_exit_reason"]))
        ce_tp_price.append(ce_metrics["tp_price"])
        ce_sl_price.append(ce_metrics["sl_price"])
        ce_time_stop_exit.append(ce_metrics["time_stop_exit"])
        ce_hold_extension_eligible.append(ce_metrics["hold_extension_eligible"])

        pe_entry.append(pe_metrics["entry_price"])
        pe_exit.append(pe_metrics["exit_price"])
        pe_ret.append(pe_metrics["forward_return"])
        pe_mfe.append(pe_metrics["mfe"])
        pe_mae.append(pe_metrics["mae"])
        pe_valid.append(pe_metrics["label_valid"])
        pe_label.append(_label_from_metrics(pe_metrics, cfg, side="pe"))
        pe_tp_hit.append(pe_metrics["tp_hit"])
        pe_sl_hit.append(pe_metrics["sl_hit"])
        pe_first_hit.append(str(pe_metrics["first_hit"]))
        pe_first_hit_offset.append(pe_metrics["first_hit_offset_min"])
        pe_path_exit_reason.append(str(pe_metrics["path_exit_reason"]))
        pe_tp_price.append(pe_metrics["tp_price"])
        pe_sl_price.append(pe_metrics["sl_price"])
        pe_time_stop_exit.append(pe_metrics["time_stop_exit"])
        pe_hold_extension_eligible.append(pe_metrics["hold_extension_eligible"])

    out["label_horizon_minutes"] = int(cfg.horizon_minutes)
    out["label_return_threshold"] = float(cfg.return_threshold)
    out["ce_label_return_threshold"] = float(_side_config(cfg, "ce")[0])
    out["pe_label_return_threshold"] = float(_side_config(cfg, "pe")[0])
    out["ce_symbol"] = ce_symbols
    out["pe_symbol"] = pe_symbols
    out["ce_entry_price"] = ce_entry
    out["ce_exit_price"] = ce_exit
    out["ce_forward_return"] = ce_ret
    out["ce_mfe"] = ce_mfe
    out["ce_mae"] = ce_mae
    out["ce_label_valid"] = ce_valid
    out["ce_label"] = ce_label
    out["ce_tp_hit"] = ce_tp_hit
    out["ce_sl_hit"] = ce_sl_hit
    out["ce_first_hit"] = ce_first_hit
    out["ce_first_hit_offset_min"] = ce_first_hit_offset
    out["ce_path_exit_reason"] = ce_path_exit_reason
    out["ce_tp_price"] = ce_tp_price
    out["ce_sl_price"] = ce_sl_price
    out["ce_time_stop_exit"] = ce_time_stop_exit
    out["ce_hold_extension_eligible"] = ce_hold_extension_eligible
    out["pe_entry_price"] = pe_entry
    out["pe_exit_price"] = pe_exit
    out["pe_forward_return"] = pe_ret
    out["pe_mfe"] = pe_mfe
    out["pe_mae"] = pe_mae
    out["pe_label_valid"] = pe_valid
    out["pe_label"] = pe_label
    out["pe_tp_hit"] = pe_tp_hit
    out["pe_sl_hit"] = pe_sl_hit
    out["pe_first_hit"] = pe_first_hit
    out["pe_first_hit_offset_min"] = pe_first_hit_offset
    out["pe_path_exit_reason"] = pe_path_exit_reason
    out["pe_tp_price"] = pe_tp_price
    out["pe_sl_price"] = pe_sl_price
    out["pe_time_stop_exit"] = pe_time_stop_exit
    out["pe_hold_extension_eligible"] = pe_hold_extension_eligible
    # Path target is trainable only when TP/SL path is resolved (time-stop excluded).
    out["ce_path_target_valid"] = (
        (out["ce_label_valid"].fillna(0.0) == 1.0)
        & out["ce_path_exit_reason"].astype(str).str.lower().isin({"tp", "sl", "tp_sl_same_bar"})
    ).astype(float)
    out["pe_path_target_valid"] = (
        (out["pe_label_valid"].fillna(0.0) == 1.0)
        & out["pe_path_exit_reason"].astype(str).str.lower().isin({"tp", "sl", "tp_sl_same_bar"})
    ).astype(float)

    # Best-side guidance for downstream experiments; no-trade is 0.
    ce_candidate = out["ce_label"].fillna(0.0)
    pe_candidate = out["pe_label"].fillna(0.0)
    out["best_side_label"] = np.where((ce_candidate <= 0.0) & (pe_candidate <= 0.0), 0, np.where(ce_candidate >= pe_candidate, 1, -1))
    return out


def build_labeled_dataset(
    features: pd.DataFrame,
    base_path: Path,
    cfg: EffectiveLabelConfig,
) -> pd.DataFrame:
    frame = features.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "trade_date", "atm_strike", "expiry_code"]).sort_values("timestamp")

    days = [str(x) for x in frame["trade_date"].astype(str).unique().tolist()]
    labeled_parts: List[pd.DataFrame] = []
    for day in days:
        day_features = frame[frame["trade_date"].astype(str) == day].copy()
        options_day = load_day_options(base_path=base_path, day=day)
        day_labeled = label_day(day_features, options_day, cfg).sort_values("timestamp").reset_index(drop=True)
        for h in cfg.aux_horizons:
            aux_cfg = replace(cfg, horizon_minutes=int(h), aux_horizons=())
            aux = label_day(day_features, options_day, aux_cfg).sort_values("timestamp").reset_index(drop=True)
            suffix = f"_h{int(h)}m"
            day_labeled[f"ce_forward_return{suffix}"] = aux["ce_forward_return"].to_numpy()
            day_labeled[f"pe_forward_return{suffix}"] = aux["pe_forward_return"].to_numpy()
            day_labeled[f"ce_label_valid{suffix}"] = aux["ce_label_valid"].to_numpy()
            day_labeled[f"pe_label_valid{suffix}"] = aux["pe_label_valid"].to_numpy()
            day_labeled[f"ce_label{suffix}"] = aux["ce_label"].to_numpy()
            day_labeled[f"pe_label{suffix}"] = aux["pe_label"].to_numpy()
            day_labeled[f"label_horizon_minutes{suffix}"] = int(h)
        labeled_parts.append(day_labeled)

    if not labeled_parts:
        return pd.DataFrame()
    out = pd.concat(labeled_parts, ignore_index=True)
    out = out.sort_values("timestamp").reset_index(drop=True)
    return out


def _build_report(df: pd.DataFrame, cfg: EffectiveLabelConfig, base_path: Path) -> Dict[str, object]:
    ce_valid = df["ce_label_valid"].fillna(0.0) == 1.0
    pe_valid = df["pe_label_valid"].fillna(0.0) == 1.0
    ce_pos = float(df.loc[ce_valid, "ce_label"].fillna(0.0).mean()) if ce_valid.any() else np.nan
    pe_pos = float(df.loc[pe_valid, "pe_label"].fillna(0.0).mean()) if pe_valid.any() else np.nan
    ce_path_target_valid = (
        int(df["ce_path_target_valid"].fillna(0.0).sum()) if "ce_path_target_valid" in df.columns else 0
    )
    pe_path_target_valid = (
        int(df["pe_path_target_valid"].fillna(0.0).sum()) if "pe_path_target_valid" in df.columns else 0
    )
    ce_rt, ce_gate, ce_mfe, ce_mae = _side_config(cfg, "ce")
    pe_rt, pe_gate, pe_mfe, pe_mae = _side_config(cfg, "pe")
    return {
        "base_path": str(base_path),
        "config": {
            "horizon_minutes": cfg.horizon_minutes,
            "aux_horizons": [int(x) for x in cfg.aux_horizons],
            "return_threshold": cfg.return_threshold,
            "use_excursion_gate": cfg.use_excursion_gate,
            "min_favorable_excursion": cfg.min_favorable_excursion,
            "max_adverse_excursion": cfg.max_adverse_excursion,
            "ce_return_threshold": ce_rt,
            "pe_return_threshold": pe_rt,
            "ce_use_excursion_gate": ce_gate,
            "pe_use_excursion_gate": pe_gate,
            "ce_min_favorable_excursion": ce_mfe,
            "pe_min_favorable_excursion": pe_mfe,
            "ce_max_adverse_excursion": ce_mae,
            "pe_max_adverse_excursion": pe_mae,
            "stop_loss_pct": cfg.stop_loss_pct,
            "take_profit_pct": cfg.take_profit_pct,
            "allow_hold_extension": cfg.allow_hold_extension,
            "extension_trigger_profit_pct": cfg.extension_trigger_profit_pct,
        },
        "rows_total": int(len(df)),
        "days_total": int(df["trade_date"].nunique()) if "trade_date" in df.columns else 0,
        "ce_valid_rows": int(ce_valid.sum()),
        "pe_valid_rows": int(pe_valid.sum()),
        "ce_path_target_valid_rows": ce_path_target_valid,
        "pe_path_target_valid_rows": pe_path_target_valid,
        "ce_positive_rate": ce_pos,
        "pe_positive_rate": pe_pos,
    }


def _build_path_report(df: pd.DataFrame, cfg: EffectiveLabelConfig, base_path: Path) -> Dict[str, object]:
    def _side(side: str) -> Dict[str, object]:
        valid_mask = df[f"{side}_label_valid"].fillna(0.0) == 1.0
        path_target_valid_col = f"{side}_path_target_valid"
        reason_col = f"{side}_path_exit_reason"
        reasons = df.loc[valid_mask, reason_col].fillna("invalid").value_counts().to_dict() if reason_col in df.columns else {}
        return {
            "valid_rows": int(valid_mask.sum()),
            "path_target_valid_rows": (
                int(df[path_target_valid_col].fillna(0.0).sum()) if path_target_valid_col in df.columns else 0
            ),
            "tp_hit_rate": float(df.loc[valid_mask, f"{side}_tp_hit"].fillna(0.0).mean()) if valid_mask.any() else 0.0,
            "sl_hit_rate": float(df.loc[valid_mask, f"{side}_sl_hit"].fillna(0.0).mean()) if valid_mask.any() else 0.0,
            "time_stop_rate": (
                float(df.loc[valid_mask, f"{side}_time_stop_exit"].fillna(0.0).mean())
                if valid_mask.any()
                else 0.0
            ),
            "hold_extension_eligible_rate": (
                float(df.loc[valid_mask, f"{side}_hold_extension_eligible"].fillna(0.0).mean())
                if valid_mask.any()
                else 0.0
            ),
            "exit_reason_counts": {str(k): int(v) for k, v in reasons.items()},
        }

    return {
        "base_path": str(base_path),
        "config": {
            "horizon_minutes": cfg.horizon_minutes,
            "aux_horizons": [int(x) for x in cfg.aux_horizons],
            "stop_loss_pct": cfg.stop_loss_pct,
            "take_profit_pct": cfg.take_profit_pct,
            "allow_hold_extension": cfg.allow_hold_extension,
            "extension_trigger_profit_pct": cfg.extension_trigger_profit_pct,
        },
        "rows_total": int(len(df)),
        "ce": _side("ce"),
        "pe": _side("pe"),
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build trade-aligned CE/PE labels")
    parser.add_argument(
        "--features",
        default="ml_pipeline/artifacts/t04_features.parquet",
        help="Feature parquet input",
    )
    parser.add_argument("--base-path", default=None, help="Archive base path")
    parser.add_argument("--horizon-minutes", type=int, default=None, help="Label horizon in minutes")
    parser.add_argument(
        "--aux-horizons",
        default="",
        help="Comma-separated additional horizons in minutes (example: 5,15,30)",
    )
    parser.add_argument("--return-threshold", type=float, default=None, help="Positive-return threshold")
    parser.add_argument("--ce-return-threshold", type=float, default=None, help="CE positive-return threshold override")
    parser.add_argument("--pe-return-threshold", type=float, default=None, help="PE positive-return threshold override")
    parser.add_argument(
        "--use-excursion-gate",
        action="store_true",
        help="Enable MFE/MAE gating in label-positive condition",
    )
    parser.add_argument("--min-favorable-excursion", type=float, default=None, help="MFE gate threshold")
    parser.add_argument("--max-adverse-excursion", type=float, default=None, help="MAE gate threshold")
    parser.add_argument("--ce-min-favorable-excursion", type=float, default=None, help="CE MFE gate threshold override")
    parser.add_argument("--pe-min-favorable-excursion", type=float, default=None, help="PE MFE gate threshold override")
    parser.add_argument("--ce-max-adverse-excursion", type=float, default=None, help="CE MAE gate threshold override")
    parser.add_argument("--pe-max-adverse-excursion", type=float, default=None, help="PE MAE gate threshold override")
    parser.add_argument("--ce-use-excursion-gate", action="store_true", help="Enable excursion gate for CE labels")
    parser.add_argument("--pe-use-excursion-gate", action="store_true", help="Enable excursion gate for PE labels")
    parser.add_argument("--exit-policy-json", default=None, help="Optional T14 exit policy JSON")
    parser.add_argument("--stop-loss-pct", type=float, default=None, help="Stop-loss pct override")
    parser.add_argument("--take-profit-pct", type=float, default=None, help="Take-profit pct override")
    parser.add_argument(
        "--allow-hold-extension",
        action="store_true",
        help="Override to enable hold-extension eligibility labels",
    )
    parser.add_argument(
        "--extension-trigger-profit-pct",
        type=float,
        default=None,
        help="Hold extension trigger profit pct override",
    )
    parser.add_argument(
        "--out",
        default="ml_pipeline/artifacts/t05_labeled_features.parquet",
        help="Labeled output parquet",
    )
    parser.add_argument(
        "--report-out",
        default="ml_pipeline/artifacts/t05_label_report.json",
        help="Label summary report JSON",
    )
    parser.add_argument(
        "--path-report-out",
        default="ml_pipeline/artifacts/t15_label_path_report.json",
        help="Path-aware label summary report JSON",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    feature_path = Path(args.features)
    if not feature_path.exists():
        print(f"ERROR: features file not found: {feature_path}")
        return 2
    base = resolve_archive_base(explicit_base=args.base_path)
    if base is None:
        print("ERROR: archive base path not found")
        return 2

    exit_policy: Optional[ExitPolicyConfig] = None
    if args.exit_policy_json:
        policy_path = Path(args.exit_policy_json)
        if not policy_path.exists():
            print(f"ERROR: exit policy file not found: {policy_path}")
            return 2
        exit_policy = load_exit_policy(policy_path)
    else:
        exit_policy = ExitPolicyConfig()

    hold_extension_override = True if args.allow_hold_extension else None
    aux_horizons = [int(x.strip()) for x in str(args.aux_horizons).split(",") if x.strip()]

    cfg = _effective_config(
        horizon_minutes=args.horizon_minutes,
        return_threshold=args.return_threshold,
        use_excursion_gate=True if args.use_excursion_gate else None,
        min_favorable_excursion=args.min_favorable_excursion,
        max_adverse_excursion=args.max_adverse_excursion,
        ce_return_threshold=args.ce_return_threshold,
        pe_return_threshold=args.pe_return_threshold,
        ce_use_excursion_gate=True if args.ce_use_excursion_gate else None,
        pe_use_excursion_gate=True if args.pe_use_excursion_gate else None,
        ce_min_favorable_excursion=args.ce_min_favorable_excursion,
        pe_min_favorable_excursion=args.pe_min_favorable_excursion,
        ce_max_adverse_excursion=args.ce_max_adverse_excursion,
        pe_max_adverse_excursion=args.pe_max_adverse_excursion,
        stop_loss_pct=(
            args.stop_loss_pct if args.stop_loss_pct is not None else float(exit_policy.stop_loss_pct or 0.12)
        ),
        take_profit_pct=(
            args.take_profit_pct if args.take_profit_pct is not None else float(exit_policy.take_profit_pct or 0.24)
        ),
        allow_hold_extension=(
            hold_extension_override
            if hold_extension_override is not None
            else bool(exit_policy.allow_hold_extension)
        ),
        extension_trigger_profit_pct=(
            args.extension_trigger_profit_pct
            if args.extension_trigger_profit_pct is not None
            else float(exit_policy.move_to_break_even_at_profit_pct or 0.0)
        ),
        aux_horizons=aux_horizons,
    )

    features = pd.read_parquet(feature_path)
    labeled = build_labeled_dataset(features=features, base_path=base, cfg=cfg)
    out_path = Path(args.out)
    report_path = Path(args.report_out)
    path_report_path = Path(args.path_report_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    path_report_path.parent.mkdir(parents=True, exist_ok=True)

    labeled.to_parquet(out_path, index=False)
    report = _build_report(labeled, cfg, base)
    path_report = _build_path_report(labeled, cfg, base)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    path_report_path.write_text(json.dumps(path_report, indent=2), encoding="utf-8")

    print(f"Features in: {feature_path}")
    print(f"Labeled rows: {len(labeled)}")
    print(f"Days: {report['days_total']}")
    print(f"CE valid rows: {report['ce_valid_rows']}")
    print(f"PE valid rows: {report['pe_valid_rows']}")
    print(f"Output: {out_path}")
    print(f"Report: {report_path}")
    print(f"Path report: {path_report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
