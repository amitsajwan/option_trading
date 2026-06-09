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
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

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
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    )
    body = {
        "contents": [{"parts": [{"text": _PROMPT}]}],
        "tools": [{"google_search": {}}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "option-trading-oversight/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        parts = data["candidates"][0]["content"]["parts"]
        text = " ".join(
            p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")
        ).strip()
        return text[:800]
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:200]
        except Exception:
            pass
        logger.warning("gemini_web HTTP %s: %s", exc.code, detail)
        return ""
    except Exception as exc:
        logger.warning("gemini_web failed: %s", exc)
        return ""


__all__ = ["fetch_web_context"]
