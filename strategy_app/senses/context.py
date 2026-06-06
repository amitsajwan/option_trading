"""Per-bar feature context for the senses (Layer 1 input prep).

This is NOT a sense — it is the shared, pure feature-prep that turns a list of
raw 1-min bars (futures_bar + chain_aggregates, the shape persisted in
``trading_ai.phase1_market_snapshots``) into a per-bar ``BarContext`` the senses
read from. Keeping the windowing here means every sense sees the same numbers and
no sense recomputes ATR/volume windows itself.

The ATR/volume/OI windowing mirrors the verified Phase-0 proof
(``ops/research/bigmove_score_backtest.py``) exactly so the Move sense reproduces
the B-0.1 numbers: build window = last 15 bars, baseline = the 26 before that,
warmup = 42 bars.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Windowing + thresholds — mirror ops/research/bigmove_score_backtest.py (single definition of the math).
BUILD_WINDOW = 15
BASE_WINDOW = 26
WARMUP = 42                 # BUILD_WINDOW + BASE_WINDOW + 1
COMPRESS_RATIO = 0.70
VOL_SPIKE = 1.8
VELOCITY_K = 1.5
OI_BUILD = 1.002


@dataclass(frozen=True)
class BarContext:
    """Everything the senses need for one bar. Pure data, no behaviour."""

    day: str
    index: int
    close: float
    # move/compression inputs
    atr_build: float
    atr_base: float
    last_bar_return: float          # signed close-vs-prev-close (pt)
    option_volume: float
    vol_build_avg: float
    oi_now: float
    oi_15ago: float
    # destination inputs (levels; may be None when a feed is absent in sim)
    max_pain: float | None = None
    ce_oi_top_strike: float | None = None
    pe_oi_top_strike: float | None = None
    prior_day_high: float | None = None
    prior_day_low: float | None = None
    opening_range_high: float | None = None
    opening_range_low: float | None = None
    # direction inputs (the measured signals: VWAP bias + 5-min momentum)
    vwap: float | None = None
    fut_return_5m: float | None = None
    # flow inputs (depth; abstain when absent)
    net_ofi: float | None = None
    ce_bid_strength: float | None = None
    pe_bid_strength: float | None = None
    # execution inputs
    spread_pct: float | None = None
    # structure (trader's highs/lows/breakouts lens)
    struct_breakout: str | None = None      # "up" | "down" | "none"
    struct_fakeout: bool = False
    struct_position: str | None = None       # "near_high" | "near_low" | "inside"
    struct_trend: str | None = None          # "up" | "down" | "choppy"
    day_high: float | None = None
    day_low: float | None = None
    # realised future move over the horizon (backtest only; None live)
    future_move_pt: float | None = None
    future_signed_move_pt: float | None = None   # +ve up, -ve down (for "perfect direction")
    # intra-trade path for exit simulation: (high_disp, low_disp, close_disp) per future bar,
    # displacement in points from the entry close. Backtest only.
    future_path: list[tuple[float, float, float]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "day": self.day, "index": self.index, "close": self.close,
            "atr_build": self.atr_build, "atr_base": self.atr_base,
            "compression_ratio": (self.atr_build / self.atr_base) if self.atr_base else None,
            "last_bar_return": self.last_bar_return,
            "option_volume": self.option_volume, "vol_build_avg": self.vol_build_avg,
            "oi_now": self.oi_now, "oi_15ago": self.oi_15ago,
            "max_pain": self.max_pain,
            "ce_oi_top_strike": self.ce_oi_top_strike, "pe_oi_top_strike": self.pe_oi_top_strike,
            "prior_day_high": self.prior_day_high, "prior_day_low": self.prior_day_low,
            "opening_range_high": self.opening_range_high, "opening_range_low": self.opening_range_low,
            "vwap": self.vwap, "fut_return_5m": self.fut_return_5m,
            "net_ofi": self.net_ofi,
            "ce_bid_strength": self.ce_bid_strength, "pe_bid_strength": self.pe_bid_strength,
            "spread_pct": self.spread_pct,
            "struct_breakout": self.struct_breakout, "struct_fakeout": self.struct_fakeout,
            "struct_position": self.struct_position, "struct_trend": self.struct_trend,
            "day_high": self.day_high, "day_low": self.day_low,
            "future_move_pt": self.future_move_pt, "future_signed_move_pt": self.future_signed_move_pt,
            **self.extras,
        }


def compression_ratio(context) -> float | None:
    """Short-window vol / baseline vol (lower = more compressed) — source-agnostic.

    Prefers the semantic ``compression_ratio`` (the live snapshot's ``vol_ratio`` =
    realized_vol_30m / minute-of-day baseline); falls back to the backtest's
    ``atr_build / atr_base``. Shared infra so Move and Regime read it identically
    without importing each other.
    """
    ratio = context.get("compression_ratio")
    if ratio is not None:
        return float(ratio)
    atr_build, atr_base = context.get("atr_build"), context.get("atr_base")
    if atr_base and atr_build is not None:
        return float(atr_build) / float(atr_base)
    return None


def _tr(h: float, l: float, pc: float) -> float:
    return max(h - l, abs(h - pc), abs(l - pc))


def _atr(H: list[float], L: list[float], C: list[float]) -> float:
    return sum(_tr(H[k], L[k], C[k - 1]) for k in range(1, len(H))) / max(len(H) - 1, 1)


def _none_in(values: list[Any]) -> bool:
    return any(v is None for v in values)


BREAKOUT_LOOKBACK = 20
NEAR_EDGE_FRAC = 0.2
TREND_LOOKBACK = 10
TREND_EPS = 0.001


def _structure_for_bar(bars: list[dict[str, Any]], i: int) -> dict[str, Any]:
    """Trader structure (highs/lows/breakouts) from the bars up to i. Lightweight analog
    of MarketStructureTracker for the backtest; the live path uses snapshot-native fields."""
    close = float(bars[i]["c"])
    day_h = max(float(x["h"]) for x in bars[: i + 1] if x.get("h") is not None)
    day_l = min(float(x["l"]) for x in bars[: i + 1] if x.get("l") is not None)
    win = bars[max(0, i - BREAKOUT_LOOKBACK):i]
    prior_high = max((float(x["h"]) for x in win if x.get("h") is not None), default=day_h)
    prior_low = min((float(x["l"]) for x in win if x.get("l") is not None), default=day_l)

    breakout = "up" if close > prior_high else "down" if close < prior_low else "none"
    recent = [float(x["c"]) for x in bars[max(0, i - 2):i] if x.get("c") is not None]
    fakeout = bool(recent and ((max(recent) > prior_high and close <= prior_high)
                               or (min(recent) < prior_low and close >= prior_low)))
    rng = max(day_h - day_l, 1e-9)
    position = ("near_high" if (day_h - close) < NEAR_EDGE_FRAC * rng
                else "near_low" if (close - day_l) < NEAR_EDGE_FRAC * rng else "inside")
    if i >= TREND_LOOKBACK and bars[i - TREND_LOOKBACK].get("c"):
        ref = float(bars[i - TREND_LOOKBACK]["c"])
        trend = ("up" if close > ref * (1 + TREND_EPS)
                 else "down" if close < ref * (1 - TREND_EPS) else "choppy")
    else:
        trend = "choppy"
    return {"struct_breakout": breakout, "struct_fakeout": fakeout, "struct_position": position,
            "struct_trend": trend, "day_high": day_h, "day_low": day_l}


def build_contexts(
    days_bars: dict[str, list[dict[str, Any]]],
    *,
    horizon: int = 10,
    levels: dict[str, dict[str, float | None]] | None = None,
) -> list[BarContext]:
    """Turn ``{day: [bar, ...]}`` into a flat list of per-bar :class:`BarContext`.

    ``bar`` keys: ``c/h/l`` (futures close/high/low), ``ovol`` (option volume),
    ``ooi`` (option OI), optional ``max_pain``/``ce_oi_top_strike``/
    ``pe_oi_top_strike``/``opening_range_high``/``opening_range_low``/``net_ofi``/
    ``ce_bid_strength``/``pe_bid_strength``/``spread_pct``.
    ``levels[day]`` may carry ``prior_day_high``/``prior_day_low``.
    """
    levels = levels or {}
    out: list[BarContext] = []
    for day, bars in days_bars.items():
        day_levels = levels.get(day, {})
        for i, b in enumerate(bars):
            if i < WARMUP or b.get("c") is None:
                continue
            prev = bars[i - 1]
            if prev.get("c") is None:
                continue
            H = [x["h"] for x in bars[i - BUILD_WINDOW:i]]
            L = [x["l"] for x in bars[i - BUILD_WINDOW:i]]
            C = [x["c"] for x in bars[i - BUILD_WINDOW - 1:i]]
            Hb = [x["h"] for x in bars[i - WARMUP + 1:i - BUILD_WINDOW]]
            Lb = [x["l"] for x in bars[i - WARMUP + 1:i - BUILD_WINDOW]]
            Cb = [x["c"] for x in bars[i - WARMUP:i - BUILD_WINDOW]]
            if _none_in(H + L + C + Hb + Lb + Cb):
                continue
            atr_build = _atr(H, L, C)
            atr_base = _atr(Hb, Lb, Cb)
            vol_build = sum(float(x.get("ovol") or 0.0) for x in bars[i - BUILD_WINDOW:i]) / BUILD_WINDOW
            struct = _structure_for_bar(bars, i)
            future = [x for x in bars[i + 1:i + 1 + horizon] if x.get("h") is not None and x.get("l") is not None]
            fut_move = fut_signed = None
            fut_path: list[tuple[float, float, float]] = []
            if future:
                entry = float(b["c"])
                up = max(float(x["h"]) for x in future) - entry
                down = entry - min(float(x["l"]) for x in future)
                fut_move = max(up, down)
                fut_signed = up if up >= down else -down
                fut_path = [(float(x["h"]) - entry, float(x["l"]) - entry, float(x["c"]) - entry)
                            for x in future]
            out.append(BarContext(
                day=day, index=i, close=float(b["c"]),
                atr_build=atr_build, atr_base=atr_base,
                last_bar_return=float(b["c"]) - float(prev["c"]),
                option_volume=float(b.get("ovol") or 0.0), vol_build_avg=vol_build,
                oi_now=float(b.get("ooi") or 0.0), oi_15ago=float(bars[i - BUILD_WINDOW].get("ooi") or 0.0),
                max_pain=b.get("max_pain"),
                ce_oi_top_strike=b.get("ce_oi_top_strike"), pe_oi_top_strike=b.get("pe_oi_top_strike"),
                prior_day_high=day_levels.get("prior_day_high"), prior_day_low=day_levels.get("prior_day_low"),
                opening_range_high=b.get("opening_range_high"), opening_range_low=b.get("opening_range_low"),
                vwap=b.get("vwap"), fut_return_5m=b.get("fut_return_5m"),
                net_ofi=b.get("net_ofi"),
                ce_bid_strength=b.get("ce_bid_strength"), pe_bid_strength=b.get("pe_bid_strength"),
                spread_pct=b.get("spread_pct"),
                struct_breakout=struct["struct_breakout"], struct_fakeout=struct["struct_fakeout"],
                struct_position=struct["struct_position"], struct_trend=struct["struct_trend"],
                day_high=struct["day_high"], day_low=struct["day_low"],
                future_move_pt=fut_move, future_signed_move_pt=fut_signed, future_path=fut_path,
            ))
    return out


__all__ = ["BarContext", "build_contexts", "compression_ratio", "BUILD_WINDOW", "BASE_WINDOW",
           "WARMUP", "COMPRESS_RATIO", "VOL_SPIKE", "VELOCITY_K", "OI_BUILD"]
