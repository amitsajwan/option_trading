"""Cost-ratio entry gate — "if we are right, does the move clear cost?".

This is the P&L-relevant selectivity lever (decision: arm B). The ML model is the
magnitude predictor (our strongest signal, AUC 0.82); this gate sits AFTER the ML
floor and removes the trades whose expected move is too small to clear round-trip
cost — the "+5% gross / −43% net" bleed the live history is full of.

Direction-agnostic by design: it asks only whether a RIGHT-SIDE move pays. Direction
is resolved separately and may still veto. This gate can only REMOVE trades, never
create or rescue one (penalties subtract; nothing adds — see docs/ENGINE_DECISION_FLOW).

Physics are reused from CostEvSense (the empirical-anchor calibration, handover §1):
    right-side premium gain %  ≈  right_slope × expected_move_pt
    round-trip cost %          ≈  cost_model on the live ATM premium
    cost_ratio                 =  gain_if_right_pct / cost_pct

Expected move (per-bar, varies with vol so the gate actually bites on quiet bars):
    expected_move_pt = atr_ratio × spot × sqrt(hold_bars)
    (atr_ratio is a fraction of price in futures_derived; confirmed units 2026-06-20)

FAIL-SAFE: if any input is missing (no atr_ratio / spot / premium), the gate PASSES
(returns ok=True, reason="insufficient_data") and logs debug — a missing feature must
never silently block trading. The trace records what happened either way.

COST CALIBRATION (important): CostEvSense.cost_pct() is BROKERAGE+TAXES only (~0.47%).
The real all-in cost adds slippage, which memory flags as the dominant, UNMEASURED
component (~0.8-1%). Until the depth feed (2026-06-23) lets us measure slippage from
the live bid-ask spread, we add a flat ``ENTRY_COST_SLIPPAGE_PCT`` placeholder so the
gate bites at the empirically-right level (~1.3% all-in ≈ the "108pt cost wall").
When depth is live, replace the flat placeholder with the measured half-spread.

Env knobs:
    ENTRY_COST_RATIO_GATE_ENABLED  default 1
    ENTRY_COST_RATIO_MIN           default 1.5   (gain must be ≥1.5× all-in cost)
    ENTRY_COST_HOLD_BARS           default 10    (hold horizon for the sqrt scale)
    ENTRY_COST_SLIPPAGE_PCT        default 0.008 (placeholder until depth-measured)
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, NamedTuple, Optional

from ...senses.cost_ev import REF_MOVE_PT, CostEvSense
from ...constants import resolve_lot_size

# Spot level at which the empirical gain/move anchors (REF_MOVE_PT=117pt,
# RIGHT_PCT_AT_REF=4%) were measured — BankNifty's ~2026 level. The reference
# move scales with the live spot so the "gain%-per-underlying-point" anchor is
# comparable across instruments: an option's premium and the index's point-move
# both scale ~linearly with spot, so a fixed 117pt anchor would understate a
# lower-priced index's gain (NIFTY ~24k => moves ~half BankNifty's in points but
# the SAME % move). BankNifty (spot ≈ this) is byte-identical; NIFTY gets a
# proportionally smaller, correctly-scaled reference move.
_ANCHOR_REF_SPOT = 52000.0

logger = logging.getLogger(__name__)


def _measure_slippage() -> tuple[float, str]:
    """Round-trip slippage as a fraction of premium.

    Prefers the LIVE ATM bid-ask spread from the depth feed (relative_spread =
    spread/bid ≈ the cost of crossing the book on entry+exit). Falls back to the
    flat ENTRY_COST_SLIPPAGE_PCT placeholder when depth is absent/stale/insane —
    so the gate never breaks if depth is unavailable. Direction (CE/PE) isn't
    known at cost-gate time, so we average the available ATM CE/PE spreads.
    """
    flat = _env_float("ENTRY_COST_SLIPPAGE_PCT", 0.008)
    try:
        from ...runtime.eval_context import get_depth_context
        ctx = get_depth_context()
        if ctx is not None and getattr(ctx, "is_available", False):
            rs = []
            for leg in (getattr(ctx, "ce", None), getattr(ctx, "pe", None)):
                v = getattr(leg, "relative_spread", None) if leg is not None else None
                if v is not None and 0.0 < float(v) < 0.05:  # sane: <5% spread
                    rs.append(float(v))
            if rs:
                return sum(rs) / len(rs), "depth_measured"
    except Exception:
        logger.debug("depth slippage read failed (ignored, flat fallback)", exc_info=True)
    return flat, "flat_placeholder"


class CostGateResult(NamedTuple):
    ok: bool                  # True = proceed (pass OR insufficient-data fail-safe)
    ratio: Optional[float]    # gain_if_right_pct / cost_pct, None if not computable
    reason: str               # human-readable outcome
    evidence: dict[str, Any]  # for the decision trace


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


def evaluate_cost_gate(snap: Any) -> CostGateResult:
    """Apply the cost-ratio gate to a SnapshotAccessor. Never raises."""
    if not _env_bool("ENTRY_COST_RATIO_GATE_ENABLED", True):
        return CostGateResult(True, None, "disabled", {"enabled": False})

    ratio_min = _env_float("ENTRY_COST_RATIO_MIN", 1.5)
    hold_bars = max(1.0, _env_float("ENTRY_COST_HOLD_BARS", 10.0))

    try:
        fd = snap.raw_payload.get("futures_derived") or {}
        atr_ratio = fd.get("atr_ratio")
        spot = snap.fut_close
        premium = snap.atm_premium
    except Exception:
        logger.debug("cost_gate: snapshot read failed (ignored, pass)", exc_info=True)
        return CostGateResult(True, None, "insufficient_data", {"error": "snapshot_read"})

    # Fail-safe: missing inputs must not block trading.
    if atr_ratio is None or not spot or not premium:
        return CostGateResult(
            True, None, "insufficient_data",
            {"atr_ratio": atr_ratio, "spot": spot, "premium": premium},
        )

    try:
        expected_move_pt = float(atr_ratio) * float(spot) * math.sqrt(hold_bars)
        # Instrument-correct the cost/gain physics:
        #  - lot_qty: actual contract size (NIFTY=75) so brokerage% amortizes over
        #    the real notional (primary_default=30 preserves BankNifty's behavior).
        #  - ref_move_pt: scale the gain anchor by spot so a lower-priced index's
        #    smaller point-moves are referenced against a proportionally smaller
        #    move (BankNifty spot ≈ _ANCHOR_REF_SPOT => unchanged).
        ref_move_scaled = REF_MOVE_PT * (float(spot) / _ANCHOR_REF_SPOT)
        sense = CostEvSense(
            premium_pts=float(premium),
            lot_qty=resolve_lot_size(primary_default=30),
            ref_move_pt=ref_move_scaled,
        )
        gain_if_right_pct = sense.right_at(expected_move_pt)   # right_slope × move
        brokerage_pct = sense.cost_pct()
        slippage_pct, slippage_source = _measure_slippage()  # live depth spread or flat fallback
        cost_pct = brokerage_pct + slippage_pct
        if cost_pct <= 0:
            return CostGateResult(True, None, "insufficient_data", {"cost_pct": cost_pct})
        ratio = gain_if_right_pct / cost_pct
    except Exception:
        logger.debug("cost_gate: compute failed (ignored, pass)", exc_info=True)
        return CostGateResult(True, None, "insufficient_data", {"error": "compute"})

    ok = ratio >= ratio_min
    evidence = {
        "expected_move_pt": round(expected_move_pt, 1),
        "gain_if_right_pct": round(gain_if_right_pct, 4),
        "brokerage_pct": round(brokerage_pct, 4),
        "slippage_pct": round(slippage_pct, 4),
        "slippage_source": slippage_source,
        "cost_pct": round(cost_pct, 4),
        "cost_ratio": round(ratio, 2),
        "ratio_min": ratio_min,
        "hold_bars": hold_bars,
    }
    reason = f"cost_ratio={ratio:.2f}{'>=' if ok else '<'}{ratio_min}"
    return CostGateResult(ok, round(ratio, 2), reason, evidence)


__all__ = ["evaluate_cost_gate", "CostGateResult"]
