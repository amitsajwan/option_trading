"""Historical snapshot backfill via Dhan /charts/rollingoption (Phase 1 depth).

2026-07-10 discovery: Dhan's rollingoption endpoint serves 1-min premium/IV/OI
for ANY strike back to (at least) Nov 2024 — expired contracts resolved
server-side. This overturns the old "no historical option-chain data" verdict
and unlocks the full Seller v2 Phase 1 window.

Pipeline (all existing pieces):
  ingestion_app /api/v1/historical/day/{instrument}?date=D&strikes=12
    -> build_snapshots_from_dhan_data()  (runtime snapshot format)
    -> Mongo `phase1_market_snapshots_hist`  (SEPARATE collection — never mix
       backfilled data with the live-collected `phase1_market_snapshots`)
    -> strategy_app.seller.replay --coll phase1_market_snapshots_hist

Run inside the dashboard container (has this package + pymongo + network):
  python -m market_data_dashboard.services.hist_backfill \
      --from 2024-11-01 --to 2026-05-23 [--instrument BANKNIFTY]

Idempotent per day (existing day docs are deleted before insert). Weekends are
skipped; holidays return 0 index bars and are skipped automatically. Throttled
~1 day/min to respect Dhan rate limits (50+ rollingoption calls per day built).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

from .dhan_replay_builder import build_snapshots_from_dhan_data

_INGESTION = os.getenv("INGESTION_BASE", "http://ingestion_app:8004")
_COLL = "phase1_market_snapshots_hist"


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """weekday: Mon=0 .. Sun=6."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def bn_monthly_expiry(d: date) -> date:
    """BANKNIFTY monthly expiry for the month containing/after d.

    Exchange expiry-day changed over the window (VERIFY against contract notes
    if DTE-sensitive results look off):
      Nov-2024 .. Mar-2025 : last Wednesday   (weekly BN discontinued Nov-2024)
      Apr-2025 .. Aug-2025 : last Thursday    (NSE moved index F&O expiry)
      Sep-2025 .. now      : last Tuesday     (SEBI Tue/Thu rule; NSE chose Tue —
                                               2026-07-28 IS the last Tuesday ✓)
    """
    def _day_for(y: int, m: int) -> int:
        if (y, m) <= (2025, 3):
            return 2  # Wednesday
        if (y, m) <= (2025, 8):
            return 3  # Thursday
        return 1      # Tuesday

    exp = _last_weekday_of_month(d.year, d.month, _day_for(d.year, d.month))
    if exp < d:  # already past this month's expiry -> next month's
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        exp = _last_weekday_of_month(y, m, _day_for(y, m))
    return exp


def _enrich_for_seller(snapshots: list[dict], trade_date: str) -> None:
    """Two gaps in the replay builder's output that the seller needs:
    futures_bar.fut_close (it writes 'close'; SnapshotAccessor reads 'fut_close')
    and session_context.days_to_expiry (it writes None; DTE drives seller exits)."""
    d = date.fromisoformat(trade_date)
    exp = bn_monthly_expiry(d)
    dte = (exp - d).days
    for s in snapshots:
        fb = s.get("futures_bar") or {}
        if "fut_close" not in fb:
            fb["fut_close"] = fb.get("close")
        sc = s.get("session_context") or {}
        sc["days_to_expiry"] = dte
        sc["is_expiry_day"] = dte == 0
        sc["expiry"] = exp.isoformat()


def backfill_day(mongo_db, instrument: str, trade_date: str, strikes: int = 10) -> int:
    r = requests.get(
        f"{_INGESTION}/api/v1/historical/day/{instrument}",
        params={"date": trade_date, "strikes": strikes, "interval": "1"},
        timeout=600,
    )
    r.raise_for_status()
    raw = r.json()
    if not raw.get("index_bars"):
        return 0  # holiday / no session
    snapshots = build_snapshots_from_dhan_data(raw)
    _enrich_for_seller(snapshots, trade_date)
    coll = mongo_db[_COLL]
    coll.delete_many({"trade_date_ist": trade_date})  # idempotent per day
    docs = [{
        "event_type": "hist_backfill",
        "snapshot_id": s.get("snapshot_id"),
        "instrument": instrument,
        "timestamp": s.get("timestamp"),
        "trade_date_ist": trade_date,
        "payload": {"snapshot": s},
    } for s in snapshots]
    if docs:
        coll.insert_many(docs)
    return len(docs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", required=True)
    ap.add_argument("--to", dest="d_to", required=True)
    ap.add_argument("--instrument", default="BANKNIFTY")
    ap.add_argument("--strikes", type=int, default=10)  # ingestion endpoint caps le=10 (ATM±10 = 21 strikes; seller legs live within ±5)
    ap.add_argument("--pause", type=float, default=20.0, help="seconds between days (rate limit)")
    args = ap.parse_args()

    from pymongo import MongoClient
    db = MongoClient(os.getenv("MONGO_HOST", "mongo"),
                     int(os.getenv("MONGO_PORT", "27017") or 27017))[
        os.getenv("MONGO_DB", "trading_ai")]

    d = date.fromisoformat(args.d_from)
    end = date.fromisoformat(args.d_to)
    total_days = total_snaps = 0
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            t0 = time.time()
            try:
                n = backfill_day(db, args.instrument, d.isoformat(), args.strikes)
            except Exception as exc:
                print(f"{d} ERROR {str(exc)[:140]}", flush=True)
                n = -1
            if n > 0:
                total_days += 1
                total_snaps += n
                print(f"{d} OK {n} snapshots ({time.time()-t0:.0f}s) "
                      f"[{total_days} days, {total_snaps} total]", flush=True)
            elif n == 0:
                print(f"{d} holiday/empty", flush=True)
            time.sleep(args.pause)
        d += timedelta(days=1)
    print(f"BACKFILL_DONE days={total_days} snapshots={total_snaps}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
