"""Smart strike selector — tiered ATM/OTM selection based on confidence, IV, regime, OI.

Evaluates up to 4 OTM tiers (deepest first) and returns the deepest one that passes
all gates, falling back to shallower tiers or ATM. No code changes needed to tune —
every threshold is an env var.

Master switch:
  STRATEGY_SMART_STRIKE_ENABLED=1

Premium budget:
  SMART_STRIKE_MAX_PREMIUM=600         0 = no cap; try deepest within budget first.
  SMART_STRIKE_HARD_PREMIUM_CAP=1      1 = HARD cap (DEFAULT): the budget is a veto.
                                       If no affordable, priced strike exists, SKIP
                                       the trade (strike=None) — strike/depth
                                       analysis is part of the decision, like
                                       IV-too-high. Only bites when a cap is set.
                                       0 = SOFT cap (legacy): fall back to the
                                       best/ATM strike anyway, even over budget.

IV hard reject (trade skipped entirely):
  SMART_STRIKE_IV_REJECT_PCTILE=90.0

Tier 1 — 1-OTM (~300pt away, ~850 premium):
  SMART_STRIKE_OTM_CONFIDENCE=0.55      min confidence (entry gate for ANY OTM)
  SMART_STRIKE_OTM_IV_CEIL=60.0         max IV percentile

Tier 2 — 2-OTM (~200pt away, ~550 premium):
  SMART_STRIKE_OTM2_ENABLED=1
  SMART_STRIKE_OTM2_CONFIDENCE=0.65
  SMART_STRIKE_OTM2_IV_CEIL=50.0
  SMART_STRIKE_OTM2_REGIMES=            (empty = any regime)
  SMART_STRIKE_OTM2_MAX_BAR_HOUR=0      (0 = no hour restriction)
  SMART_STRIKE_OTM2_MIN_OI=100000       min open interest at the OTM strike

Tier 3 — 3-OTM (~300pt away, ~350 premium):
  SMART_STRIKE_OTM3_ENABLED=1
  SMART_STRIKE_OTM3_CONFIDENCE=0.75
  SMART_STRIKE_OTM3_IV_CEIL=40.0
  SMART_STRIKE_OTM3_REGIMES=BREAKOUT,TRENDING
  SMART_STRIKE_OTM3_MAX_BAR_HOUR=12
  SMART_STRIKE_OTM3_MIN_OI=75000

Tier 4 — 4-OTM (~400pt away, ~200 premium):
  SMART_STRIKE_OTM4_ENABLED=1
  SMART_STRIKE_OTM4_CONFIDENCE=0.85
  SMART_STRIKE_OTM4_IV_CEIL=30.0
  SMART_STRIKE_OTM4_REGIMES=BREAKOUT
  SMART_STRIKE_OTM4_MAX_BAR_HOUR=11
  SMART_STRIKE_OTM4_MIN_OI=50000

NOTE: BankNifty strike step is 100pt. Tiers in step-counts:
  1-OTM = 1 step, 2-OTM = 2 steps, 3-OTM = 3 steps, 4-OTM = 4 steps.
  With step=100pt: 4-OTM = 400pt away from ATM → ~200-350 premium range.
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
    mode: str  # "atm" | "otm_1".."otm_4" | "rejected_high_iv" | "legacy_atm"
    otm_steps: int = 0  # 0 = ATM, 1..4 = OTM depth


@dataclass(frozen=True)
class _TierConfig:
    n: int            # OTM steps (1–4)
    conf_min: float
    iv_ceil: float
    regimes: frozenset  # empty = any regime allowed
    max_hour: int     # 0 = no restriction
    min_oi: float     # 0 = no OI gate


# Conservative production defaults — tune via env vars, not code.
# NOTE: *_IV_CEIL is compared against snap.iv_percentile (0–100), so these MUST
# be percentile thresholds, not absolute IV. They were previously 30–60 (read
# like absolute IV%), which rejected ALL OTM tiers whenever IV percentile > 60
# — i.e. most active days — forcing every trade to ATM. Corrected to percentile
# ceilings: deeper tiers gated slightly tighter, but all reachable in normal IV.
_DEFAULTS: dict[str, Any] = {
    "IV_REJECT_PCTILE": 90.0,
    # Tier 1 entry gate (also gate for ALL OTM)
    "OTM_CONFIDENCE": 0.55,
    "OTM_IV_CEIL": 92.0,
    # Tier 2
    "OTM2_CONFIDENCE": 0.65,
    "OTM2_IV_CEIL": 91.0,
    "OTM2_REGIMES": "",
    "OTM2_MAX_BAR_HOUR": 0,
    "OTM2_MIN_OI": 75_000.0,
    # Tier 3
    "OTM3_CONFIDENCE": 0.75,
    "OTM3_IV_CEIL": 90.0,
    "OTM3_REGIMES": "BREAKOUT,TRENDING",
    "OTM3_MAX_BAR_HOUR": 12,
    "OTM3_MIN_OI": 75_000.0,
    # Tier 4
    "OTM4_CONFIDENCE": 0.85,
    "OTM4_IV_CEIL": 89.0,
    "OTM4_REGIMES": "BREAKOUT",
    "OTM4_MAX_BAR_HOUR": 11,
    "OTM4_MIN_OI": 50_000.0,
}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_regimes(name: str, default: str) -> frozenset:
    raw = os.getenv(name, default)
    return frozenset(r.strip().upper() for r in raw.split(",") if r.strip())


def _build_otm_tiers() -> list[_TierConfig]:
    """Return enabled OTM tiers sorted deepest-first.

    The max depth is bounded by STRATEGY_STRIKE_MAX_OTM_STEPS (default 4).
    Tiers 5–8 use the same *_ENABLED / *_CONFIDENCE / *_IV_CEIL / *_REGIMES env
    pattern as tiers 2–4; add SMART_STRIKE_OTM5_ENABLED=1 etc. to activate them.
    Defaults for tiers 5–8 follow the same progression (higher confidence + tighter
    IV ceilings than the inner tiers) so they are off until explicitly configured.
    """
    max_steps = int(_env_float("STRATEGY_STRIKE_MAX_OTM_STEPS", 4.0))
    max_steps = max(1, min(max_steps, 8))   # safety: clamp to [1, 8]

    tiers: list[_TierConfig] = []

    # Tier 1 is always included (no separate _ENABLED flag)
    tiers.append(_TierConfig(
        n=1,
        conf_min=_env_float("SMART_STRIKE_OTM_CONFIDENCE", _DEFAULTS["OTM_CONFIDENCE"]),
        iv_ceil=_env_float("SMART_STRIKE_OTM_IV_CEIL", _DEFAULTS["OTM_IV_CEIL"]),
        regimes=frozenset(),
        max_hour=0,
        min_oi=0.0,
    ))

    # Tiers 2–8: each requires explicit _ENABLED=1 AND must be within max_steps.
    # Tiers 2–4 have explicit _DEFAULTS; 5–8 use a linear extrapolation so they
    # have sane (very strict) defaults if activated without fine-tuning.
    for n in range(2, max_steps + 1):
        tag = f"OTM{n}"
        if os.getenv(f"SMART_STRIKE_{tag}_ENABLED", "").strip() != "1":
            continue

        if n <= 4:
            # Tier has explicit defaults
            conf_default  = _DEFAULTS[f"{tag}_CONFIDENCE"]
            iv_ceil_def   = _DEFAULTS[f"{tag}_IV_CEIL"]
            regimes_def   = _DEFAULTS[f"{tag}_REGIMES"]
            max_hour_def  = _DEFAULTS[f"{tag}_MAX_BAR_HOUR"]
            min_oi_def    = _DEFAULTS[f"{tag}_MIN_OI"]
        else:
            # Tier 5–8: conservative extrapolated defaults (conf rises, IV ceil tightens)
            conf_default  = min(0.97, _DEFAULTS["OTM4_CONFIDENCE"] + 0.02 * (n - 4))
            iv_ceil_def   = max(80.0, _DEFAULTS["OTM4_IV_CEIL"] - 2.0 * (n - 4))
            regimes_def   = "BREAKOUT"
            max_hour_def  = 10
            min_oi_def    = max(20_000.0, _DEFAULTS["OTM4_MIN_OI"] - 5_000.0 * (n - 4))

        tiers.append(_TierConfig(
            n=n,
            conf_min=_env_float(f"SMART_STRIKE_{tag}_CONFIDENCE", conf_default),
            iv_ceil=_env_float(f"SMART_STRIKE_{tag}_IV_CEIL", iv_ceil_def),
            regimes=_env_regimes(f"SMART_STRIKE_{tag}_REGIMES", regimes_def),
            max_hour=int(_env_float(f"SMART_STRIKE_{tag}_MAX_BAR_HOUR", max_hour_def)),
            min_oi=_env_float(f"SMART_STRIKE_{tag}_MIN_OI", min_oi_def),
        ))

    # Deepest tier first so we return the best possible strike
    return sorted(tiers, key=lambda t: t.n, reverse=True)


def _confidence_for_direction(decision: Any, direction: str) -> float:
    if direction == "CE":
        value = getattr(decision, "ce_prob", None)
    elif direction == "PE":
        value = getattr(decision, "pe_prob", None)
    else:
        value = None
    if value is None:
        ce = float(getattr(decision, "ce_prob", 0.0) or 0.0)
        pe = float(getattr(decision, "pe_prob", 0.0) or 0.0)
        return max(ce, pe)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _tier_passes(
    tier: _TierConfig,
    confidence: float,
    iv_pct: Optional[float],
    regime: str,
    snap: Any,
    direction: str,
    strike: int,
) -> bool:
    """Return True if all gates pass for this tier at the given strike."""
    if confidence < tier.conf_min:
        return False
    if iv_pct is not None and float(iv_pct) > tier.iv_ceil:
        return False
    if tier.regimes and regime.upper() not in tier.regimes:
        return False
    if tier.max_hour > 0:
        ts = getattr(snap, "timestamp", None)
        if ts is not None and ts.hour >= tier.max_hour:
            return False
    if tier.min_oi > 0:
        oi_fn = getattr(snap, "option_oi", None)
        if callable(oi_fn):
            oi = oi_fn(direction, strike)
            if oi is None or float(oi) < tier.min_oi:
                return False
    return True


def select_strike(snap: Any, direction: str, decision: Any, regime: str = "") -> StrikeSelection:
    """Pick the deepest OTM strike whose tier gates all pass.

    Returns a StrikeSelection. When `strike is None`, the caller MUST treat it as
    a hold/skip (IV too high to trade at all).

    Args:
        snap: SnapshotAccessor — needs atm_strike, iv_percentile, strike_step,
              option_ltp, option_oi, timestamp.
        direction: "CE" or "PE".
        decision: object with ce_prob / pe_prob attributes.
        regime: current market regime string (e.g. "BREAKOUT", "SIDEWAYS").
    """
    atm = getattr(snap, "atm_strike", None)
    confidence = _confidence_for_direction(decision, direction)
    iv_pct = getattr(snap, "iv_percentile", None)

    if os.getenv("STRATEGY_SMART_STRIKE_ENABLED", "").strip() != "1":
        return StrikeSelection(
            strike=int(atm) if atm else None,
            reason="legacy_atm_path",
            confidence=confidence,
            iv_percentile=iv_pct,
            mode="legacy_atm",
            otm_steps=0,
        )

    if atm is None or int(atm) <= 0:
        return StrikeSelection(
            strike=None,
            reason="missing_atm_strike",
            confidence=confidence,
            iv_percentile=iv_pct,
            mode="atm",
            otm_steps=0,
        )

    iv_reject = _env_float("SMART_STRIKE_IV_REJECT_PCTILE", _DEFAULTS["IV_REJECT_PCTILE"])
    if iv_pct is not None and float(iv_pct) > iv_reject:
        return StrikeSelection(
            strike=None,
            reason=f"iv_percentile_above_{iv_reject:.0f}",
            confidence=confidence,
            iv_percentile=float(iv_pct),
            mode="rejected_high_iv",
            otm_steps=0,
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
            otm_steps=0,
        )
    step = int(step)

    ltp_fn = getattr(snap, "option_ltp", None)
    atm_int = int(atm)

    def _otm_strike(n: int) -> int:
        return atm_int + n * step if direction == "CE" else atm_int - n * step

    def _ltp(strike: int) -> Optional[float]:
        if not callable(ltp_fn):
            return None
        v = ltp_fn(direction, strike)
        return float(v) if v is not None and float(v) > 0 else None

    max_premium = _env_float("SMART_STRIKE_MAX_PREMIUM", 0.0)  # 0 = no cap
    # Hard cap (DEFAULT): the premium budget is a real veto. Strike/depth analysis
    # is part of the decision — "no affordable, priced strike" is a legitimate
    # no-trade, exactly like IV-too-high. Only bites when a positive cap is set;
    # with no cap (max_premium=0) nothing changes. Set SMART_STRIKE_HARD_PREMIUM_CAP=0
    # to restore the legacy soft behaviour (trade the best strike even over budget).
    hard_cap = os.getenv("SMART_STRIKE_HARD_PREMIUM_CAP", "1").strip() == "1"

    # Pass 1: try tiers deepest-first with ALL gates including premium target.
    # Deeper OTM = cheaper, so if ltp > max_premium here, shallower will be worse —
    # skip pass 1 entirely for this tier and let pass 2 handle it.
    for tier in _build_otm_tiers():
        strike_candidate = _otm_strike(tier.n)
        ltp = _ltp(strike_candidate)
        if ltp is None:
            continue
        if max_premium > 0 and ltp > max_premium:
            break  # shallower = more expensive — no point continuing pass 1
        if not _tier_passes(tier, confidence, iv_pct, regime, snap, direction, strike_candidate):
            continue
        return StrikeSelection(
            strike=strike_candidate,
            reason=f"otm_{tier.n}_conf_{confidence:.3f}_iv_{iv_pct}_regime_{regime or 'any'}",
            confidence=confidence,
            iv_percentile=iv_pct,
            mode=f"otm_{tier.n}",
            otm_steps=tier.n,
        )

    # Pass 2: no strike fit the premium budget. SOFT cap only — take the best
    # available strike, ignoring the cap. Under a hard cap this pass is skipped:
    # if nothing affordable exists we veto rather than buy an over-budget strike.
    if not (hard_cap and max_premium > 0):
        for tier in _build_otm_tiers():
            strike_candidate = _otm_strike(tier.n)
            ltp = _ltp(strike_candidate)
            if ltp is None:
                continue
            if not _tier_passes(tier, confidence, iv_pct, regime, snap, direction, strike_candidate):
                continue
            return StrikeSelection(
                strike=strike_candidate,
                reason=f"otm_{tier.n}_over_cap_{ltp:.0f}_conf_{confidence:.3f}",
                confidence=confidence,
                iv_percentile=iv_pct,
                mode=f"otm_{tier.n}",
                otm_steps=tier.n,
            )

    # ATM fallback. Under a hard cap, ATM only qualifies if it is itself priced
    # AND within budget — a missing LTP means no depth, an over-budget LTP means
    # unaffordable; either way the strike layer vetoes the trade.
    if hard_cap and max_premium > 0:
        atm_ltp = _ltp(atm_int)
        if atm_ltp is None or atm_ltp > max_premium:
            return StrikeSelection(
                strike=None,
                reason=f"no_affordable_strike_cap_{max_premium:.0f}_atm_ltp_{atm_ltp}",
                confidence=confidence,
                iv_percentile=iv_pct,
                mode="rejected_premium_cap",
                otm_steps=0,
            )

    # ATM fallback — soft path (or hard cap with affordable ATM): return tradeable
    return StrikeSelection(
        strike=atm_int,
        reason=f"atm_fallback_conf_{confidence:.3f}",
        confidence=confidence,
        iv_percentile=iv_pct,
        mode="atm",
        otm_steps=0,
    )
