"""Live snapshot -> sense-input context (board: entry wiring, the "context bridge").

STATELESS. The windowing already lives in the snapshot pipeline (``futures_derived``:
``vol_ratio`` = realized_vol_30m / minute-of-day baseline, ``fut_oi_change_30m``,
``fut_return_1m``, opening range, vwap, OI walls). This adapter only *maps* those
fields onto the semantic keys the senses read — no rolling buffer in the engine.

Fidelity note: the live ``compression_ratio`` comes from ``vol_ratio`` (30-min realized
vol vs a seasonality-adjusted baseline), which is a slightly different window than the
Phase-0 proof's 15-vs-26-bar ATR ratio — the same phenomenon, arguably a better
baseline. If bit-exact reproduction of the B-0.1 ``loaded`` rate is required, add the
proof's exact ``atr_build``/``atr_base`` to ``futures_derived`` (the one correct home)
and this adapter will read them via the same ``compression_ratio`` key.
"""
from __future__ import annotations

from typing import Any, Optional

from ..market.snapshot_accessor import SnapshotAccessor
from .context import VELOCITY_K, VOL_SPIKE


def _structure_from_snapshot(snap, close, prev_high, prev_low) -> dict[str, Any]:
    """Stateless structure read from snapshot-native fields (EMA stack + ORB + prior-day H/L).

    A lighter analog of MarketStructureTracker (which is stateful and runs in the engine).
    The engine shadow can overlay the full tracker's swing pivots later; this is enough for
    breakout/fakeout/trend without engine state.

    Liquidity sweeps (board §12.2): a sweep is a single-bar trap — the bar pierced a prior-day
    extreme intrabar (``fut_high`` > PDH / ``fut_low`` < PDL) but the *close* came back inside.
    That is the ICT "swept the pool then rejected" pattern, and it is treated as a ``fakeout``
    (the existing trap verdict), so it routes through the brain's ``loaded_into_fakeout`` conflict.
    ``sweep_direction`` ("up" = took upside liquidity then fell back) is recorded as EVIDENCE
    only — direction-agnostic by design, mirroring how breakout direction is recorded but never
    voted as a side. Feeding sweeps into the DIRECTION sense is deliberately deferred to §12.3.
    """
    # trend from the EMA stack (the trader's quick trend read)
    e9, e21, e50 = snap.ema_9, snap.ema_21, snap.ema_50
    if None not in (e9, e21, e50):
        trend = "up" if e9 > e21 > e50 else "down" if e9 < e21 < e50 else "choppy"
    else:
        trend = None

    pvorh, pvorl = snap.price_vs_orh, snap.price_vs_orl   # +ve above ORH / below ORL
    orh_broken, orl_broken = snap.orh_broken, snap.orl_broken

    broke_up = (prev_high is not None and close is not None and close > prev_high) or bool(orh_broken and (pvorh or 0) > 0)
    broke_down = (prev_low is not None and close is not None and close < prev_low) or bool(orl_broken and (pvorl or 0) < 0)
    breakout = "up" if broke_up else "down" if broke_down else "none"

    # prior-day liquidity sweep: bar pierced PDH/PDL intrabar but closed back inside (a trap)
    bar_high, bar_low = snap.fut_high, snap.fut_low
    swept_up = bool(prev_high is not None and close is not None and bar_high is not None
                    and bar_high > prev_high and close < prev_high)
    swept_down = bool(prev_low is not None and close is not None and bar_low is not None
                      and bar_low < prev_low and close > prev_low)
    sweep_direction = "up" if swept_up else "down" if swept_down else "none"
    swept = swept_up or swept_down

    # fakeout: broke the opening range, OR swept a prior-day extreme, then snapped back inside
    orb_fakeout = bool((orh_broken and (pvorh is not None) and pvorh < 0)
                       or (orl_broken and (pvorl is not None) and pvorl > 0))
    fakeout = orb_fakeout or swept

    position = None
    if close is not None and prev_high is not None and prev_low is not None and prev_high > prev_low:
        rng = prev_high - prev_low
        if (prev_high - close) < 0.2 * rng:
            position = "near_high"
        elif (close - prev_low) < 0.2 * rng:
            position = "near_low"
        else:
            position = "inside"

    return {"struct_breakout": breakout, "struct_fakeout": fakeout,
            "struct_swept": swept, "struct_sweep_direction": sweep_direction,
            "struct_position": position, "struct_trend": trend,
            "day_high": prev_high, "day_low": prev_low}


def snapshot_to_sense_context(
    snap: SnapshotAccessor,
    *,
    risk_ctx: Optional[dict[str, Any]] = None,
    depth_ctx: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    # NOTE: SnapshotAccessor exposes these as @property (no call parens).
    close = snap.fut_close
    ret_pct = snap.fut_return_1m
    realized_vol = snap.realized_vol_30m

    # last bar return in POINTS (conflict B / velocity derive use points)
    last_bar_return = (float(ret_pct) * float(close)) if (ret_pct is not None and close is not None) else 0.0
    # optional release-timing flags derived from snapshot-native fields (B-0.2: loaded-alone is fine)
    velocity_flag = None
    if ret_pct is not None and realized_vol:
        velocity_flag = abs(float(ret_pct)) > VELOCITY_K * float(realized_vol)
    vol_ratio_volume = snap.fut_volume_ratio
    volume_flag = (float(vol_ratio_volume) > VOL_SPIKE) if vol_ratio_volume is not None else None

    prev_high, prev_low = snap.prev_day_high, snap.prev_day_low
    struct = _structure_from_snapshot(snap, close, prev_high, prev_low)

    ctx: dict[str, Any] = {
        "close": close,
        # compression: vol_ratio < COMPRESS_RATIO == coiled (lower = quieter than baseline)
        "compression_ratio": snap.vol_ratio,
        # oi build: signed 30-min OI change (>0 == building)
        "oi_change": snap.fut_oi_change_30m,
        "last_bar_return": last_bar_return,
        "velocity_flag": velocity_flag,
        "volume_flag": volume_flag,
        # destination levels (always-present runtime feeds)
        "max_pain": snap.max_pain,
        "ce_oi_top_strike": snap.ce_oi_top_strike,
        "pe_oi_top_strike": snap.pe_oi_top_strike,
        "opening_range_high": snap.orh,
        "opening_range_low": snap.orl,
        "prior_day_high": prev_high,
        "prior_day_low": prev_low,
        # weekly levels (always-present session_levels feed — same source as prior-day H/L)
        "week_high": snap.week_high,
        "week_low": snap.week_low,
        # direction inputs (measured signals: VWAP bias + 5-min momentum)
        "vwap": snap.vwap,
        "fut_return_5m": snap.fut_return_5m,
        # structure (trader highs/lows/breakouts — stateless analog of MarketStructureTracker)
        **struct,
        # execution
        "spread_pct": None,
        # cost/ev premium (lets CostEvSense use the live ATM premium instead of a default)
        "atm_premium": snap.atm_premium,
        # expiry (lets the brain factor days-to-expiry into moneyness/cost reasoning)
        "days_to_expiry": snap.days_to_expiry,
    }

    # flow/OFI — only when depth is present; else FlowSense abstains (handover: depth optional)
    if depth_ctx:
        ctx["net_ofi"] = depth_ctx.get("net_ofi", depth_ctx.get("qty_imbalance"))
        ctx["ce_bid_strength"] = depth_ctx.get("ce_bid_strength")
        ctx["pe_bid_strength"] = depth_ctx.get("pe_bid_strength")
        if depth_ctx.get("spread_pct") is not None:
            ctx["spread_pct"] = depth_ctx.get("spread_pct")

    # risk state from the live tracker (else RiskSense reads safe defaults)
    if risk_ctx:
        ctx["daily_dd"] = risk_ctx.get("daily_dd", 0.0)
        ctx["consec_losses"] = risk_ctx.get("consec_losses", 0)
        ctx["in_position"] = risk_ctx.get("in_position", False)

    return ctx


__all__ = ["snapshot_to_sense_context"]
