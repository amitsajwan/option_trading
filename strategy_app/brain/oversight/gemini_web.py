"""ONLINE-DATA sense — Gemini web grounding (Gemini browses, Groq reasons).

Calls Gemini ``generateContent`` with the ``google_search`` tool via raw HTTP
(zero new dependency, same style as the Groq client) to pull live, cited market
context the deterministic facts can't provide: today's scheduled events
(RBI/Fed/CPI/results/expiry), prior-session FII/DII, and the broad market tone
from current news.

Discipline:
- **Quota-friendly:** called ONCE per day (pre-open), cached in the scratchpad —
  Gemini grounding has tight free limits (429s).
- **Soft, not trusted:** Gemini is an LLM that can hallucinate. Its output is
  passed to Groq as *context to weigh skeptically*, never as ground truth, and the
  prompt tells Gemini to write "unknown" rather than guess.
- **Never raises:** any error / 429 → returns "" and the brain proceeds without it.

Note: web grounding needs the **native** Gemini endpoint + ``google_search`` tool;
it does NOT work through the OpenAI-compatible endpoint (which is why this is a
separate fetcher from ``openai_compatible.py``).
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def _salvage_brief(text: str) -> dict | None:
    """Regex-salvage the key brief fields from a TRUNCATED or grounding-decorated
    response (Gemini's grounded reply sometimes cuts off mid-JSON), so a valid bias
    isn't lost just because the object never closed. Returns None if no bias found."""
    def field(key: str, quoted: bool = True):
        if quoted:
            m = re.search(r'"' + key + r'"\s*:\s*"([^"]*)"', text)
        else:
            m = re.search(r'"' + key + r'"\s*:\s*([0-9.]+|true|false)', text, re.I)
        return m.group(1) if m else None

    bias = field("day_bias")
    if not bias:
        return None
    out: dict = {"day_bias": bias}
    conv = field("conviction", quoted=False)
    if conv is not None:
        try:
            out["conviction"] = float(conv)
        except ValueError:
            pass
    gr = field("grounded", quoted=False)
    if gr is not None:
        out["grounded"] = gr.strip().lower() == "true"
    ns = field("news_summary")
    if ns:
        out["news_summary"] = ns
    return out

_DEFAULT_MODEL = "gemini-2.5-flash"
_PROMPT = (
    "In 2-3 short factual lines, give today's BankNifty / Nifty 50 market context for "
    "an Indian intraday options trader: (1) any major SCHEDULED events today or this "
    "week (RBI policy, US FOMC, US/India CPI, major results, F&O expiry, NSE holiday); "
    "(2) the most recent FII/DII cash flow if known; (3) the broad market tone from "
    "current news/global cues. Only state what you can verify from search RIGHT NOW; "
    "write 'unknown' for anything you cannot verify. No advice, no predictions — just facts."
)


def fetch_web_context(*, api_key: str, model: str | None = None, timeout_s: float = 25.0) -> str:
    """Return a short, search-grounded market-context string ("" on any failure)."""
    if not api_key:
        return ""
    model = (model or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    return _call_gemini(_PROMPT, api_key=api_key, model=model, timeout_s=timeout_s)[:800]


_RETRYABLE_HTTP = {500, 502, 503, 504}  # transient server / overload ("high demand")


def _call_gemini(prompt: str, *, api_key: str, model: str, timeout_s: float,
                 retries: int = 3) -> str:
    """POST one grounded prompt to Gemini generateContent; return joined text or ''.
    Retries transient failures (timeouts + 5xx "high demand") with backoff — the
    grounded google_search call 503s/times-out intermittently. A 4xx (auth/quota)
    is non-transient -> return '' immediately (no point retrying depleted credits)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
    data_bytes = json.dumps(body).encode("utf-8")
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(
            url, data=data_bytes, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "option-trading-oversight/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parts = data["candidates"][0]["content"]["parts"]
            text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")).strip()
            if text:
                return text
            # empty body (rare) -> treat as transient and retry
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:160]
            except Exception:
                pass
            logger.warning("gemini_web HTTP %s (attempt %d/%d): %s", exc.code, attempt + 1, retries, detail)
            if exc.code not in _RETRYABLE_HTTP:
                return ""  # auth/quota/bad-request -> don't retry
        except Exception as exc:  # timeout / connection reset -> retry
            logger.warning("gemini_web failed (attempt %d/%d): %s", attempt + 1, retries, exc)
        if attempt < retries - 1:
            time.sleep(2.0 * (attempt + 1))  # 2s, 4s backoff
    return ""


def _brief_prompt(ctx: dict, *, phase: str = "morning", prior: dict | None = None) -> str:
    """Clear, well-guided session-brief prompt: LIVE news first, then OUR levels, then a
    structured view. Demands a `grounded` flag so hallucinated (non-retrieved) views are
    visible and discounted downstream.

    phase="morning"  -> pre-open thesis (overnight cues, scheduled events).
    phase="intraday" -> 30-min update: what's NEW since open + reassess the prior thesis.
    """
    def g(k, d="unknown"):
        v = ctx.get(k)
        return d if v is None else v
    ctx_lines = (
        f"- date/time: {g('date')} ~{g('time')} IST, days_to_expiry={g('days_to_expiry')}\n"
        f"- spot/futures now: {g('spot')}, today_open: {g('open')}\n"
        f"- prev day: high={g('prev_day_high')} low={g('prev_day_low')} close={g('prev_day_close')}\n"
        f"- opening range: high={g('orb_high')} low={g('orb_low')}\n"
        f"- OI walls: call_wall(resistance)={g('call_wall')} put_wall(support)={g('put_wall')} max_pain={g('max_pain')}\n"
        f"- India VIX: {g('vix')} ({g('vix_regime')})"
    )
    # Explicit, PHASE-AWARE date anchor so the model can't drift to "today's real
    # date" on replay, and so the morning vs intraday scope is unambiguous.
    if phase == "intraday":
        scope = (f"analyse {g('date')}'s session INCLUDING intraday developments through ~{g('time')} "
                 f"IST; \"yesterday\" = the prior trading session before {g('date')}")
    else:
        scope = (f"analyse the run-up to the {g('date')} session — overnight before {g('date')}, "
                 f"{g('date')} pre-market, and the prior trading session's FII/DII")
    anchor = (
        f"TODAY'S TRADING SESSION IS {g('date')}, current time ~{g('time')} IST. Everything below "
        f"(\"overnight\", \"yesterday\", \"today\", \"now\") is RELATIVE TO {g('date')}: {scope}. "
        f"Do NOT analyse any other date.\n\n"
    )
    if phase == "intraday":
        pr = prior or {}
        prior_line = (
            f'Your PRIOR view this session was: bias={pr.get("day_bias", "NEUTRAL")} '
            f'conviction={pr.get("conviction", 0.0)} grounded={pr.get("grounded", False)} '
            f'— "{str(pr.get("news_summary") or "")[:200]}".\n'
        )
        step1 = (
            f"It is now {g('time')} IST, MID-SESSION. " + prior_line +
            "STEP 1 - RETRIEVE LIVE INFO NOW via web search (do NOT use training memory): any "
            "NEW or breaking developments SINCE THE OPEN that move Indian banks/Nifty — breaking "
            "headlines, RBI/government statements, a shift in global/US-futures tone, big single-bank "
            "news (HDFC/ICICI/SBI/Axis/Kotak), or an intraday event. State only what search verifies now.\n\n"
            "STEP 1b - REASSESS the prior view: is it still valid? Hold it, flip it, or cut conviction "
            "based on what is ACTUALLY happening plus the current levels below.\n"
        )
        role = ("You are an intraday analyst updating a BankNifty (NSE: NIFTY BANK) options trader "
                "mid-session. Be concrete and skeptical.\n\n")
    else:
        step1 = (
            "This is the SESSION OPEN — the day's range is still forming (treat today_open / opening "
            "range below as the early picture, not a settled trend). Set the day thesis from the "
            "overnight set-up.\n"
            "STEP 1 - RETRIEVE LIVE INFO NOW via web search (do NOT use training memory; only state "
            "what current search results verify):\n"
            "  - Overnight/global cues: US close (Dow/Nasdaq/S&P), GIFT Nifty / SGX Nifty indication for "
            "India's open, Asian markets, Brent crude, USD/INR.\n"
            "  - India/bank-specific: RBI actions or commentary, major banking-sector news, yesterday's "
            "FII/DII cash flow.\n"
            "  - Scheduled TODAY/this week: India/US CPI, FOMC, RBI policy, big bank results, F&O expiry, NSE holiday.\n"
        )
        role = ("You are a pre-market analyst for an Indian intraday options trader trading BankNifty "
                "(NSE: NIFTY BANK) weekly options. Be concrete and skeptical.\n\n")
    return (
        role + anchor + step1 + "\n"
        "STEP 2 - OUR STRUCTURAL CONTEXT (BankNifty today):\n" + ctx_lines + "\n\n"
        "STEP 3 - Give your view, grounded in the STEP-1 news AND STEP-2 levels: the directional "
        "bias for the rest of the session (banks trend up, down, or chop?), conviction, the levels "
        "that matter now, and a concrete scenario plan. Cite the real news you used.\n\n"
        "Respond with ONLY this JSON (no prose, no markdown):\n"
        '{"day_bias":"BULLISH|BEARISH|NEUTRAL","conviction":0.0,"grounded":true,'
        '"news_summary":"<=2 sentences of the REAL retrieved news/cues","key_levels":'
        '{"support":[],"resistance":[]},"plan":"<=2 sentences of scenarios","risks":"<=1 sentence",'
        '"as_of":"' + str(g("date")) + " " + str(g("time")) + ' IST"}\n'
        'Set "grounded":false if you could NOT retrieve real current news; then "day_bias" must be '
        '"NEUTRAL" unless the levels alone justify a lean (say so in news_summary).'
    )


def fetch_session_brief(context: dict, *, api_key: str, model: str | None = None,
                        timeout_s: float = 40.0, phase: str = "morning",
                        prior: dict | None = None) -> dict:
    """Context-aware, search-grounded session brief. Returns a dict with day_bias /
    conviction / grounded / news_summary / key_levels / plan / risks. Never raises;
    on any failure returns a NEUTRAL, ungrounded brief. ``phase`` = morning | intraday;
    ``prior`` is the previous brief, reassessed on intraday refreshes."""
    neutral = {"day_bias": "NEUTRAL", "conviction": 0.0, "grounded": False,
               "news_summary": "", "key_levels": {}, "plan": "", "risks": "", "as_of": ""}
    if not api_key:
        return neutral
    model = (model or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    text = _call_gemini(_brief_prompt(context or {}, phase=phase, prior=prior),
                        api_key=api_key, model=model, timeout_s=timeout_s)
    if not text:
        return neutral
    try:
        from ..providers.openai_compatible import extract_json_object
        obj = extract_json_object(text)
    except Exception:
        obj = None
    if not isinstance(obj, dict):
        obj = _salvage_brief(text)   # truncated/decorated JSON -> regex-salvage the bias
    if not isinstance(obj, dict):
        return {**neutral, "news_summary": text[:400]}
    out = {**neutral, **obj}
    bias = str(out.get("day_bias") or "NEUTRAL").upper()
    out["day_bias"] = bias if bias in ("BULLISH", "BEARISH", "NEUTRAL") else "NEUTRAL"
    try:
        out["conviction"] = max(0.0, min(1.0, float(out.get("conviction") or 0.0)))
    except (TypeError, ValueError):
        out["conviction"] = 0.0
    out["grounded"] = bool(out.get("grounded"))
    return out


__all__ = ["fetch_web_context", "fetch_session_brief"]
