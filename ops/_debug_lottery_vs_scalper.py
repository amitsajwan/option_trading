#!/usr/bin/env python3
"""One-off: split closed trades by entry_regime (lottery-routed BREAKOUT/TRENDING
vs everything-else/scalper), show MFE-vs-realized-pnl capture for each, to check
whether the lottery stack's wide giveback tolerance is actually earning its keep
(rare big win) or just giving back modest real profit (the common case)."""
import sys
from pymongo import MongoClient

cutoff = sys.argv[1] if len(sys.argv) > 1 else "2026-07-15T20:26:36+05:30"
run_pattern = sys.argv[2] if len(sys.argv) > 2 else "replay-multiday-2026-06-0[1-4]-banknifty"
LOTTERY_REGIMES = {"BREAKOUT", "TRENDING"}

db = MongoClient("mongo", 27017).trading_ai
closes = list(db.strategy_positions_historical.find({
    "exit_reason": {"$ne": None},
    "run_id": {"$regex": f"^{run_pattern}$"},
    "received_at_ist": {"$gte": cutoff},
}).sort("timestamp", 1))

seen = set()
trades = []
for p in closes:
    pos = (p.get("payload") or {}).get("position") or {}
    key = (p.get("trade_date_ist"), p.get("strike"), p.get("direction"),
           pos.get("bars_held"), round((p.get("pnl_pct") or 0), 6), p.get("exit_reason"))
    if key in seen:
        continue
    seen.add(key)
    regime = str(pos.get("entry_regime") or "").upper()
    trades.append({
        "date": p.get("trade_date_ist"), "strike": p.get("strike"), "dir": p.get("direction"),
        "bars": pos.get("bars_held"), "pnl": (p.get("pnl_pct") or 0) * 100,
        "mfe": (pos.get("mfe_pct") or 0) * 100, "reason": p.get("exit_reason"),
        "regime": regime, "stack": "lottery" if regime in LOTTERY_REGIMES else "scalper",
    })

for stack in ("lottery", "scalper"):
    grp = [t for t in trades if t["stack"] == stack]
    print(f"\n=== {stack.upper()} ({len(grp)} trades) ===")
    for t in grp:
        capture = (t["pnl"] / t["mfe"] * 100) if t["mfe"] > 0.01 else None
        cap_str = f"capture={capture:6.1f}%" if capture is not None else "capture=   n/a"
        big = " BIG_WIN(mfe>=20%)" if t["mfe"] >= 20 else ""
        print(f"{t['date']} {t['strike']:>6} {t['dir']:>3} bars={t['bars']:>3} regime={t['regime']:<10} "
              f"pnl={t['pnl']:7.2f}% mfe={t['mfe']:6.2f}% {cap_str} {t['reason']:<16}{big}")
    if grp:
        total = sum(t["pnl"] for t in grp)
        wins = sum(1 for t in grp if t["pnl"] > 0)
        big_wins = [t for t in grp if t["mfe"] >= 20]
        modest = [t for t in grp if t["mfe"] < 20]
        modest_total = sum(t["pnl"] for t in modest)
        big_total = sum(t["pnl"] for t in big_wins)
        print(f"\nTOTAL: {total:.2f}%  avg={total/len(grp):.2f}%/trade  win_rate={100*wins/len(grp):.1f}%  n={len(grp)}")
        print(f"  of which: {len(big_wins)} reached >=20% MFE (contributed {big_total:+.2f}%), "
              f"{len(modest)} peaked <20% MFE (contributed {modest_total:+.2f}%)")
