"""LLM DIRECTION ADVISOR — the shared "CE or PE?" call.

One module, two callers:
  * offline measurement  → strategy_app/tools/llm_direction_replay.py
  * live SHADOW           → the engine entry hook (logs a pick, never changes the order)

Design (validated 2026-06-10, see project_llm_direction_test_2026-06-10):
  - Reasoning over our STRUCTURAL facts alone hits the ~56% direction ceiling — it does
    not beat the vwap baseline. So this advisor is built to also carry the one thing that
    can move a coin-flip: `web_context`, the live Gemini news/RBI/macro grounding (the
    NEW information our senses lack). Offline that field is empty; live it is filled.
  - Prompt is NEUTRAL: it states sign conventions but does NOT tell the model which way a
    signal points, because several textbook reads (ORB, 15m momentum) are anti-predictive
    on our data and we refuse to leak that losing logic in.
  - Never raises: any transport/parse failure returns an ABSTAIN verdict.

Provider split (ops/gcp/llm_providers.env.example): morning posture → Gemini; per-entry
direction → Groq (fast, high RPM, ~1 call/trade).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .providers.openai_compatible import (
    LLMClientError,
    chat_completion,
    extract_json_object,
)

logger = logging.getLogger(__name__)

# Named providers: name -> (base_url, default_model, env-var fallbacks for the key).
PROVIDERS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # gemini-2.5-flash is a THINKING model (eats output budget, truncates JSON at low
    # max_tokens); flash-lite is non-thinking, fast, clean JSON — right for bulk/per-bar.
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash-lite",
               ("GEMINI_API_KEY", "BRAIN_LLM_API_KEY")),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile",
             ("GROQ_API_KEY", "DIRECTION_LLM_API_KEY", "BRAIN_LLM_API_KEY")),
}

# Curated, human-readable facts handed to the model. Maps snapshot column -> label.
# Compact and meaningful — raw 100+ column dumps confuse the model. Depth/live-only
# fields can be added here; they are simply skipped when absent (include-if-available).
FACT_COLUMNS: dict[str, str] = {
    "ret_1m": "return_1m",
    "ret_3m": "return_3m",
    "ret_5m": "return_5m",
    "vwap_distance": "price_vs_vwap_frac",
    "ema_9_21_spread": "ema9_minus_ema21",
    "ema_9_slope": "ema9_slope",
    "ema_21_slope": "ema21_slope",
    "osc_rsi_14": "rsi_14",
    "adx_14": "adx_14",
    "osc_atr_ratio": "atr_ratio",
    "dist_from_day_high": "dist_from_day_high_frac",
    "dist_from_day_low": "dist_from_day_low_frac",
    "fut_flow_oi_change_5m": "fut_oi_change_5m",
    "fut_flow_oi_zscore_20": "fut_oi_zscore_20",
    "fut_flow_rel_volume_20": "fut_rel_volume_20",
    "opt_flow_pcr_oi": "pcr_oi",
    "pcr_change_5m": "pcr_change_5m",
    "pcr_change_15m": "pcr_change_15m",
    "atm_oi_ratio": "atm_oi_ratio_ce_pe",
    "opt_flow_atm_call_return_1m": "atm_call_return_1m",
    "opt_flow_atm_put_return_1m": "atm_put_return_1m",
    "vel_price_delta_30m": "price_delta_30m",
    "vel_price_acceleration": "price_acceleration",
    "vel_pcr_delta_30m": "pcr_delta_30m",
    "vel_ce_oi_build_rate": "ce_oi_build_rate",
    "vel_pe_oi_build_rate": "pe_oi_build_rate",
    "vel_iv_skew_delta_open": "iv_skew_delta_from_open",
    "vel_iv_compression_rate": "iv_compression_rate",
    "ctx_opening_range_breakout_up": "orb_breakout_up",
    "ctx_opening_range_breakout_down": "orb_breakout_down",
    "ctx_am_trend_strength": "am_trend_strength",
    "ctx_regime_trend_up": "regime_trend_up",
    "ctx_regime_trend_down": "regime_trend_down",
    "ctx_is_high_vix_day": "high_vix_day",
    "ctx_is_expiry_day": "is_expiry_day",
    "time_minute_of_day": "minute_of_day",
    # live-only microstructure (skipped when absent) — depth becomes available in live feed
    "depth_imbalance": "depth_imbalance",
    "depth_bid_ask_ratio": "depth_bid_ask_ratio",
}

# Prompt v2 — NEUTRAL framing. States sign conventions only; never says which way a
# signal points. Honest that direction is near coin-flip (encourages calibrated abstain).
SYSTEM_PROMPT = (
    "You are an intraday DIRECTION classifier for the BankNifty index (BankNifty options). "
    "An entry signal has already fired: a sizable index move (often >=100 points) is likely "
    "within the next ~15 minutes. The decision to trade is already made — your ONLY job is "
    "the SIDE: will the index be HIGHER ('CE') or LOWER ('PE') in ~15 minutes?\n"
    "Intraday direction is genuinely hard, close to a coin-flip, so:\n"
    "- Commit to CE or PE when the facts show real confluence one way.\n"
    "- Answer 'abstain' when they genuinely conflict or are flat. Abstaining is scored "
    "separately and beats a forced 50/50 guess — do NOT guess just to seem decisive.\n"
    "- Reason ONLY from the facts given; never assume a value you were not given. All facts "
    "are as-of the decision time (no lookahead).\n"
    "Fact conventions (read signs correctly):\n"
    "- *_frac and return_* are signed fractions of price (+ up/above, - down/below).\n"
    "- price_vs_vwap_frac>0 = price ABOVE vwap; ema9_minus_ema21>0 = fast above slow.\n"
    "- dist_from_day_high_frac<=0 (distance below today's high); dist_from_day_low_frac>=0.\n"
    "- pcr_oi = put/call OI ratio; *_build_rate and *_change are signed momentum of a field.\n"
    "- 1/0 flags (orb_breakout_up, regime_trend_down, ...) are on/off conditions only.\n"
    "- 'web_context', if present, is a SOFT real-time news/macro read — treat as colour, not "
    "gospel; the numeric facts win on direct conflict.\n"
    "Respond with ONLY this JSON: "
    "{\"direction\":\"CE|PE|abstain\",\"confidence\":0.0-1.0,\"reason\":\"<=1 sentence\"}"
)


@dataclass(frozen=True)
class DirectionVerdict:
    direction: str          # "CE" | "PE" | "ABSTAIN"
    confidence: float       # 0..1
    reason: str = ""
    model: str = ""
    grounded: bool = False  # was web_context supplied?
    error: str = ""         # non-empty if the call failed (→ ABSTAIN)

    @property
    def committed(self) -> bool:
        return self.direction in ("CE", "PE")

    def as_dict(self) -> dict[str, Any]:
        return {"direction": self.direction, "confidence": self.confidence,
                "reason": self.reason, "model": self.model,
                "grounded": self.grounded, "error": self.error}


def _num(x: Any) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # drop NaN


def build_facts(row: Mapping[str, Any]) -> dict[str, Any]:
    """Curated, rounded, non-null facts for the LLM from a snapshot-like mapping."""
    out: dict[str, Any] = {}
    for col, label in FACT_COLUMNS.items():
        if col not in row:
            continue
        v = _num(row[col])
        if v is None:
            continue
        out[label] = round(v, 6) if abs(v) < 1 else round(v, 2)
    return out


def build_facts_from_accessor(snap: Any) -> dict[str, Any]:
    """Live equivalent of build_facts — maps a SnapshotAccessor's properties to the
    same labelled fact bundle (the live snapshot is nested, not flat columns).

    Duck-typed (getattr) so it never hard-depends on SnapshotAccessor and silently
    skips any property that is absent or None. Depth, if wired into the accessor
    later, can be added here too (include-if-available).
    """
    out: dict[str, Any] = {}

    def put(label: str, val: Any) -> None:
        v = _num(val)
        if v is not None:
            out[label] = round(v, 6) if abs(v) < 1 else round(v, 2)

    put("return_1m", getattr(snap, "fut_return_1m", None))
    put("return_3m", getattr(snap, "fut_return_3m", None))
    put("return_5m", getattr(snap, "fut_return_5m", None))
    put("return_15m", getattr(snap, "fut_return_15m", None))
    put("price_vs_vwap_frac", getattr(snap, "price_vs_vwap", None))
    put("rsi_14", getattr(snap, "rsi_14", None))
    put("adx_14", getattr(snap, "adx_14", None))
    put("pcr_oi", getattr(snap, "pcr", None))
    put("pcr_change_5m", getattr(snap, "pcr_change_5m", None))
    put("pcr_change_15m", getattr(snap, "pcr_change_15m", None))
    put("vix_intraday_chg", getattr(snap, "vix_intraday_chg", None))
    put("minute_of_day", getattr(snap, "minutes_since_open", None))

    e9, e21 = _num(getattr(snap, "ema_9", None)), _num(getattr(snap, "ema_21", None))
    if e9 is not None and e21 is not None:
        put("ema9_minus_ema21", e9 - e21)
    ce_iv, pe_iv = _num(getattr(snap, "atm_ce_iv", None)), _num(getattr(snap, "atm_pe_iv", None))
    if ce_iv and pe_iv and ce_iv > 0:
        put("iv_skew_pe_over_ce", pe_iv / ce_iv)

    for label, attr in (("orb_high_broken", "orh_broken"),
                        ("orb_low_broken", "orl_broken"),
                        ("or_ready", "or_ready")):
        try:
            val = getattr(snap, attr, None)
            if val is not None:
                out[label] = int(bool(val))
        except Exception:
            pass

    # live-only microstructure (depth side-channel) — include if the accessor exposes it
    put("depth_imbalance", getattr(snap, "depth_imbalance", None))
    return out


def resolve_provider(name: str) -> tuple[str, str, tuple[str, ...]]:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"unknown direction provider {name!r}; choose from {list(PROVIDERS)}")


def ask_direction(
    facts: Mapping[str, Any],
    *,
    base_url: str,
    api_key: str,
    model: str,
    web_context: str = "",
    timeout_s: float = 20.0,
    max_tokens: int = 300,
    temperature: float = 0.1,
    max_retries: int = 3,
) -> DirectionVerdict:
    """Ask the LLM for CE/PE/abstain. Never raises — failures become an ABSTAIN verdict."""
    if not api_key:
        return DirectionVerdict("ABSTAIN", 0.0, model=model, error="no api key")

    facts_block = dict(facts)
    if web_context:
        facts_block["web_context"] = web_context[:800]
    user = (
        "An entry signal has fired; an index move is likely within ~15 minutes.\n"
        f"Decision-time facts: {json.dumps(facts_block, sort_keys=True)}\n"
        "Will the index be HIGHER (CE) or LOWER (PE) in ~15 minutes?"
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]

    content: Optional[str] = None
    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            content = chat_completion(
                base_url=base_url, api_key=api_key, model=model, messages=messages,
                timeout_s=timeout_s, max_tokens=max_tokens, temperature=temperature,
                json_mode=True,
            )
            break
        except LLMClientError as exc:
            last_err = str(exc)
            # free-tier RPM → 429: back off and retry; other errors abort immediately.
            if "429" in last_err and attempt < max_retries:
                time.sleep(min(60.0, 5.0 * (2 ** attempt)))
                continue
            logger.warning("direction advisor call failed: %s", last_err)
            return DirectionVerdict("ABSTAIN", 0.0, model=model,
                                    grounded=bool(web_context), error=last_err[:200])

    try:
        obj = extract_json_object(content or "")
    except LLMClientError as exc:
        return DirectionVerdict("ABSTAIN", 0.0, model=model,
                                grounded=bool(web_context), error=str(exc)[:200])

    d = str(obj.get("direction", "")).strip().upper()
    if d not in ("CE", "PE", "ABSTAIN"):
        d = "ABSTAIN"
    conf = obj.get("confidence")
    conf = max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.0
    return DirectionVerdict(
        direction=d, confidence=round(conf, 3),
        reason=str(obj.get("reason", "")).strip()[:200],
        model=model, grounded=bool(web_context),
    )


__all__ = ["DirectionVerdict", "ask_direction", "build_facts", "build_facts_from_accessor",
           "resolve_provider", "PROVIDERS", "FACT_COLUMNS", "SYSTEM_PROMPT"]
