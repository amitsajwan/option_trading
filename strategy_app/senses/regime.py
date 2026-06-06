"""IntradayRegime sense — "is the market alive right now?" (board B-1.3).

The plan calls to wrap ``market/regime.RegimeClassifier``. That classifier needs the
engine's snapshot accessor, so for the self-contained e2e gate this sense derives
the same alive/dead/expanding/compressed read from the build-vs-baseline ATR already
in the context (the cheaper, more robust path — the handover lists IntradayRegime as
"to build (gate)"). Swap in the full classifier as an overlay once it is wired.

States: ``alive`` (normal vol), ``expanding`` (vol breaking out), ``compressed``
(coiled — a loaded spring lives here), ``dead`` (no vol — lunch/holiday),
``chaotic`` (vol far above baseline — untradeably wild).
"""
from __future__ import annotations

from typing import Any, Mapping

from . import SenseVerdict
from .context import compression_ratio

DEAD_RATIO = 0.35        # atr_build/atr_base below this => dead
COMPRESSED_RATIO = 0.70  # below this (but above dead) => compressed/coiled
EXPANDING_RATIO = 1.5    # above this => expanding
CHAOTIC_RATIO = 3.0      # far above => chaotic / untradeable


class RegimeSense:
    name = "regime"

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        ratio = compression_ratio(context)
        if ratio is None:
            return SenseVerdict.abstain(self.name, reason="no compression input")

        if ratio < DEAD_RATIO:
            state, conf = "dead", 0.7
        elif ratio < COMPRESSED_RATIO:
            state, conf = "compressed", 0.6
        elif ratio < EXPANDING_RATIO:
            state, conf = "alive", 0.6
        elif ratio < CHAOTIC_RATIO:
            state, conf = "expanding", 0.65
        else:
            state, conf = "chaotic", 0.7

        return SenseVerdict(
            sense=self.name,
            verdict=state,
            confidence=conf,
            value=round(ratio, 3),
            evidence={"atr_ratio": round(ratio, 3), "reason": f"atr_build/atr_base={ratio:.2f}"},
        )


__all__ = ["RegimeSense"]
