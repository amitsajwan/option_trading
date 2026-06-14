"""SellerBrain (T2+T3) — decides WHICH defined-risk credit spread to sell, if any.

Pure decision logic (no orders): regime -> IV gate -> structure -> strikes.
Validated mapping (docs/SELLER_SYSTEM_DESIGN_2026-06-12.md + online evidence):
  TREND_UP   -> BULL PUT spread   (sell ATM put,  buy OTM put)
  TREND_DOWN -> BEAR CALL spread  (sell ATM call, buy OTM call)
  RANGE(MID) -> IRON CONDOR       (both spreads)  — collects ~2x credit, same margin
  CHOP       -> SIT OUT
IV gate: only sell when premium is rich (IV-rank >= IV_RANK_MIN) — else thin credit.
Strikes: ATM short, fixed WIDTH, only strikes present in the chain (liquidity check
is refined later by the StrikeSelector against depth). Width/IV thresholds via env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from ..market.snapshot_accessor import SnapshotAccessor
from ..brain.regime_director import RegimeDirector, regime_quality, CE, PE


# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SpreadLeg:
    action: str        # "SELL" | "BUY"
    option_type: str   # "CE" | "PE"
    strike: int
    role: str          # "short" | "long"


@dataclass(frozen=True)
class SellerDecision:
    structure: str                       # "bull_put" | "bear_call" | "iron_condor" | "none"
    legs: tuple[SpreadLeg, ...] = ()
    direction: Optional[str] = None      # underlying view: "up" | "down" | "neutral"
    regime: str = "?"
    iv_rank: Optional[float] = None
    reason: str = ""

    @property
    def fires(self) -> bool:
        return self.structure != "none" and bool(self.legs)


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name, "") or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


class SellerBrain:
    """Regime-switched defined-risk credit-spread selector."""

    def __init__(self, width: Optional[int] = None, iv_rank_min: Optional[float] = None,
                 direction_signal: Optional[str] = None, condor_offset: Optional[int] = None):
        self._width = int(width if width is not None else _env_float("SELLER_SPREAD_WIDTH", 300))
        self._iv_min = iv_rank_min if iv_rank_min is not None else _env_float("SELLER_IV_RANK_MIN", 30.0)
        # Default 200 = the VALIDATED offset (209-day expiry-aware SIM). NOTE (trader review): even
        # ±200 is only ~0.3σ of a weekly — a delta-based strike (~12-16Δ ≈ ATM±500-700) is the
        # recommended redesign before real money. 200 is the validated-fixed-point default for now.
        self._cond_off = int(condor_offset if condor_offset is not None else _env_float("SELLER_CONDOR_OFFSET", 200))
        # Entry-DTE gate: only sell with theta runway. EXPIRY-AWARE SIM (209 days, weekly options)
        # proved the edge lives ENTIRELY in early-cycle entries — DTE>=4 wins ~74% (+₹1,149/trade),
        # while DTE 0-1 entries are pure gamma with no theta left to harvest (0% win, net loss).
        self._min_dte = int(_env_float("SELLER_MIN_DTE", 4))
        # Directional vertical only on genuine strong trends (default ON); else iron condor.
        self._directional_on_trend = (os.getenv("SELLER_DIRECTIONAL_ON_TREND", "1") or "1").strip() not in ("0", "false", "no")
        self._dir = RegimeDirector(direction_signal or os.getenv("REGIME_DIRECTION_SIGNAL", "weighted"))

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _nearest_strike(snap: SnapshotAccessor, ref: float) -> Optional[int]:
        strikes = snap.available_strikes()
        if not strikes:
            return None
        return min(strikes, key=lambda k: abs(k - ref))

    def _have(self, snap: SnapshotAccessor, strike: int) -> bool:
        return strike in set(snap.available_strikes())

    def _bull_put(self, snap: SnapshotAccessor, atm: int) -> Optional[tuple[SpreadLeg, ...]]:
        long_k = atm - self._width
        if not self._have(snap, long_k):
            return None
        return (SpreadLeg("SELL", "PE", atm, "short"), SpreadLeg("BUY", "PE", long_k, "long"))

    def _bear_call(self, snap: SnapshotAccessor, atm: int) -> Optional[tuple[SpreadLeg, ...]]:
        long_k = atm + self._width
        if not self._have(snap, long_k):
            return None
        return (SpreadLeg("SELL", "CE", atm, "short"), SpreadLeg("BUY", "CE", long_k, "long"))

    def _iron_condor(self, snap: SnapshotAccessor, atm: int) -> Optional[tuple[SpreadLeg, ...]]:
        """OTM-short condor: sell put `offset` below ATM + sell call `offset` above, buy wings."""
        sp, lp = atm - self._cond_off, atm - self._cond_off - self._width
        sc, lc = atm + self._cond_off, atm + self._cond_off + self._width
        if not all(self._have(snap, k) for k in (sp, lp, sc, lc)):
            return None
        return (SpreadLeg("SELL", "PE", sp, "short"), SpreadLeg("BUY", "PE", lp, "long"),
                SpreadLeg("SELL", "CE", sc, "short"), SpreadLeg("BUY", "CE", lc, "long"))

    # ── the decision ─────────────────────────────────────────────────────────
    # VALIDATED (brain_validate SIM, 209 days, positional): the IRON CONDOR sold broadly
    # (IV-gated) is the best + most robust play — the edge is the VRP/theta harvest, not
    # direction. So condor is the DEFAULT; a directional vertical is used only on a genuine
    # strong TREND (to dodge the condor's threatened side). We do NOT sit out chop — the
    # condor harvests theta there too.
    def decide(self, snap: SnapshotAccessor, session_bias=None) -> SellerDecision:
        quality, _ = regime_quality(snap)
        iv_rank = snap.iv_percentile  # 0..100 IV-rank proxy (may be None)
        ref = snap.fut_close
        if ref is None:
            return SellerDecision("none", regime=quality, iv_rank=iv_rank, reason="no fut_close")

        # IV gate — only sell rich premium.
        if iv_rank is not None and iv_rank < self._iv_min:
            return SellerDecision("none", regime=quality, iv_rank=iv_rank,
                                  reason=f"IV-rank {iv_rank:.0f} < {self._iv_min:.0f} (premium too thin)")

        # DTE gate — need theta runway. Near-expiry entries are all gamma, no theta (proven net-loss).
        dte = snap.days_to_expiry
        if dte is not None and dte < self._min_dte:
            return SellerDecision("none", regime=quality, iv_rank=iv_rank,
                                  reason=f"DTE {dte} < {self._min_dte} (no theta runway — gamma trap)")

        atm = self._nearest_strike(snap, float(ref))
        if atm is None:
            return SellerDecision("none", regime=quality, iv_rank=iv_rank, reason="no strikes in chain")

        # genuine strong TREND -> directional vertical (avoid the condor's threatened side)
        if self._directional_on_trend and quality == "TREND":
            verdict = self._dir.decide(snap, session_bias=session_bias)
            if verdict.side == CE:
                legs = self._bull_put(snap, atm)
                if legs:
                    return SellerDecision("bull_put", legs=legs, direction="up", regime=quality,
                                          iv_rank=iv_rank, reason=f"strong TREND up — bull put ({verdict.reason})")
            elif verdict.side == PE:
                legs = self._bear_call(snap, atm)
                if legs:
                    return SellerDecision("bear_call", legs=legs, direction="down", regime=quality,
                                          iv_rank=iv_rank, reason=f"strong TREND down — bear call ({verdict.reason})")

        # DEFAULT: iron condor (the validated theta/VRP engine) — broad, IV-gated
        condor = self._iron_condor(snap, atm)
        if condor:
            return SellerDecision("iron_condor", legs=condor, direction="neutral", regime=quality,
                                  iv_rank=iv_rank, reason=f"iron condor (theta harvest, regime={quality})")
        # condor legs missing -> fall back to a vertical if a side is clear, else sit out
        verdict = self._dir.decide(snap, session_bias=session_bias)
        if verdict.side == CE and self._bull_put(snap, atm):
            return SellerDecision("bull_put", legs=self._bull_put(snap, atm), direction="up",
                                  regime=quality, iv_rank=iv_rank, reason="condor legs missing — bull put fallback")
        if verdict.side == PE and self._bear_call(snap, atm):
            return SellerDecision("bear_call", legs=self._bear_call(snap, atm), direction="down",
                                  regime=quality, iv_rank=iv_rank, reason="condor legs missing — bear call fallback")
        return SellerDecision("none", regime=quality, iv_rank=iv_rank, reason="no tradeable structure in chain")
