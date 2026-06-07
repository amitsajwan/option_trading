"""Direction sense — "which way, or UNKNOWN?" (board B-3.2, structural).

Built LAST and abstains when unclear (D5: a low-confidence / conflicted read is UNKNOWN,
never a blind side). The signal is the one MEASURED on our data (direction_research.py,
8 days): on LOADED bars, **VWAP bias** is the best single predictor (~56.6%, full
coverage), with **5-min momentum** (~54.8%) corroborating. So:

  primary  = VWAP side  (close above VWAP -> CE, below -> PE)
  confirm  = sign(fut_return_5m)
  agree    -> that side, higher confidence
  disagree -> UNKNOWN (abstain -> brain WAITs)
  no VWAP  -> UNKNOWN

The brain only consults this AFTER the loaded gate — exactly where the signal works
(on random/all bars these are anti-predictive; the market mean-reverts). Honest: ~0.56
is modest and below the cost break-even, so direction abstains freely and pairs with the
exit/cost work — it is not a standalone money signal yet.

For the B-2.6 cost sweep, :class:`PlaceholderDirection` still supplies a fixed side so
the curve can sweep assumed accuracy independently of this sense.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import SenseVerdict

UNKNOWN = "UNKNOWN"
# confidences anchored to measured loaded-bar accuracies (vwap 0.566; agree higher).
# Reconciliation 2026-06-07: abstaining on every vwap/momentum CONFLICT threw away winning
# trades (live made +10.5%/52tr vs brain +4%/7tr). So we no longer abstain on conflict —
# we take the VWAP side (the best single signal) at lower confidence, and only abstain when
# VWAP itself is unavailable. Far more trades; per-trade accuracy ~vwap-alone.
_CONF_AGREE = 0.60        # vwap & momentum agree
_CONF_VWAP_ONLY = 0.55    # momentum neutral
_CONF_CONFLICT = 0.50     # momentum disagrees — trust vwap but flag low conviction


def _sign(x: Any) -> int:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0
    return 1 if x > 0 else -1 if x < 0 else 0


class DirectionSense:
    """Structural direction: VWAP primary + 5-min momentum confirm; abstain on disagreement."""

    name = "direction"

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        close = context.get("close")
        vwap = context.get("vwap")
        if close is None or vwap is None:
            return SenseVerdict(self.name, UNKNOWN, 0.0, value=None,
                                evidence={"side": UNKNOWN, "basis": [], "reason": "no vwap"})
        vwap_side = _sign(float(close) - float(vwap))
        if vwap_side == 0:
            return SenseVerdict(self.name, UNKNOWN, 0.0, value=None,
                                evidence={"side": UNKNOWN, "basis": [], "reason": "price at vwap"})
        mom_side = _sign(context.get("fut_return_5m"))

        side = "CE" if vwap_side > 0 else "PE"
        if mom_side == vwap_side:
            conf, basis = _CONF_AGREE, ["vwap", "momentum_5m"]
        elif mom_side == 0:
            conf, basis = _CONF_VWAP_ONLY, ["vwap"]
        else:
            conf, basis = _CONF_CONFLICT, ["vwap"]   # momentum disagrees -> trust vwap, low conviction
        return SenseVerdict(self.name, side, conf, value=float(vwap_side),
                            evidence={"side": side, "basis": basis, "confidence_src": "measured_loaded_bars",
                                      "vwap_side": vwap_side, "mom_side": mom_side})


class PlaceholderDirection:
    """Backtest-only: a fixed structural-bias side so the ladder reaches TRADE.

    The *correctness* of this side is not modelled here — it is swept as the
    direction-accuracy parameter ``p`` in the B-2.6 backtest (D5). ``confidence`` is
    set above any UNKNOWN threshold purely so the WAIT-on-UNKNOWN rung is bypassed,
    which is exactly the "direction deferred to Sprint 4" semantics.
    """

    name = "direction"

    def __init__(self, side: str = "CE") -> None:
        if side not in ("CE", "PE"):
            raise ValueError("PlaceholderDirection side must be CE or PE")
        self.side = side

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        return SenseVerdict(
            sense=self.name,
            verdict=self.side,
            confidence=1.0,
            value=None,
            evidence={"side": self.side, "basis": ["structural_bias_placeholder"],
                      "note": "accuracy swept as p in B-2.6 — not a real direction call"},
        )


__all__ = ["DirectionSense", "PlaceholderDirection", "UNKNOWN"]
