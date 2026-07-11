#!/usr/bin/env python3
"""Daily event-calendar refresh via Gemini (stdlib only — runs on the host).

Division of labor (2026-07-11 design): the LLM is the calendar's LIBRARIAN,
never a trading decision-maker. It retrieves scheduled market-moving events
(FOMC, RBI MPC, budget, elections, expiry specials) with web grounding; this
script validates hard (ISO dates, bounded horizon, size caps) and atomically
rewrites config/event_calendar.json — which the sellers' deterministic entry
veto reads fresh each day. Failure mode = keep yesterday's file (stale beats
absent); hallucinated events cost at most a skipped entry day and are visible
in the daily Telegram summary.

Env: GEMINI_API_KEY in /opt/option_trading/.env.llm (chmod 600).
Alerts: Telegram token/chat read from .env.compose.
Manual entries: any event with "manual": true in the file is PRESERVED across
refreshes — the human always wins.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import date, timedelta

REPO = "/opt/option_trading"
CAL = os.path.join(REPO, "config", "event_calendar.json")
ENV_LLM = os.path.join(REPO, ".env.llm")
ENV_COMPOSE = os.path.join(REPO, ".env.compose")
MODEL = os.getenv("CALENDAR_LLM_MODEL", "gemini-2.5-flash")
HORIZON_DAYS = 45
MAX_EVENTS = 40


def _read_env(path):
    d = {}
    if os.path.exists(path):
        for ln in open(path, encoding="utf-8", errors="surrogateescape"):
            ln = ln.strip()
            if "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip()
    return d


def _telegram(text):
    env = _read_env(ENV_COMPOSE)
    tok, chat = env.get("ALERT_TELEGRAM_TOKEN"), env.get("ALERT_TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=body,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def _ask_gemini(api_key):
    today = date.today().isoformat()
    prompt = (
        "List all SCHEDULED events in the next 45 days that typically move Indian "
        "index markets (NIFTY/BANKNIFTY): RBI MPC meeting dates and decision day, "
        "US FOMC meeting dates and decision day, Indian Union Budget, Indian "
        "general/major-state election result days, and US CPI release dates. "
        f"Today is {today}. Reply with ONLY a JSON array, no prose, each item "
        '{"date":"YYYY-MM-DD","label":"short name"}. Use exchange-relevant dates '
        "(IST). If a meeting spans days, list each day."
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.0},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{MODEL}:generateContent?key={api_key}")
    for attempt, payload in enumerate((body, {k: v for k, v in body.items() if k != "tools"})):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
            text = resp["candidates"][0]["content"]["parts"][0]["text"]
            return text, ("grounded" if attempt == 0 else "model-knowledge")
        except Exception as exc:
            err = str(exc)
    raise RuntimeError(f"gemini call failed: {err[:200]}")


def _parse_events(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in response")
    raw = json.loads(m.group(0))
    today = date.today()
    out, seen = [], set()
    for e in raw:
        try:
            d = date.fromisoformat(str(e.get("date", ""))[:10])
            label = str(e.get("label", "")).strip()[:60]
        except Exception:
            continue
        if not label or d < today or d > today + timedelta(days=HORIZON_DAYS):
            continue
        key = (d.isoformat(), label.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": d.isoformat(), "label": label, "source": "llm"})
    if not out:
        raise ValueError("0 valid events after validation")
    return out[:MAX_EVENTS]


def main():
    api_key = _read_env(ENV_LLM).get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("SKIP: no GEMINI_API_KEY in .env.llm — keeping existing calendar")
        return 0
    try:
        existing = json.load(open(CAL)) if os.path.exists(CAL) else {"events": []}
    except Exception:
        existing = {"events": []}
    manual = [e for e in existing.get("events", []) if e.get("manual")]
    try:
        text, mode = _ask_gemini(api_key)
        events = _parse_events(text)
    except Exception as exc:
        print(f"REFRESH FAILED ({exc}) — keeping existing calendar")
        _telegram(f"<b>EVENT CALENDAR</b> refresh failed ({str(exc)[:80]}) — "
                  f"using existing file ({len(existing.get('events', []))} events)")
        return 1
    merged = manual + [e for e in events
                       if not any(m["date"] == e["date"] for m in manual)]
    merged.sort(key=lambda e: e["date"])
    doc = {
        "_comment": ("Seller entry veto dates. AUTO-REFRESHED daily by "
                     "ops/gcp/event_calendar_refresh.py; add \"manual\": true to any "
                     "entry you edit by hand and it will be preserved."),
        "refreshed_at": date.today().isoformat(),
        "refresh_mode": mode,
        "events": merged,
    }
    tmp = CAL + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, CAL)
    nxt = ", ".join(f"{e['date'][5:]} {e['label']}" for e in merged[:6])
    print(f"OK: {len(merged)} events ({mode}); next: {nxt}")
    _telegram(f"<b>EVENT CALENDAR</b> refreshed ({mode}): {len(merged)} events.\n"
              f"Next: {nxt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
