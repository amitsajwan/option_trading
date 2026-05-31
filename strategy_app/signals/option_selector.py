"""Smart strike selector (Phase 1.3 of PROJECT_PLAN.md).

Replaces unconditional ATM selection with a confidence + IV-aware choice:
  - reject when IV percentile is at the top of its range (theta/vega will eat the move)
  - move 2-OTM when confidence is very high, IV is low, regime is BREAKOUT, and it's early session
  - move 1-OTM when confidence is high and IV is reasonable (cheaper premium, more leverage on a real move)
  - fall back to ATM otherwise

Gated behind STRATEGY_SMART_STRIKE_ENABLED env var so the change is A/B-testable
against the legacy ATM path in the same replay.

All thresholds are env-var overridable — no code changes needed to tune:

  STRATEGY_SMART_STRIKE_ENABLED=1       master switch
  SMART_STRIKE_IV_REJECT_PCTILE=90.0    reject trade entirely above this IV
  SMART_STRIKE_OTM_CONFIDENCE=0.75      min confidence to go 1-OTM
  SMART_STRIKE_OTM_IV_CEIL=50.0         max IV percentile allowed for 1-OTM
  SMART_STRIKE_OTM2_ENABLED=1           sub-switch for 2-OTM tier
  SMART_STRIKE_OTM2_CONFIDENCE=0.85     min confidence to go 2-OTM
  SMART_STRIKE_OTM2_IV_CEIL=30.0        max IV percentile allowed for 2-OTM
  SMART_STRIKE_OTM2_REGIMES=BREAKOUT    comma-separated regime allowlist (empty = any)
  SMART_STRIKE_OTM2_MAX_BAR_HOUR=11     only before this hour IST (0 = no restriction)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class StrikeSelection:
    strike: Optional[int]
    reason: str
    confidence: float
    iv_percentile: Optional[float]
    mode: str  # "atm" | "otm_1" | "otm_2" | "rejected_high_iv" | "legacy_atm"


# Defaults are conservative and overridable via env for tuning without code changes.
# IMPORTANT: `snap.iv_percentile` is on the 0-100 scale (per snapshot's iv_derived.iv_percentile),
# NOT the 0-1 scale. Thresholds below match that convention.
_DEFAULT_IV_REJECT_PCTILE = 90.0
_DEFAULT_OTM_CONFIDENCE = 0.75
_DEFAULT_OTM_IV_CEIL = 50.0
_DEFAULT_OTM2_CONFIDENCE = 0.85
_DEFAULT_OTM2_IV_CEIL = 30.0
_DEFAULT_OTM2_REGIMES = "BREAKOUT"
_DEFAULT_OTM2_MAX_BAR_HOUR = 11


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _confidence_for_direction(decision: Any, direction: str) -> float:
    if direction == "CE":
        value = getattr(decision, "ce_prob", None)
    elif direction == "PE":
        value = getattr(decision, "pe_prob", None)
    else:
        value = None
    if value is None:
        # Fall back to whichever side Stage 2 was more sure about.
        ce = float(getattr(decision, "ce_prob", 0.0) or 0.0)
        pe = float(getattr(decision, "pe_prob", 0.0) or 0.0)
        return max(ce, pe)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def select_strike(snap: Any, direction: str, decision: Any, regime: str = "") -> StrikeSelection:
    """Pick an option strike for the given direction.

    Returns a StrikeSelection. When `strike is None`, the caller MUST treat it as
    a hold/skip (e.g. IV regime makes the trade unviable).

    Args:
        snap: SnapshotAccessor providing atm_strike, iv_percentile, strike_step, option_ltp, timestamp.
        direction: "CE" or "PE".
        decision: object with ce_prob / pe_prob attributes.
        regime: current market regime string (e.g. "BREAKOUT", "SIDEWAYS"). Used for 2-OTM gating.
    """
    atm = getattr(snap, "atm_strike", None)
    confidence = _confidence_for_direction(decision, direction)
    iv_pct = getattr(snap, "iv_percentile", None)

    enabled = os.getenv("STRATEGY_SMART_STRIKE_ENABLED", "").strip() == "1"
    if not enabled:
        return StrikeSelection(
            strike=int(atm) if atm else None,
            reason="legacy_atm_path",
            confidence=confidence,
            iv_percentile=iv_pct,
            mode="legacy_atm",
        )

    if atm is None or int(atm) <= 0:
        return StrikeSelection(
            strike=None,
            reason="missing_atm_strike",
            confidence=confidence,
            iv_percentile=iv_pct,
            mode="atm",
        )

    iv_reject = _env_float("SMART_STRIKE_IV_REJECT_PCTILE", _DEFAULT_IV_REJECT_PCTILE)
    if iv_pct is not None and float(iv_pct) > iv_reject:
        return StrikeSelection(
            strike=None,
            reason=f"iv_percentile_above_{iv_reject:.2f}",
            confidence=confidence,
            iv_percentile=float(iv_pct),
            mode="rejected_high_iv",
        )

    otm_conf = _env_float("SMART_STRIKE_OTM_CONFIDENCE", _DEFAULT_OTM_CONFIDENCE)
    otm_iv_ceil = _env_float("SMART_STRIKE_OTM_IV_CEIL", _DEFAULT_OTM_IV_CEIL)

    take_otm = (
        confidence >= otm_conf
        and (iv_pct is None or float(iv_pct) <= otm_iv_ceil)
    )

    if not take_otm:
        return StrikeSelection(
            strike=int(atm),
            reason=f"atm_confidence_{confidence:.3f}",
            confidence=confidence,
            iv_percentile=iv_pct,
            mode="atm",
        )

    step_fn = getattr(snap, "strike_step", None)
    step = step_fn() if callable(step_fn) else None
    if step is None or int(step) <= 0:
        return StrikeSelection(
            strike=int(atm),
            reason="atm_no_strike_step",
            confidence=confidence,
            iv_percentile=iv_pct,
            mode="atm",
        )

    otm1_strike = int(atm) + int(step) if direction == "CE" else int(atm) - int(step)

    # Confirm the OTM strike has a tradable LTP; otherwise fall back to ATM.
    ltp_fn = getattr(snap, "option_ltp", None)
    if callable(ltp_fn):
        ltp1 = ltp_fn(direction, otm1_strike)
        if ltp1 is None or float(ltp1) <= 0:
            return StrikeSelection(
                strike=int(atm),
                reason="atm_otm_missing_premium",
                confidence=confidence,
                iv_percentile=iv_pct,
                mode="atm",
            )

    # --- 2-OTM tier: higher conviction, low IV, correct regime, early session ---
    if os.getenv("SMART_STRIKE_OTM2_ENABLED", "").strip() == "1":
        otm2_conf = _env_float("SMART_STRIKE_OTM2_CONFIDENCE", _DEFAULT_OTM2_CONFIDENCE)
        otm2_iv_ceil = _env_float("SMART_STRIKE_OTM2_IV_CEIL", _DEFAULT_OTM2_IV_CEIL)
        otm2_max_hour = int(_env_float("SMART_STRIKE_OTM2_MAX_BAR_HOUR", _DEFAULT_OTM2_MAX_BAR_HOUR))

        otm2_regimes_raw = os.getenv("SMART_STRIKE_OTM2_REGIMES", _DEFAULT_OTM2_REGIMES)
        otm2_regimes = {r.strip().upper() for r in otm2_regimes_raw.split(",") if r.strip()}

        # Hour gate: only take 2-OTM before otm2_max_hour IST (0 = no restriction)
        hour_ok = True
        if otm2_max_hour > 0:
            ts = getattr(snap, "timestamp", None)
            if ts is not None:
                hour_ok = ts.hour < otm2_max_hour

        # Regime gate: empty allowlist means any regime is fine
        regime_ok = (not otm2_regimes) or (regime.upper() in otm2_regimes)

        take_otm2 = (
            confidence >= otm2_conf
            and (iv_pct is None or float(iv_pct) <= otm2_iv_ceil)
            and regime_ok
            and hour_ok
        )

        if take_otm2:
            otm2_strike = otm1_strike + int(step) if direction == "CE" else otm1_strike - int(step)
            if callable(ltp_fn):
                ltp2 = ltp_fn(direction, otm2_strike)
                if ltp2 is not None and float(ltp2) > 0:
                    return StrikeSelection(
                        strike=otm2_strike,
                        reason=f"otm_2_conf_{confidence:.3f}_iv_{iv_pct}_regime_{regime or 'any'}",
                        confidence=confidence,
                        iv_percentile=iv_pct,
                        mode="otm_2",
                    )
            # 2-OTM LTP missing — fall through to 1-OTM

    return StrikeSelection(
        strike=otm1_strike,
        reason=f"otm_1_confidence_{confidence:.3f}",
        confidence=confidence,
        iv_percentile=iv_pct,
        mode="otm_1",
    )
