"""RegimeDirector — STEP 1 of the dual-model entry: decide the SIDE (CE/PE) first.

Your design: confirm the direction/trend up front, THEN call the matching signed
model (CE or PE) to confirm a move of X% is likely that way. This module is step 1.

It is deliberately PLUGGABLE (REGIME_DIRECTION_SIGNAL) because the right direction
signal is regime-dependent and must be chosen by data, not faith:
  * "agreement_lever" (DEFAULT) — momentum_15m + ATM-OI + max_pain all AGREE -> that
    side, else ABSTAIN. The only OOS-validated direction edge (~61% on 2024 big moves,
    project_direction_lever_2026-06-10). Ported from c:/tmp/direction_backtest2.py.
  * "ema_cross"  — ema_9 vs ema_21 (the chart pattern you read visually).
  * "vwap"       — price vs VWAP side (trend-follow).
  * "fade_vwap"  — contrarian to VWAP (the recent 8-day mean-reverting reading, ~57%).
  * "combo"      — require ema_cross AND agreement_lever to agree (highest conf, low cov).

Every detector returns CE / PE / ABSTAIN plus a per-signal breakdown for the trace.
Never raises: missing fields -> ABSTAIN.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from ..market.snapshot_accessor import SnapshotAccessor

logger = logging.getLogger(__name__)

CE, PE, ABSTAIN = "CE", "PE", "ABSTAIN"


@dataclass
class RegimeVerdict:
    side: str                       # "CE" | "PE" | "ABSTAIN"
    confidence: float               # 0..1 (heuristic: 1.0 full agreement, lower if partial)
    signal: str                     # which detector produced it
    breakdown: Dict[str, Optional[str]] = field(default_factory=dict)  # per-sub-signal votes
    reason: str = ""
    quality: str = "MID"            # regime quality: "TREND" | "MID" | "CHOP" (complex mind)
    trend_dir: Optional[str] = None  # multi-timeframe aligned trend side, if any


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def _mtf(s: SnapshotAccessor) -> Dict[str, Any]:
    raw = getattr(s, "raw_payload", None)
    return (raw.get("mtf_derived") or {}) if isinstance(raw, dict) else {}


def regime_quality(s: SnapshotAccessor) -> tuple[str, Optional[str]]:
    """Complex-mind regime classifier (NOT a single EMA): multi-timeframe EMA trend
    alignment + Bollinger band position. Returns (quality, aligned_trend_side).
      TREND = MTF-aligned trend AND price extended to the band edge (late/mature).
      MID   = MTF-aligned trend AND price near the mean (pullback/early — best entry).
      CHOP  = no multi-timeframe trend agreement (stand aside).
    """
    m = _mtf(s)
    aligned = bool(m.get("mtf_aligned"))
    t5 = str(m.get("ema_trend_5m") or "").upper()
    t15 = str(m.get("ema_trend_15m") or "").upper()
    bb = _f(m.get("bb_pct_b_5m"))
    adir: Optional[str] = None
    if aligned and t5 == t15 and t5 in ("BULLISH", "BEARISH"):
        adir = CE if t5 == "BULLISH" else PE
    if adir is None:
        return "CHOP", None
    if bb is None:
        return "MID", adir
    extended = (bb >= 0.75) if adir == CE else (bb <= 0.25)
    return ("TREND" if extended else "MID"), adir


def _sgn_side(value: Optional[float], pos: str = CE, neg: str = PE) -> Optional[str]:
    if value is None or value == 0:
        return None
    return pos if value > 0 else neg


# ── individual sub-signals (each votes CE/PE/None) ────────────────────────────
def _mom15(s: SnapshotAccessor) -> Optional[str]:
    return _sgn_side(s.fut_return_15m)


def _ema(s: SnapshotAccessor) -> Optional[str]:
    e9, e21 = s.ema_9, s.ema_21
    if e9 is None or e21 is None:
        return None
    return _sgn_side(e9 - e21)


def _vwap(s: SnapshotAccessor) -> Optional[str]:
    pv = s.price_vs_vwap
    if pv is None:
        # fall back to spot vs vwap
        if s.fut_close is not None and s.vwap is not None:
            return _sgn_side(s.fut_close - s.vwap)
        return None
    return _sgn_side(pv)


def _atm_oi(s: SnapshotAccessor) -> Optional[str]:
    ce, pe = s.atm_ce_oi_change_30m, s.atm_pe_oi_change_30m
    if ce is None or pe is None:
        # 30m change follows the rolling ATM and is None ~46% of bars (see LIVE_ISSUES
        # D1); fall back to the 1-min change (98% present) so OI still contributes.
        ao = (getattr(s, "raw_payload", None) or {}).get("atm_options") or {}
        ce = ao.get("atm_ce_oi_change_1m") if ce is None else ce
        pe = ao.get("atm_pe_oi_change_1m") if pe is None else pe
    ce, pe = _f(ce), _f(pe)
    if ce is None or pe is None or ce == pe:
        return None
    # more CE-OI written than PE-OI => call writers => bearish => PE.
    return PE if ce > pe else CE


def _max_pain(s: SnapshotAccessor) -> Optional[str]:
    mp, spot = s.max_pain, s.fut_close
    if mp is None or mp == 0 or spot is None:
        return None
    # price tends toward max_pain: below it => expect up => CE.
    return CE if spot < mp else PE


# ── detectors (each returns RegimeVerdict) ────────────────────────────────────
def _detect_agreement_lever(s: SnapshotAccessor) -> RegimeVerdict:
    mom, oi, mp = _mom15(s), _atm_oi(s), _max_pain(s)
    bd = {"mom15": mom, "atm_oi": oi, "max_pain": mp}
    if mom is not None and oi is not None and mp is not None and mom == oi == mp:
        return RegimeVerdict(mom, 1.0, "agreement_lever", bd, "mom15+oi+max_pain all agree")
    return RegimeVerdict(ABSTAIN, 0.0, "agreement_lever", bd, "trio disagrees -> abstain")


def _detect_ema_cross(s: SnapshotAccessor) -> RegimeVerdict:
    side = _ema(s)
    bd = {"ema9_minus_ema21": side}
    if side is None:
        return RegimeVerdict(ABSTAIN, 0.0, "ema_cross", bd, "ema unavailable")
    return RegimeVerdict(side, 0.6, "ema_cross", bd, f"ema9{'>' if side==CE else '<'}ema21")


def _detect_vwap(s: SnapshotAccessor) -> RegimeVerdict:
    side = _vwap(s)
    bd = {"vwap_side": side}
    if side is None:
        return RegimeVerdict(ABSTAIN, 0.0, "vwap", bd, "vwap unavailable")
    return RegimeVerdict(side, 0.55, "vwap", bd, "price vs vwap")


def _detect_fade_vwap(s: SnapshotAccessor) -> RegimeVerdict:
    side = _vwap(s)
    bd = {"vwap_side": side}
    if side is None:
        return RegimeVerdict(ABSTAIN, 0.0, "fade_vwap", bd, "vwap unavailable")
    faded = PE if side == CE else CE
    return RegimeVerdict(faded, 0.57, "fade_vwap", bd, "contrarian to vwap")


def _detect_combo(s: SnapshotAccessor) -> RegimeVerdict:
    lever = _detect_agreement_lever(s)
    ema = _ema(s)
    bd = dict(lever.breakdown)
    bd["ema9_minus_ema21"] = ema
    if lever.side != ABSTAIN and ema is not None and lever.side == ema:
        return RegimeVerdict(lever.side, 1.0, "combo", bd, "agreement_lever AND ema agree")
    return RegimeVerdict(ABSTAIN, 0.0, "combo", bd, "lever/ema disagree -> abstain")


def _wenv(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


# sub-signal -> (function, default weight env var, default weight). A weight of 0
# disables a signal; a NEGATIVE weight FADES it (use the opposite side) — useful since
# e.g. EMA/vwap follow-the-trend was anti-predictive in the recent mean-reverting regime.
_WEIGHTED_SIGNALS = (
    ("mom15", _mom15, "REGIME_W_MOM", 1.0),
    ("max_pain", _max_pain, "REGIME_W_MAXPAIN", 0.8),
    ("atm_oi", _atm_oi, "REGIME_W_OI", 0.8),
    ("vwap", _vwap, "REGIME_W_VWAP", 1.0),
    ("ema", _ema, "REGIME_W_EMA", 0.5),
)


def _detect_weighted(s: SnapshotAccessor) -> RegimeVerdict:
    """Weighted-confidence direction (LIVE_ISSUES L2): instead of requiring ALL signals
    to agree (4% coverage), sum signed weighted votes. A MISSING signal contributes 0
    (graceful — no abstain on missing OI). Fire when |confidence| >= REGIME_CONF_THRESHOLD.
    Confidence = |score| / sum(|weight of present signals|), so it's the *net* lean."""
    tau = _wenv("REGIME_CONF_THRESHOLD", 0.34)
    score = 0.0
    wsum = 0.0
    bd: Dict[str, Optional[str]] = {}
    for name, fn, env, default in _WEIGHTED_SIGNALS:
        w = _wenv(env, default)
        side = fn(s)
        bd[name] = side
        if side in (CE, PE) and w != 0.0:
            vote = 1.0 if side == CE else -1.0
            score += w * vote          # negative w fades the signal
            wsum += abs(w)
    if wsum == 0.0:
        return RegimeVerdict(ABSTAIN, 0.0, "weighted", bd, "no signals present")
    conf = abs(score) / wsum
    bd["score"] = round(score, 2)            # type: ignore[assignment]
    bd["conf"] = round(conf, 2)              # type: ignore[assignment]
    if score == 0.0 or conf < tau:
        return RegimeVerdict(ABSTAIN, conf, "weighted", bd, f"conf {conf:.2f} < {tau:.2f} -> abstain")
    side = CE if score > 0 else PE
    return RegimeVerdict(side, conf, "weighted", bd, f"weighted {side} conf={conf:.2f} (>= {tau:.2f})")


def _detect_mtf_trend(s: SnapshotAccessor) -> RegimeVerdict:
    """Complex-mind direction: trade WITH the multi-timeframe aligned trend."""
    q, adir = regime_quality(s)
    bd = {"quality": q, "mtf_trend": adir}
    if adir is None:
        return RegimeVerdict(ABSTAIN, 0.0, "mtf_trend", bd, "no multi-timeframe trend")
    return RegimeVerdict(adir, 0.7, "mtf_trend", bd, f"mtf-aligned {q} {adir}")


_DETECTORS: Dict[str, Callable[[SnapshotAccessor], RegimeVerdict]] = {
    "agreement_lever": _detect_agreement_lever,
    "ema_cross": _detect_ema_cross,
    "vwap": _detect_vwap,
    "fade_vwap": _detect_fade_vwap,
    "combo": _detect_combo,
    "weighted": _detect_weighted,
    "mtf_trend": _detect_mtf_trend,
}

DEFAULT_SIGNAL = "agreement_lever"


class RegimeDirector:
    """Step-1 direction call. Detector chosen by REGIME_DIRECTION_SIGNAL env. Every
    verdict carries the complex-mind regime `quality` (TREND/MID/CHOP) so the engine
    can gate to MID+TREND and skip CHOP."""

    def __init__(self, signal: Optional[str] = None) -> None:
        self.signal = (signal or os.getenv("REGIME_DIRECTION_SIGNAL", DEFAULT_SIGNAL)).strip().lower()
        if self.signal not in _DETECTORS:
            logger.warning("regime_director: unknown signal %r, using %s", self.signal, DEFAULT_SIGNAL)
            self.signal = DEFAULT_SIGNAL

    def decide(self, snap: SnapshotAccessor, session_bias: Any = None) -> RegimeVerdict:
        try:
            verdict = _DETECTORS[self.signal](snap)
        except Exception:
            logger.debug("regime_director: detector %s failed", self.signal, exc_info=True)
            verdict = RegimeVerdict(ABSTAIN, 0.0, self.signal, {}, "detector error -> abstain")
        try:
            quality, trend_dir = regime_quality(snap)
            verdict.quality = quality
            verdict.trend_dir = trend_dir
        except Exception:
            pass
        if session_bias is not None:
            _apply_llm_overlay(verdict, session_bias)
        return verdict


def _apply_llm_overlay(v: RegimeVerdict, bias: Any,
                       min_conviction: float = None) -> None:
    """Fold the Gemini session bias (live news) into the verdict:
      - a grounded, high-conviction LLM bias that CONTRADICTS the structural side
        VETOES it (-> ABSTAIN: don't trade into the news),
      - one that AGREES boosts confidence,
      - and either way the LLM's news becomes the decision REASON.
    The LLM never creates a side on its own (it confirms/vetoes structure, not leads).
    Controlled by REGIME_LLM_MIN_CONVICTION (default 0.55); never raises.
    """
    if min_conviction is None:
        try:
            min_conviction = float(os.getenv("REGIME_LLM_MIN_CONVICTION", "0.55") or 0.55)
        except ValueError:
            min_conviction = 0.55
    try:
        grounded = bool(getattr(bias, "grounded", False))
        bside = getattr(bias, "side", None)            # CE / PE / None
        conv = float(getattr(bias, "conviction", 0.0) or 0.0)
        news = str(getattr(bias, "news_summary", "") or "")
    except Exception:
        return
    v.breakdown["llm_bias"] = bside
    v.breakdown["llm_conviction"] = round(conv, 2)
    v.breakdown["llm_grounded"] = grounded
    if (not grounded) or conv < min_conviction or bside not in (CE, PE):
        return  # no strong grounded view -> structure stands as-is
    if v.side in (CE, PE):
        if bside == v.side:
            v.confidence = min(1.0, v.confidence + 0.15)
            v.reason = f"{v.reason}; LLM AGREES {bside} ({conv:.2f}): {news[:140]}"
        else:
            v.reason = f"LLM VETO — structure={v.side} vs grounded news={bside} ({conv:.2f}): {news[:140]}"
            v.side = ABSTAIN
            v.confidence = 0.0
