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
        "prior_day_high": None,
        "prior_day_low": None,
        # execution
        "spread_pct": None,
        # cost/ev premium (lets CostEvSense use the live ATM premium instead of a default)
        "atm_premium": snap.atm_premium,
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
