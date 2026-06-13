"""Non-ML volatility-gate entry strategy.

A transparent, swap-in alternative to ML_ENTRY. The entry *trigger* is simply
"current volatility is elevated" — ``atr_14_1m`` as a fraction of price. On live
data this single rule matched/beat the 57-feature ML entry model (AUC 0.722 vs
0.717), because that model was ~42% ATR anyway — but without the decay,
isotonic-plateau calibration, or xgboost-version fragility.

Direction is resolved by the SAME shared policy as ML_ENTRY
(:func:`resolve_direction_for_entry`), so the two are a clean A/B: only the
trigger differs.

Config (env):
  ENTRY_VOL_GATE_ENABLED   : "1" to activate this strategy (default off)
  ATR_ENTRY_MIN_PCT        : gate on atr_14_1m/price >= this (default 0.00088
                             ~ p90 of live ATR; ~3x lift over base move rate).
                             Level-invariant by design (won't drift as the index
                             level rises — the flaw that decayed the ML model).
  ATR_ENTRY_MIN_ABS        : if >0, gate on absolute atr_14_1m >= this instead
                             of the pct (escape hatch; default 0 = use pct).
  ATR_ENTRY_BB_MIN         : optional bb_width_5m confirm (default 0 = off).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ...contracts import (
    BaseStrategy,
    Direction,
    PositionContext,
    RiskContext,
    SignalType,
    SnapshotPayload,
    StrategyVote,
)
from ...market.snapshot_accessor import SnapshotAccessor
from .entry_direction_policy import _env_float, resolve_direction_for_entry

logger = logging.getLogger(__name__)

STRATEGY_NAME = "VOL_GATE_ENTRY"


class VolGateEntryStrategy(BaseStrategy):
    """Entry votes triggered by elevated volatility (atr_14_1m), no ML."""

    name = STRATEGY_NAME

    def __init__(self) -> None:
        self._min_pct: float = _env_float("ATR_ENTRY_MIN_PCT", 0.00088)
        self._min_abs: float = _env_float("ATR_ENTRY_MIN_ABS", 0.0)
        self._bb_min: float = _env_float("ATR_ENTRY_BB_MIN", 0.0)
        logger.info(
            "vol_gate_entry: min_pct=%.5f min_abs=%.2f bb_min=%.4f",
            self._min_pct, self._min_abs, self._bb_min,
        )

    @staticmethod
    def _mtf(snap: SnapshotAccessor, key: str) -> Optional[float]:
        block = snap.raw_payload.get("mtf_derived")
        if not isinstance(block, dict):
            return None
        val = block.get(key)
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        if position is not None:
            return None
        snap = SnapshotAccessor(snapshot)
        atr = self._mtf(snap, "atr_14_1m")
        price = snap.fut_close
        if atr is None or price is None or price <= 0:
            return None
        atr_pct = atr / price

        # trigger: absolute ATR if configured, else level-invariant ATR %.
        if self._min_abs > 0:
            fired = atr >= self._min_abs
            gate_val, gate_min = atr, self._min_abs
        else:
            fired = atr_pct >= self._min_pct
            gate_val, gate_min = atr_pct, self._min_pct

        # optional volatility-confirm
        if self._bb_min > 0:
            bb = self._mtf(snap, "bb_width_5m")
            if bb is None or bb < self._bb_min:
                fired = False

        # record diag every bar (parity with ML_ENTRY's separation trace)
        try:
            from ...runtime.eval_context import set_entry_diag
            set_entry_diag({
                "atr_pct": round(atr_pct, 6),
                "atr_14_1m": round(atr, 3),
                "threshold": round(gate_min, 6),
                "fired": bool(fired),
                "snapshot_id": snap.snapshot_id,
            })
        except Exception:
            pass
        if not fired:
            return None

        direction, raw_signals = resolve_direction_for_entry(snap)
        if direction is None:
            return None

        # confidence scales with how far above the gate we are: at the gate -> 0.5,
        # at 2x the gate -> 1.0. Keeps downstream confidence ranking meaningful.
        conf = min(1.0, max(0.5, gate_val / (2.0 * gate_min))) if gate_min > 0 else 0.5
        raw_signals = {
            "atr_14_1m": round(atr, 3),
            "atr_pct": round(atr_pct, 6),
            "atr_threshold": round(gate_min, 6),
            "trigger": "vol_gate",
            **raw_signals,
        }
        premium = snap.atm_ce_close if direction == Direction.CE else snap.atm_pe_close
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.ENTRY,
            direction=direction,
            confidence=round(conf, 3),
            reason=f"vol_gate: atr_pct={atr_pct:.5f}>={gate_min:.5f}",
            raw_signals=raw_signals,
            proposed_strike=snap.atm_strike,
            proposed_entry_premium=premium,
        )
