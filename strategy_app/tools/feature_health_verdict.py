"""Daily feature-health verdict — the silent-degradation detector.

Runs inside a strategy container (systemd timer, 10:00 IST) and answers one
question: of the entry model's features, which were actually ALIVE in today's
live snapshots, and which were dead (100% NaN -> median-imputed on every bar)?

A dead feature is invisible in normal operation — inference median-fills it
and trades on. vix_current (the BankNifty model's #2 feature) was dead for
WEEKS before anyone noticed (2026-07-07). This makes it a daily one-liner:

    FEATURE HEALTH BANKNIFTY 2026-07-08: 45/47 live — DEAD: vix_current, vix_intraday_chg

Output goes to stdout (journald) and, when ALERT_TELEGRAM_TOKEN +
ALERT_TELEGRAM_CHAT_ID are set, to Telegram. Exit code: 0 all live,
1 dead/sick features found, 2 could not evaluate.

Usage:  python -m strategy_app.tools.feature_health_verdict [--events PATH] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from ..market.snapshot_accessor import SnapshotAccessor
from ..ml.bundle_inference import build_feature_row, load_joblib_bundle

# Bars before 09:45 IST are feature warmup by design — exclude from health.
SESSION_START_HHMM = "09:45"

def _find_events(explicit: str | None, instrument: str) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    # Instrument-specific path FIRST — containers can mount both instruments'
    # run dirs, and probing the wrong one silently scores the other book.
    candidates = (
        ("/app/.run/snapshot_app_nifty/events.jsonl", "/app/.run/snapshot_app/events.jsonl")
        if instrument == "NIFTY"
        else ("/app/.run/snapshot_app/events.jsonl", "/app/.run/snapshot_app_nifty/events.jsonl")
    )
    for cand in candidates:
        p = Path(cand)
        if p.exists():
            return p
    return None


def _send_telegram(text: str) -> None:
    token = (os.getenv("ALERT_TELEGRAM_TOKEN") or "").strip()
    chat = (os.getenv("ALERT_TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
    except Exception as exc:  # alerting must never crash the probe
        print(f"(telegram send failed: {exc})", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=os.getenv("FEATURE_HEALTH_EVENTS_PATH") or None)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    args = ap.parse_args()

    instrument = (os.getenv("STRATEGY_INSTRUMENT") or "BANKNIFTY").strip().upper()
    day = args.date or date.today().isoformat()

    bundle_path = (os.getenv("ENTRY_ML_MODEL_PATH") or "").strip()
    bundle = load_joblib_bundle(bundle_path, expected_kind="entry_only_bundle") if bundle_path else None
    if bundle is None:
        msg = f"FEATURE HEALTH {instrument} {day}: NO ENTRY BUNDLE at '{bundle_path}' — cannot evaluate"
        print(msg)
        _send_telegram(f"⚠️ {msg}")
        return 2

    features: list[str] = list(bundle.get("features") or [])
    events = _find_events(args.events, instrument)
    if events is None:
        msg = f"FEATURE HEALTH {instrument} {day}: no events.jsonl found — cannot evaluate"
        print(msg)
        _send_telegram(f"⚠️ {msg}")
        return 2

    nan_counts: dict[str, int] = {f: 0 for f in features}
    bars = 0
    for line in events.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        snap = d.get("snapshot", d)
        ts = str(snap.get("timestamp") or "")
        if not ts.startswith(day) or (len(ts) > 15 and ts[11:16] < SESSION_START_HHMM):
            continue
        row = build_feature_row(SnapshotAccessor(snap), features)
        if row is None:
            continue
        bars += 1
        for f, v in row.items():
            if isinstance(v, float) and math.isnan(v):
                nan_counts[f] += 1

    if bars == 0:
        msg = f"FEATURE HEALTH {instrument} {day}: 0 session bars in {events} — snapshots not flowing!"
        print(msg)
        _send_telegram(f"🚨 {msg}")
        return 2

    dead = sorted(f for f, n in nan_counts.items() if n == bars)
    sick = sorted(f for f, n in nan_counts.items() if bars > n > bars * 0.5 and f not in dead)
    live = len(features) - len(dead) - len(sick)

    parts = [f"FEATURE HEALTH {instrument} {day}: {live}/{len(features)} live ({bars} bars)"]
    if dead:
        parts.append(f"DEAD: {', '.join(dead)}")
    if sick:
        parts.append(f"SICK(>50% NaN): {', '.join(sick)}")
    msg = " — ".join(parts)
    print(msg)

    if dead or sick:
        _send_telegram(f"🚨 {msg}")
        return 1
    _send_telegram(f"✅ {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
