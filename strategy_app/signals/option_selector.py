"""Smart strike selector (Phase 1.3 of PROJECT_PLAN.md).

Replaces unconditional ATM selection with a confidence + IV-aware choice:
  - reject when IV percentile is at the top of its range (theta/vega will eat the move)
  - move 1-OTM when confidence is high and IV is reasonable (cheaper premium, more leverage on a real move)
  - fall back to ATM otherwise

Gated behind STRATEGY_SMART_STRIKE_ENABLED env var so the change is A/B-testable
against the legacy ATM path in the same replay.
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
    mode: str  # "atm" | "otm_1" | "rejected_high_iv" | "legacy_atm"


# Defaults are conservative and overridable via env for tuning without code changes.
# IMPORTANT: `snap.iv_percentile` is on the 0-100 scale (per snapshot's iv_derived.iv_percentile),
# NOT the 0-1 scale. Thresholds below match that convention.
_DEFAULT_IV_REJECT_PCTILE = 90.0
_DEFAULT_OTM_CONFIDENCE = 0.75
_DEFAULT_OTM_IV_CEIL = 50.0


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


def select_strike(snap: Any, direction: str, decision: Any) -> StrikeSelection:
    """Pick an option strike for the given direction.

    Returns a StrikeSelection. When `strike is None`, the caller MUST treat it as
    a hold/skip (e.g. IV regime makes the trade unviable).
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

    otm_strike = int(atm) + int(step) if direction == "CE" else int(atm) - int(step)

    # Confirm the OTM strike has a tradable LTP; otherwise fall back to ATM.
    ltp_fn = getattr(snap, "option_ltp", None)
    if callable(ltp_fn):
        ltp = ltp_fn(direction, otm_strike)
        if ltp is None or float(ltp) <= 0:
            return StrikeSelection(
                strike=int(atm),
                reason="atm_otm_missing_premium",
                confidence=confidence,
                iv_percentile=iv_pct,
                mode="atm",
            )

    return StrikeSelection(
        strike=otm_strike,
        reason=f"otm_1_confidence_{confidence:.3f}",
        confidence=confidence,
        iv_percentile=iv_pct,
        mode="otm_1",
    )
