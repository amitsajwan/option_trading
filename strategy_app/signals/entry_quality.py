"""Entry-quality grading: GOOD / OK / BAD for each candidate entry.

Purpose
-------
On paper we keep taking many trades so we can learn. But not every trade is one
we'd risk real money on. This module grades an entry's *direction quality* at
entry time so the RiskManager can route it:

    GOOD -> live-eligible (the subset we'd actually fire on real money)
    OK   -> paper-only, kept for analysis ("acceptable but not conviction")
    BAD  -> paper-only, flagged ("clearly wrong / conflicted entry")

It is purely observational — it NEVER blocks a trade. Paper takes everything;
the grade only decides the *tier* (see RiskManager.live_eligible).

Root cause it guards against (2026-06-03 trade fce59da2)
-------------------------------------------------------
That CE was chosen on a SIDEWAYS day with a 4.03 direction margin that was
manufactured by 4 correlated depth ticks (now de-correlated in
entry_direction_resolver). The grader penalises exactly that failure mode:
  * effective margin too thin
  * depth dominating the score (correlation risk)
  * SIDEWAYS / chop regime (noisy direction)
  * momentum not fresh (1m disagrees with 5m — the move was already reversing)
  * iv_skew disagreeing with the chosen side (the options market leaned the
    other way — it did, correctly, on fce59da2)

All thresholds are env-driven (ENTRY_QUALITY_*) so this is tunable without code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from ..contracts import Direction
from ..market.snapshot_accessor import SnapshotAccessor
from ..ml.entry_direction_resolver import EntryDirectionResult

GOOD = "GOOD"
OK = "OK"
BAD = "BAD"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_set(name: str, default: set[str]) -> set[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return set(default)
    return {p.strip().upper() for p in raw.split(",") if p.strip()}


@dataclass
class EntryQualityResult:
    grade: str
    score: float
    reasons: list[str] = field(default_factory=list)
    penalties: dict[str, float] = field(default_factory=dict)

    def as_raw_signals(self) -> dict[str, Any]:
        return {
            "entry_grade": self.grade,
            "entry_grade_score": round(self.score, 4),
            "entry_grade_reasons": list(self.reasons),
            "entry_grade_penalties": dict(self.penalties),
        }


def _depth_share(dir_result: EntryDirectionResult) -> float:
    """Fraction of the winning side's score that came from depth.

    With de-correlation depth contributes a single 'depth_net' source; without
    it, several 'depth_*' tags. Either way we sum the depth-attributed weight and
    compare to the total winning-side score.
    """
    winner_total = max(dir_result.ce_score, dir_result.pe_score)
    if winner_total <= 0:
        return 0.0
    depth_weight = 0.0
    src = dir_result.sources or {}
    if "depth_net" in src:
        depth_weight = abs(float(src.get("depth_net") or 0.0))
    else:
        for k, v in src.items():
            if str(k).startswith("depth_"):
                depth_weight += abs(float(v or 0.0))
    return min(1.0, depth_weight / winner_total)


def grade_entry_from_raw(
    raw_signals: dict[str, Any],
    snap: SnapshotAccessor,
    *,
    direction: Optional[Direction],
    regime: Optional[str] = None,
) -> Optional[EntryQualityResult]:
    """Grade an entry from a vote's raw_signals dict (post-resolution).

    Used by the engine, which holds the vote (with entry_dir_* fields) and the
    regime, but not the original EntryDirectionResult object. Returns None when
    the direction scores are absent (e.g. ce_only / pe_only / consensus modes
    that don't run the composite resolver).
    """
    if direction is None:
        return None
    # Composite direction mode stores entry_dir_*; consensus mode stores
    # direction_consensus_*. Grade either — fall back to consensus when the
    # composite fields are absent (e.g. the ops-sim replay default profile).
    has_composite = "entry_dir_ce_score" in raw_signals or "entry_dir_margin" in raw_signals
    has_consensus = "direction_consensus_ce" in raw_signals or "direction_consensus_margin" in raw_signals
    if not has_composite and not has_consensus:
        return None
    if has_composite:
        dir_result = EntryDirectionResult(
            direction=direction,
            source=str(raw_signals.get("direction_source") or "composite"),
            ce_score=float(raw_signals.get("entry_dir_ce_score") or 0.0),
            pe_score=float(raw_signals.get("entry_dir_pe_score") or 0.0),
            margin=float(raw_signals.get("entry_dir_margin") or 0.0),
            vetoed=bool(raw_signals.get("entry_dir_vetoed") or False),
            veto_reason=str(raw_signals.get("entry_dir_veto_reason") or ""),
            sources=dict(raw_signals.get("entry_dir_sources") or {}),
        )
    else:
        dir_result = EntryDirectionResult(
            direction=direction,
            source=str(raw_signals.get("direction_source") or "direction_consensus"),
            ce_score=float(raw_signals.get("direction_consensus_ce") or 0.0),
            pe_score=float(raw_signals.get("direction_consensus_pe") or 0.0),
            margin=float(raw_signals.get("direction_consensus_margin") or 0.0),
            vetoed=False,
            veto_reason="",
            sources=dict(raw_signals.get("direction_consensus_sources") or {}),
        )
    return grade_entry(dir_result, snap, regime=regime)


def grade_entry(
    dir_result: EntryDirectionResult,
    snap: SnapshotAccessor,
    *,
    regime: Optional[str] = None,
) -> EntryQualityResult:
    """Grade an entry GOOD/OK/BAD from its direction result + snapshot context.

    Scoring starts at 1.0 (clean) and subtracts penalties; the final score maps
    to a grade via two thresholds. A hard veto (e.g. the resolver itself vetoed)
    short-circuits to BAD.
    """
    reasons: list[str] = []
    penalties: dict[str, float] = {}
    score = 1.0

    if dir_result.vetoed or dir_result.direction is None:
        return EntryQualityResult(
            grade=BAD, score=0.0,
            reasons=[f"direction_vetoed:{dir_result.veto_reason or 'no_direction'}"],
            penalties={"vetoed": 1.0},
        )

    direction = dir_result.direction
    regime_name = str(regime or "").strip().upper()

    # ── 1) Effective margin ─────────────────────────────────────────────────
    # Margin is now the de-correlated margin (depth capped to one vote). A thin
    # margin means the sources barely agreed.
    good_margin = _env_float("ENTRY_QUALITY_MARGIN_GOOD", 2.0)
    weak_margin = _env_float("ENTRY_QUALITY_MARGIN_WEAK", 1.0)
    margin = float(dir_result.margin)
    if margin < weak_margin:
        p = _env_float("ENTRY_QUALITY_PEN_MARGIN_THIN", 0.5)
        score -= p
        penalties["margin_thin"] = p
        reasons.append(f"margin_thin={margin:.2f}<{weak_margin:g}")
    elif margin < good_margin:
        p = _env_float("ENTRY_QUALITY_PEN_MARGIN_MILD", 0.2)
        score -= p
        penalties["margin_mild"] = p
        reasons.append(f"margin_mild={margin:.2f}<{good_margin:g}")

    # ── 2) Depth dominance (correlation risk) ───────────────────────────────
    depth_share = _depth_share(dir_result)
    max_depth_share = _env_float("ENTRY_QUALITY_MAX_DEPTH_SHARE", 0.6)
    if depth_share > max_depth_share:
        p = _env_float("ENTRY_QUALITY_PEN_DEPTH_DOM", 0.3)
        score -= p
        penalties["depth_dominant"] = p
        reasons.append(f"depth_dominant={depth_share:.0%}>{max_depth_share:.0%}")

    # ── 3) Regime (SIDEWAYS / chop = noisy direction) ───────────────────────
    chop_regimes = _env_set("ENTRY_QUALITY_CHOP_REGIMES", {"SIDEWAYS", "CHOP", "UNKNOWN"})
    if regime_name in chop_regimes:
        p = _env_float("ENTRY_QUALITY_PEN_CHOP_REGIME", 0.3)
        score -= p
        penalties["chop_regime"] = p
        reasons.append(f"chop_regime={regime_name}")

    # ── 4) Momentum freshness (1m must agree with 5m) ───────────────────────
    r1 = snap.fut_return_1m
    r5 = snap.fut_return_5m
    if r1 is not None and r5 is not None and float(r5) != 0.0 and float(r1) != 0.0:
        if (float(r1) > 0) != (float(r5) > 0):
            p = _env_float("ENTRY_QUALITY_PEN_STALE_MOMENTUM", 0.3)
            score -= p
            penalties["stale_momentum"] = p
            reasons.append(f"stale_momentum r1m={float(r1):+.4f} vs r5m={float(r5):+.4f}")

    # ── 5) IV skew disagreement (options market leaned the other way) ───────
    ce_iv = snap.atm_ce_iv
    pe_iv = snap.atm_pe_iv
    if ce_iv and pe_iv and float(ce_iv) > 0 and float(pe_iv) > 0:
        ratio = float(pe_iv) / float(ce_iv)
        skew_band = _env_float("ENTRY_QUALITY_IV_SKEW_BAND", 0.08)
        iv_side: Optional[Direction] = None
        if ratio > 1.0 + skew_band:
            iv_side = Direction.PE  # puts richer -> downside lean
        elif ratio < 1.0 - skew_band:
            iv_side = Direction.CE
        if iv_side is not None and iv_side != direction:
            p = _env_float("ENTRY_QUALITY_PEN_IV_DISAGREE", 0.2)
            score -= p
            penalties["iv_disagree"] = p
            reasons.append(f"iv_skew_disagree ratio={ratio:.3f} iv_side={iv_side.value}")

    # ── Grade mapping ───────────────────────────────────────────────────────
    good_at = _env_float("ENTRY_QUALITY_GOOD_AT", 0.8)
    bad_below = _env_float("ENTRY_QUALITY_BAD_BELOW", 0.5)
    score = max(0.0, score)
    if score >= good_at:
        grade = GOOD
    elif score < bad_below:
        grade = BAD
    else:
        grade = OK
    if not reasons:
        reasons.append("clean")
    return EntryQualityResult(grade=grade, score=score, reasons=reasons, penalties=penalties)


@dataclass
class TierDecision:
    tier: str            # "live" | "paper"
    live_would_take: bool
    reason: str

    def as_raw_signals(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "live_would_take": self.live_would_take,
            "tier_reason": self.reason,
        }


def decide_tier(grade: str, risk: Any, *, confidence: float = 1.0) -> TierDecision:
    """Decide LIVE vs PAPER from entry grade + recent (session) performance.

    Operates on a RiskContext-like object (the snapshot of session risk state that
    is already threaded into strategy evaluation), so it needs no coupling to the
    RiskManager. Paper takes everything; this only labels the tier. The set of
    tier=live trades is the book we'd fire on real money.

    Returns a TierDecision; the caller does NOT block on it in paper mode.
    """
    grade_u = str(grade or "").strip().upper()
    order = {"BAD": 0, "OK": 1, "GOOD": 2}
    min_grade = str(os.getenv("RISK_LIVE_MIN_GRADE", "GOOD") or "GOOD").strip().upper()
    min_rank = order.get(min_grade, 2)

    def paper(reason: str) -> TierDecision:
        return TierDecision(tier="paper", live_would_take=False, reason=reason)

    if order.get(grade_u, -1) < min_rank:
        return paper(f"grade_below_min:{grade_u}<{min_grade}")

    # Defensive session states.
    if getattr(risk, "daily_loss_breached", False):
        return paper("daily_loss_breached")
    if getattr(risk, "weekly_loss_breached", False):
        return paper("weekly_loss_breached")
    if getattr(risk, "session_trade_cap_breached", False):
        return paper("session_trade_cap")
    if getattr(risk, "vix_spike_halt", False):
        return paper("vix_spike_halt")
    if getattr(risk, "consecutive_loss_limit", False):
        return paper("consecutive_loss_pause")

    # Consecutive-loss caution: back off live one loss before the hard limit.
    max_consec = int(getattr(risk, "max_consecutive_losses", 0) or 0)
    consec = int(getattr(risk, "consecutive_losses", 0) or 0)
    if max_consec > 0:
        caution_at = max(1, max_consec - 1)
        if consec >= caution_at:
            return paper(f"consec_loss_caution:{consec}>={caution_at}")

    # Session win-rate floor (only after a minimum sample).
    min_trades = int(os.getenv("RISK_LIVE_WINRATE_MIN_TRADES", "5") or "5")
    min_winrate = float(os.getenv("RISK_LIVE_MIN_WINRATE", "0.0") or "0.0")
    wins = int(getattr(risk, "session_win_count", 0) or 0)
    losses = int(getattr(risk, "session_loss_count", 0) or 0)
    decided = wins + losses
    if min_winrate > 0.0 and decided >= min_trades:
        winrate = wins / decided if decided > 0 else 0.0
        if winrate < min_winrate:
            return paper(f"session_winrate_low:{winrate:.2f}<{min_winrate:.2f}")

    return TierDecision(tier="live", live_would_take=True, reason=f"eligible:grade={grade_u}")


__all__ = [
    "grade_entry", "EntryQualityResult", "decide_tier", "TierDecision",
    "GOOD", "OK", "BAD",
]
