"""Trailing-profit watcher for a MANUAL option position (2026-07-10).

Why: a resting broker SL on a long option demands full option-WRITING margin
(Dhan RMS does not net it against the long — measured 2026-07-09), which
doesn't fit the account. This watcher trails in-app and market-sells through
DhanAdapter when the trail breaks.

Marks come from the live NIFTY snapshot chain in Mongo (1-min cadence — the
same feed the seller trades on). Trail: sell when LTP <= max(floor,
peak * (1 - trail_pct)). Peak ratchets up only.

Run inside seller_app_nifty (has creds + mongo + strategy/execution code):
  python /tmp/manual_trail.py --strike 24050 --otype CE --qty 130 \
      --entry 134.43 --trail-pct 0.15 --floor 170 --expiry 2026-07-14

Exits the process after selling (or on unrecoverable errors) — one position,
one lifecycle. Alerts via Telegram on arm/ratchet/sell/stale-data.
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import date, datetime, timezone


def _alert(text: str) -> None:
    try:
        from execution_app.alerts import _send_telegram
        _send_telegram(text)
    except Exception:
        pass
    print(f"[{datetime.now(timezone.utc).isoformat()}] {text}", flush=True)


def _ltp(coll, strike: int, otype: str):
    doc = coll.find_one(sort=[("timestamp", -1)])
    if not doc:
        return None, None
    snap = (doc.get("payload") or {}).get("snapshot") or {}
    ts = snap.get("timestamp")
    for row in snap.get("strikes") or []:
        if int(row.get("strike") or 0) == strike:
            v = row.get("ce_ltp" if otype == "CE" else "pe_ltp")
            return (float(v) if v else None), ts
    return None, ts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strike", type=int, required=True)
    ap.add_argument("--otype", default="CE")
    ap.add_argument("--qty", type=int, required=True)
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--trail-pct", type=float, default=0.15)
    ap.add_argument("--floor", type=float, required=True, help="hard profit floor price")
    ap.add_argument("--expiry", required=True)
    ap.add_argument("--interval", type=float, default=30.0)
    args = ap.parse_args()

    from pymongo import MongoClient
    from execution_app.adapter.dhan import DhanAdapter

    db = MongoClient(os.getenv("MONGO_HOST", "mongo"), 27017)[os.getenv("MONGO_DB", "trading_ai")]
    coll = db["phase1_market_snapshots_nifty"]
    adapter = DhanAdapter()
    expiry = date.fromisoformat(args.expiry)

    ltp, _ = _ltp(coll, args.strike, args.otype)
    peak = max(ltp or 0.0, args.floor / (1 - args.trail_pct))
    _alert(f"<b>MANUAL TRAIL ARMED</b> {args.otype} {args.strike} x{args.qty}"
           f"\nentry {args.entry} · now {ltp} · peak {peak:.1f}"
           f"\ntrail {args.trail_pct:.0%} from peak · hard floor {args.floor}")

    stale_alerted = False
    while True:
        try:
            ltp, ts = _ltp(coll, args.strike, args.otype)
            now_hhmm = datetime.now(timezone.utc).strftime("%H%M")
            # market closed (UTC 10:00 = 15:30 IST): stop for the day with a status note
            if now_hhmm >= "1001":
                _alert(f"<b>MANUAL TRAIL — SESSION END</b> {args.otype} {args.strike}: "
                       f"still holding, ltp {ltp}, peak {peak:.1f}, "
                       f"stop {max(args.floor, peak * (1 - args.trail_pct)):.1f}. "
                       f"RE-ARM next session.")
                return 0
            if ltp is None:
                if not stale_alerted:
                    _alert(f"MANUAL TRAIL: no mark for {args.otype} {args.strike} (chain shift?) — watching")
                    stale_alerted = True
                time.sleep(args.interval)
                continue
            stale_alerted = False
            if ltp > peak:
                if ltp > peak * 1.05:
                    _alert(f"MANUAL TRAIL ratchet: peak {peak:.1f} → {ltp:.1f} "
                           f"(stop now {max(args.floor, ltp * (1 - args.trail_pct)):.1f})")
                peak = ltp
            stop = max(args.floor, peak * (1 - args.trail_pct))
            if ltp <= stop:
                _alert(f"<b>MANUAL TRAIL TRIGGERED</b> {args.otype} {args.strike}: "
                       f"ltp {ltp:.1f} <= stop {stop:.1f} — selling {args.qty} at market")
                sec, _lot = adapter._scrips.resolve(expiry, args.strike, args.otype)
                res = adapter._place(security_id=sec, qty=args.qty, side="SELL", tag="manual-trail")
                if res.status == "placed":
                    time.sleep(3)
                    st = adapter.get_order_status(res.order_id)
                    pnl = ((st.fill_price or ltp) - args.entry) * args.qty
                    _alert(f"<b>MANUAL TRAIL SOLD</b> {args.qty} @ {st.fill_price or '~' + str(ltp)}"
                           f" · P&L ~₹{pnl:,.0f} (entry {args.entry})")
                    return 0
                _alert(f"<b>MANUAL TRAIL SELL FAILED</b>: {res.error} — retrying next cycle")
        except Exception as exc:  # keep watching; a blip must not orphan the trail
            print("trail loop error:", exc, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
