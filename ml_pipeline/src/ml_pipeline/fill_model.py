from dataclasses import asdict, dataclass
from typing import Dict

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class FillModelConfig:
    model: str = "constant"  # constant|spread_fraction|liquidity_adjusted
    constant_slippage: float = 0.0
    spread_fraction: float = 0.5
    volume_impact_coeff: float = 0.02
    min_slippage: float = 0.0
    max_slippage: float = 0.01


def validate_fill_model_config(cfg: FillModelConfig) -> None:
    if cfg.model not in {"constant", "spread_fraction", "liquidity_adjusted"}:
        raise ValueError("fill model must be one of: constant, spread_fraction, liquidity_adjusted")
    if cfg.constant_slippage < 0.0:
        raise ValueError("constant_slippage must be >= 0")
    if cfg.spread_fraction < 0.0:
        raise ValueError("spread_fraction must be >= 0")
    if cfg.volume_impact_coeff < 0.0:
        raise ValueError("volume_impact_coeff must be >= 0")
    if cfg.min_slippage < 0.0:
        raise ValueError("min_slippage must be >= 0")
    if cfg.max_slippage < cfg.min_slippage:
        raise ValueError("max_slippage must be >= min_slippage")


def _safe_float(value: object) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def _clamp(value: float, lo: float, hi: float) -> float:
    return float(min(max(value, lo), hi))


def _side_prefix(side: str) -> str:
    s = str(side).upper()
    if s == "CE":
        return "opt_0_ce"
    if s == "PE":
        return "opt_0_pe"
    raise ValueError("side must be CE or PE")


def estimate_slippage_return(row: pd.Series, side: str, config: FillModelConfig) -> float:
    validate_fill_model_config(config)
    prefix = _side_prefix(side)
    close = _safe_float(row.get(f"{prefix}_close"))
    high = _safe_float(row.get(f"{prefix}_high"))
    low = _safe_float(row.get(f"{prefix}_low"))
    volume = _safe_float(row.get(f"{prefix}_volume"))

    if config.model == "constant":
        base = float(config.constant_slippage)
    else:
        spread_proxy = 0.0
        if np.isfinite(close) and close > 0 and np.isfinite(high) and np.isfinite(low):
            spread_proxy = max(0.0, float((high - low) / close))
        if config.model == "spread_fraction":
            base = float(spread_proxy * config.spread_fraction)
        else:
            volume_term = 0.0
            if np.isfinite(volume) and volume > 0:
                volume_term = float(config.volume_impact_coeff / np.sqrt(volume))
            else:
                volume_term = float(config.volume_impact_coeff)
            base = float((spread_proxy * config.spread_fraction) + volume_term)

    return _clamp(base, float(config.min_slippage), float(config.max_slippage))


def config_to_dict(cfg: FillModelConfig) -> Dict[str, object]:
    return asdict(cfg)
