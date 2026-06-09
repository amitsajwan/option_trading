"""VERIFY — catch LLM hallucinations by cross-checking the verdict vs ground truth.

The deterministic :class:`MarketFacts` are the truth. If the LLM's verdict
contradicts them — a bullish lean on a clearly-falling tape, a posture that
disagrees with the move, key levels that don't exist near the price, or high
confidence on a flat tape — we **downgrade** the verdict (toward neutral) and
flag it. A risk-reducing layer must never act on a hallucinated read.

Returns ``(verified_verdict, flags)``. Flags are logged + stored for scoring.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .reasoner import OversightVerdict

# A move beyond this (vs prev close) is "clearly directional" — a lean the other
# way is a contradiction we don't trust.
_STRONG_MOVE = 0.004
_FLAT = 0.001          # |move| under this = flat tape; cap confidence
_FLAT_CONF_CAP = 0.5


def verify_verdict(verdict: OversightVerdict, facts: dict[str, Any]) -> tuple[OversightVerdict, list[str]]:
    flags: list[str] = []
    chg = facts.get("fut_vs_prev_close_pct")
    px = facts.get("fut_price")

    lean = verdict.direction_lean
    conf = float(verdict.lean_confidence)
    posture = verdict.posture

    # 1. Lean / posture must not contradict a strong, unambiguous move.
    if isinstance(chg, (int, float)):
        if lean == "CE" and chg <= -_STRONG_MOVE:
            flags.append("lean_CE_contradicts_downmove"); lean, conf = "none", 0.0
        elif lean == "PE" and chg >= _STRONG_MOVE:
            flags.append("lean_PE_contradicts_upmove"); lean, conf = "none", 0.0
        if posture == "trend_up" and chg <= -_STRONG_MOVE:
            flags.append("posture_up_contradicts_move"); posture = "choppy"
        elif posture == "trend_down" and chg >= _STRONG_MOVE:
            flags.append("posture_down_contradicts_move"); posture = "choppy"
        # overconfidence on a flat tape
        if abs(chg) < _FLAT and conf > _FLAT_CONF_CAP:
            flags.append("overconfident_on_flat_tape"); conf = _FLAT_CONF_CAP

    # 2. key_levels sanity — drop any "level" not in a plausible band around price.
    levels = verdict.key_levels
    if isinstance(px, (int, float)) and px > 0 and levels:
        lo_cands = [v for v in (facts.get("week_low"), px * 0.95) if isinstance(v, (int, float))]
        hi_cands = [v for v in (facts.get("week_high"), px * 1.05) if isinstance(v, (int, float))]
        lo, hi = (min(lo_cands) if lo_cands else px * 0.9), (max(hi_cands) if hi_cands else px * 1.1)
        kept = tuple(l for l in levels if lo <= l <= hi)
        if len(kept) != len(levels):
            flags.append("dropped_implausible_levels")
        levels = kept

    if lean == "none":
        conf = 0.0

    verified = replace(
        verdict, direction_lean=lean, lean_confidence=conf, posture=posture, key_levels=levels
    )
    return verified, flags


__all__ = ["verify_verdict"]
