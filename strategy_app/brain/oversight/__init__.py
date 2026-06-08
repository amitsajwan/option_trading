"""Oversight brain — the periodic (pre-open + every ~30 min) reasoning layer.

A slow-lane "trader brain" that runs OFF the per-bar hot path. Each cycle it:

  SENSE      gather real market facts (levels, PCR/max-pain/OI walls, VIX, futures)
  MEMORY     load the running day-thesis + recent cycles (scratchpad)
  REASON     LLM forms/updates a posture + directional lean + risk flag
  WRITE      emit ONLY risk-reducing variables the engine reads next 30 min
  LOG        persist reasoning + lean for scoring against outcomes

Safety doctrine (see docs/INTELLIGENT_BRAIN_AGENTIC_ARCHITECTURE.md):
- Never in the per-bar path — output is precomputed into variables the engine reads.
- **Risk-reducing only** until validated: it can say "avoid / stand down / don't take
  CE", never "take a trade". Bounded downside (missed gains), no tail risk.
- Facts-not-memory: reasons over real numbers we compute; marks unknown, never invents.
- Off by default (BRAIN_OVERSIGHT_ENABLED=false); never raises.
"""

from .brain import OversightBrain
from .facts import MarketFacts
from .gate import oversight_entry_veto
from .gemini_web import fetch_web_context
from .reasoner import OversightVerdict
from .scratchpad import Scratchpad
from .verify import verify_verdict

__all__ = [
    "OversightBrain",
    "MarketFacts",
    "OversightVerdict",
    "Scratchpad",
    "oversight_entry_veto",
    "verify_verdict",
    "fetch_web_context",
]
