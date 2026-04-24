"""Builder to reduce TradeSignal construction boilerplate across engines."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from strategy_app.contracts import Direction, SignalType, TradeSignal
from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.risk.manager import RiskManager

logger = logging.getLogger(__name__)


def build_ml_entry_signal(
    *,
    snap: SnapshotAccessor,
    decision: Any,
    underlying_stop_pct: Optional[float] = None,
    underlying_target_pct: Optional[float] = None,
    premium_risk_fallback_pct: float = 0.20,
    trailing_enabled: bool = True,
    risk_manager: RiskManager,
) -> TradeSignal:
    """Construct a TradeSignal for ML-pure staged entry.

    Collapses ~30 lines of dataclass construction into a single call.
    """
    direction = "CE" if decision.action == "BUY_CE" else "PE"
    strike = int(snap.atm_strike or 0)
    if strike <= 0:
        raise RuntimeError("build_ml_entry_signal requires a valid atm_strike")
    premium = float(snap.option_ltp(direction, strike) or 0)
    if premium <= 0:
        raise RuntimeError("build_ml_entry_signal requires a valid option premium")
    risk_basis = str(getattr(decision, "risk_basis", "option_premium") or "option_premium").strip().lower()
    raw_stop_loss_pct = float(decision.stop_loss_pct or 0.0)
    raw_target_pct = float(decision.target_pct or 0.0)
    if risk_basis == "underlying":
        signal_stop_loss_pct = 0.0
        signal_target_pct = 0.0
        underlying_stop_pct = raw_stop_loss_pct if raw_stop_loss_pct > 0 else underlying_stop_pct
        underlying_target_pct = raw_target_pct if raw_target_pct > 0 else underlying_target_pct
        sizing_stop_loss_pct = max(0.0, float(premium_risk_fallback_pct))
    else:
        signal_stop_loss_pct = raw_stop_loss_pct or 0.20
        signal_target_pct = raw_target_pct or 0.80
        sizing_stop_loss_pct = signal_stop_loss_pct
    # INVESTIGATION LOG: Trace L6 signal creation
    if str(decision.recipe_id) == "L6":
        logger.warning(
            f"[SIGNAL_BUILDER_TRACE] recipe=L6 risk_basis={risk_basis!r} "
            f"signal_stop_loss_pct={signal_stop_loss_pct:.6f} signal_target_pct={signal_target_pct:.6f} "
            f"underlying_stop_pct={underlying_stop_pct} underlying_target_pct={underlying_target_pct} "
            f"premium={premium:.2f}"
        )
    max_hold_bars = int(decision.horizon_minutes or 15)
    confidence = float(max(decision.ce_prob, decision.pe_prob))
    lots = risk_manager.compute_lots(
        entry_premium=premium,
        stop_loss_pct=sizing_stop_loss_pct,
        confidence=confidence,
    )
    return TradeSignal(
        signal_id=str(uuid.uuid4())[:8],
        timestamp=snap.timestamp_or_now,
        snapshot_id=snap.snapshot_id,
        signal_type=SignalType.ENTRY,
        direction=direction,
        strike=strike,
        entry_premium=premium,
        max_hold_bars=max_hold_bars,
        stop_loss_pct=signal_stop_loss_pct,
        target_pct=signal_target_pct,
        underlying_stop_pct=underlying_stop_pct,
        underlying_target_pct=underlying_target_pct,
        trailing_enabled=trailing_enabled,
        max_lots=lots,
        entry_strategy_name="ML_PURE_STAGED",
        entry_regime_name="staged_ml",
        source="ML_PURE",
        confidence=confidence,
        reason=(
            f"ml_pure_staged: action={decision.action} entry_prob={decision.entry_prob:.4f} "
            f"dir_up_prob={decision.direction_up_prob:.4f} recipe={decision.recipe_id} "
            f"risk_basis={risk_basis} "
            f"recipe_prob={decision.recipe_prob:.4f} recipe_margin={decision.recipe_margin:.4f} "
            f"reason={decision.reason}"
        ),
        votes=[],
    )
