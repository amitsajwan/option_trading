"""Opportunity gate — score / rank / select (replaces the ATR elimination gate).

Pure, causal, session-relative. NOT wired into the engine yet — this is the
stand-alone scorer so we can validate selection on real days in the sim before
any live wiring. See docs/strategy_platform/OPPORTUNITY_GATE_DESIGN.md.

Causal by construction: every percentile is computed over the bars seen *so far*
this session (expanding window), never the full day — so a backtest cannot cheat.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class OpportunityConfig:
    enabled: bool = True
    warmup_bars: int = 15
    # component weights (should sum ~1.0); normalised defensively at use.
    # w_entry_prob is the trained move model (our best ranker, AUC ~0.78). When
    # >0 it dominates; the structural components remain as de-correlated backups.
    w_entry_prob: float = 0.60
    w_atr_pct: float = 0.20
    w_atr_accel: float = 0.10
    w_volume_pct: float = 0.05
    w_straddle_expansion: float = 0.05
    w_regime_quality: float = 0.00
    accel_lookback: int = 5          # bars back for ATR/straddle acceleration
    # selection:
    #   "percentile"  → enter if score in top (100 - selection_percentile)% of
    #                   session-so-far (relative-to-today; simple but biased early,
    #                   because a thin morning distribution makes early bars rank high).
    #   "score_cutoff"→ enter if score >= score_cutoff, where component percentiles
    #                   are ranked against a multi-day baseline (stable from bar 1).
    #                   Preferred — fixes the early-session selection bias.
    selection_mode: str = "percentile"
    selection_percentile: float = 80.0
    score_cutoff: float = 65.0
    # cost floor (economic, not statistical): the horizon-matched expected move
    # (atr_14_1m * sqrt(hold_bars)) must exceed the points needed to cover round-
    # trip cost. ~108pt ≈ the 0.20% move the entry model targets (cost coverage).
    # NOTE: the ATM straddle price is expected move to EXPIRY (days), NOT over our
    # ~10-min hold — so it is used as a SCORE component (expansion), never as the
    # cost-floor estimate. Horizon-matched ATR projection is the right estimator.
    min_expected_move_pts: float = 108.0
    hold_bars: int = 10              # hold horizon (bars) for the expected-move projection
    # daily budget
    max_entries_per_day: int = 3
    min_spacing_minutes: int = 20

    @classmethod
    def from_env(cls) -> "OpportunityConfig":
        def _i(k: str, d: int) -> int:
            try: return int(os.getenv(k, "") or d)
            except ValueError: return d
        def _f(k: str, d: float) -> float:
            try: return float(os.getenv(k, "") or d)
            except ValueError: return d
        def _s(k: str, d: str) -> str:
            return (os.getenv(k, "") or d).strip()
        return cls(
            selection_mode=_s("OPP_GATE_SELECTION_MODE", "percentile"),
            selection_percentile=_f("OPP_GATE_SELECTION_PERCENTILE", 80.0),
            score_cutoff=_f("OPP_GATE_SCORE_CUTOFF", 65.0),
            max_entries_per_day=_i("OPP_GATE_MAX_ENTRIES", 3),
            min_spacing_minutes=_i("OPP_GATE_MIN_SPACING_MINUTES", 20),
            warmup_bars=_i("OPP_GATE_WARMUP_BARS", 15),
            min_expected_move_pts=_f("OPP_GATE_MIN_MOVE_PTS", 108.0),
            hold_bars=_i("OPP_GATE_HOLD_BARS", 10),
        )


# ---------------------------------------------------------------------------
# Per-bar inputs / decision
# ---------------------------------------------------------------------------
@dataclass
class BarInputs:
    ts: datetime
    spot: float
    prob: Optional[float] = None            # entry model P(move) for this bar (primary signal)
    atr_ratio: Optional[float] = None       # atr_14_1m / price
    volume: Optional[float] = None
    straddle_premium: Optional[float] = None  # ATM CE+PE premium (expected-move proxy)
    regime_quality: Optional[float] = None    # 0..1 (RegimeDirector quality)


@dataclass
class OpportunityDecision:
    enter: bool
    score: float                 # 0..100 (0 during warmup)
    reason: str
    components: dict = field(default_factory=dict)
    expected_move_pts: Optional[float] = None
    cost_floor_pts: Optional[float] = None
    score_rank_pct: Optional[float] = None  # this bar's rank within session-so-far


def _pct_rank(history: list[float], value: float) -> float:
    """Causal percentile rank (0..100) of *value* among history + itself."""
    pool = history + [value]
    n = len(pool)
    if n <= 1:
        return 50.0
    le = sum(1 for x in pool if x <= value)
    return 100.0 * le / n


# ---------------------------------------------------------------------------
# Session state machine
# ---------------------------------------------------------------------------
class OpportunitySession:
    """Feed bars in order; get an ENTER/SKIP decision per bar.

    Holds only causal history (values seen so far today) + budget state.
    """

    def __init__(self, cfg: Optional[OpportunityConfig] = None,
                 baseline: Optional[dict] = None) -> None:
        """``baseline`` (optional): trailing prior-session values to seed the
        component histories so percentiles are stable from the first bar.
        Keys: "atr", "volume", "accel", "strd_exp" → list[float]. Used with
        ``selection_mode="score_cutoff"``.
        """
        self.cfg = cfg or OpportunityConfig()
        b = baseline or {}
        # baseline seeds rank stability but is NOT counted as session history for
        # acceleration/expansion deltas — kept in separate prefix lists.
        self._prob: list[float] = list(b.get("prob", []))
        self._atr: list[float] = list(b.get("atr", []))
        self._vol: list[float] = list(b.get("volume", []))
        self._accel: list[float] = list(b.get("accel", []))
        self._strd_exp: list[float] = list(b.get("strd_exp", []))
        self._straddle: list[float] = []
        # today-only sequences (for acceleration/expansion deltas)
        self._atr_today: list[float] = []
        self._straddle_today: list[float] = []
        self._scores: list[float] = []   # today-only (drives warmup + percentile mode)
        self._entries: int = 0
        self._last_entry_ts: Optional[datetime] = None

    # -- helpers ----------------------------------------------------------
    def _weights(self) -> dict[str, float]:
        c = self.cfg
        raw = {
            "entry_prob": c.w_entry_prob,
            "atr_percentile": c.w_atr_pct,
            "atr_acceleration": c.w_atr_accel,
            "volume_percentile": c.w_volume_pct,
            "straddle_expansion": c.w_straddle_expansion,
            "regime_quality": c.w_regime_quality,
        }
        total = sum(v for v in raw.values() if v > 0) or 1.0
        return {k: (v / total if v > 0 else 0.0) for k, v in raw.items()}

    def _expected_move_pts(self, bar: BarInputs) -> Optional[float]:
        """Horizon-matched expected move over the hold: atr_14_1m * sqrt(hold_bars).

        (atr_14_1m = atr_ratio * spot). This matches our ~10-bar hold; the ATM
        straddle would be expected move to expiry (days) — wrong horizon.
        """
        c = self.cfg
        if bar.atr_ratio and bar.spot:
            atr_pts = bar.atr_ratio * bar.spot
            return atr_pts * (c.hold_bars ** 0.5)
        return None

    # -- main -------------------------------------------------------------
    def observe(self, bar: BarInputs) -> OpportunityDecision:
        c = self.cfg

        # Deltas (acceleration/expansion) use TODAY's sequence only — never the
        # baseline (comparing today's bar to a prior day's level is meaningless).
        accel = None
        if bar.atr_ratio is not None and len(self._atr_today) >= c.accel_lookback:
            accel = bar.atr_ratio - self._atr_today[-c.accel_lookback]
        strd_exp = None
        if bar.straddle_premium and len(self._straddle_today) >= c.accel_lookback:
            prev = self._straddle_today[-c.accel_lookback]
            if prev:
                strd_exp = (bar.straddle_premium - prev) / prev

        # component percentile ranks (causal)
        comp: dict[str, float] = {}
        comp["entry_prob"] = _pct_rank(self._prob, bar.prob) if bar.prob is not None else 0.0
        comp["atr_percentile"] = _pct_rank(self._atr, bar.atr_ratio) if bar.atr_ratio is not None else 0.0
        comp["volume_percentile"] = _pct_rank(self._vol, bar.volume) if bar.volume is not None else 0.0
        comp["atr_acceleration"] = _pct_rank(self._accel, accel) if accel is not None else 0.0
        comp["straddle_expansion"] = _pct_rank(self._strd_exp, strd_exp) if strd_exp is not None else 0.0
        comp["regime_quality"] = (bar.regime_quality or 0.0) * 100.0

        w = self._weights()
        score = sum(w[k] * comp[k] for k in w)

        # update ranking pools (baseline + today) AND today-only delta sequences
        if bar.prob is not None:
            self._prob.append(bar.prob)
        if bar.atr_ratio is not None:
            self._atr.append(bar.atr_ratio)
            self._atr_today.append(bar.atr_ratio)
        if bar.volume is not None:
            self._vol.append(bar.volume)
        if accel is not None:
            self._accel.append(accel)
        if strd_exp is not None:
            self._strd_exp.append(strd_exp)
        if bar.straddle_premium:
            self._straddle.append(bar.straddle_premium)
            self._straddle_today.append(bar.straddle_premium)

        # rank this score within session-so-far, then record it
        score_rank = _pct_rank(self._scores, score)
        self._scores.append(score)

        exp_move = self._expected_move_pts(bar)
        cost_floor = c.min_expected_move_pts

        def decision(enter: bool, reason: str) -> OpportunityDecision:
            return OpportunityDecision(
                enter=enter, score=round(score, 2), reason=reason, components=comp,
                expected_move_pts=round(exp_move, 1) if exp_move is not None else None,
                cost_floor_pts=round(cost_floor, 1) if cost_floor is not None else None,
                score_rank_pct=round(score_rank, 1),
            )

        if not c.enabled:
            return decision(False, "disabled")
        if len(self._scores) <= c.warmup_bars:
            return decision(False, "warmup")
        # selection
        if c.selection_mode == "score_cutoff":
            # absolute cutoff on a baseline-stabilised score (no early-session bias)
            if score < c.score_cutoff:
                return decision(False, "below_score_cutoff")
        else:
            # relative — top (100 - P)% of the day so far
            if score_rank < c.selection_percentile:
                return decision(False, "below_selection_percentile")
        # cost floor: economic viability
        if exp_move is None or cost_floor is None or exp_move < cost_floor:
            return decision(False, "below_cost_floor")
        # budget
        if self._entries >= c.max_entries_per_day:
            return decision(False, "daily_budget_exhausted")
        if self._last_entry_ts is not None:
            gap_min = (bar.ts - self._last_entry_ts).total_seconds() / 60.0
            if gap_min < c.min_spacing_minutes:
                return decision(False, "min_spacing")

        # ENTER
        self._entries += 1
        self._last_entry_ts = bar.ts
        return decision(True, "selected")
