"""REASONER — the LLM oversight call. Facts + memory in, structured verdict out.

The verdict is **risk-reducing only**: a posture, a directional *lean*, and a
risk flag. `to_risk_state()` maps it to the handful of variables the engine reads
next cycle — and by construction those can only make the engine *more* selective
(stand down, reduce, or veto the side the brain thinks is wrong). It can never
force a trade. Trade-forcing waits until the lean is validated against outcomes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from ..providers.openai_compatible import (
    LLMClientError,
    chat_completion,
    extract_json_object,
)

logger = logging.getLogger(__name__)

_VALID_POSTURE = {"trend_up", "trend_down", "choppy", "stand_down", "unknown"}
_VALID_LEAN = {"CE", "PE", "none"}
_VALID_RISK = {"normal", "reduce", "stand_down"}
_VETO_MIN_CONF = 0.60   # a lean only vetoes the opposite side above this confidence

_SYSTEM_PROMPT = (
    "You are the oversight brain for a BankNifty intraday options strategy. You run "
    "every ~30 minutes, OFF the trade-execution path. You are given VERIFIED facts "
    "(trust them) and your own running memory. Your job is RISK MANAGEMENT, not "
    "prediction: decide whether conditions favour standing down, and whether the "
    "tape leans one direction so the engine should AVOID the opposite side.\n"
    "Rules:\n"
    "- You can only REDUCE risk. You may say 'stand_down', 'reduce', or lean a side "
    "(which makes the engine avoid the OTHER side). You CANNOT force a trade.\n"
    "- Reason from the given facts only. If a fact is missing, say so — never invent.\n"
    "- Be willing to FLIP your thesis when the facts changed since last cycle; do not "
    "defend a stale view. If unclear, posture='choppy', lean='none', risk='normal'.\n"
    "- Use location: price vs prev-day high/low (PDH/PDL), week levels, max-pain, PCR, "
    "OI walls, VIX, gap, prior FII. Chasing extended moves past levels tends to fail.\n"
    "Respond with ONLY this JSON: {\"posture\":\"trend_up|trend_down|choppy|stand_down\", "
    "\"direction_lean\":\"CE|PE|none\", \"lean_confidence\":0.0-1.0, "
    "\"risk_flag\":\"normal|reduce|stand_down\", \"key_levels\":[numbers], "
    "\"thesis\":\"<=2 sentences, your running view\", \"reasoning\":\"<=1 sentence why this cycle\"}"
)


@dataclass(frozen=True)
class OversightVerdict:
    posture: str = "unknown"
    direction_lean: str = "none"
    lean_confidence: float = 0.0
    risk_flag: str = "normal"
    key_levels: tuple = ()
    thesis: str = ""
    reasoning: str = ""

    def to_risk_state(self) -> dict[str, Any]:
        """The risk-REDUCING variables the engine reads. Never trade-forcing.

        A high-confidence lean vetoes the OPPOSITE side (don't take the side the
        brain thinks is wrong) — e.g. a confident PE lean ⇒ veto CE entries.
        """
        veto_side = ""
        if self.lean_confidence >= _VETO_MIN_CONF:
            if self.direction_lean == "PE":
                veto_side = "CE"
            elif self.direction_lean == "CE":
                veto_side = "PE"
        return {
            "oversight_risk_flag": self.risk_flag,      # normal | reduce | stand_down
            "oversight_veto_side": veto_side,           # "" | CE | PE  (side to NOT take)
            "oversight_posture": self.posture,
            "oversight_lean": self.direction_lean,
            "oversight_lean_conf": round(float(self.lean_confidence), 3),
        }


def _normalise(obj: dict[str, Any]) -> OversightVerdict:
    posture = str(obj.get("posture", "")).strip().lower()
    if posture not in _VALID_POSTURE:
        posture = "unknown"
    lean = str(obj.get("direction_lean", "")).strip().upper()
    if lean not in _VALID_LEAN:
        lean = "none"
    risk = str(obj.get("risk_flag", "")).strip().lower()
    if risk not in _VALID_RISK:
        risk = "normal"
    conf = obj.get("lean_confidence")
    conf = max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.0
    levels = tuple(
        float(x) for x in (obj.get("key_levels") or [])
        if isinstance(x, (int, float)) and not isinstance(x, bool)
    )[:8]
    # a "none" lean carries no veto confidence
    if lean == "none":
        conf = 0.0
    return OversightVerdict(
        posture=posture,
        direction_lean=lean,
        lean_confidence=conf,
        risk_flag=risk,
        key_levels=levels,
        thesis=str(obj.get("thesis", "")).strip()[:400],
        reasoning=str(obj.get("reasoning", "")).strip()[:300],
    )


def reason(
    facts_dict: dict[str, Any],
    memory_context: dict[str, Any],
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout_s: float = 20.0,
    max_tokens: int = 512,
    temperature: float = 0.2,
    json_mode: bool = True,
) -> OversightVerdict:
    """One LLM reasoning cycle. Returns a neutral verdict on any failure (never raises)."""
    if not api_key:
        return OversightVerdict()
    import json as _json

    user = (
        f"Verified market facts now: {_json.dumps(facts_dict, sort_keys=True)}\n"
        f"Your memory (running thesis + recent cycles): {_json.dumps(memory_context, sort_keys=True)}\n"
        "Update your posture, lean, and risk flag for the next ~30 minutes."
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    try:
        content = chat_completion(
            base_url=base_url, api_key=api_key, model=model, messages=messages,
            timeout_s=timeout_s, max_tokens=max_tokens, temperature=temperature,
            json_mode=json_mode,
        )
        return _normalise(extract_json_object(content))
    except (LLMClientError, Exception) as exc:  # never raise — risk-reducing layer
        logger.warning("oversight reason failed: %s", exc)
        return OversightVerdict()


__all__ = ["OversightVerdict", "reason"]
