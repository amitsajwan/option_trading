"""Which OTM depth actually maximises P&L? (operator: tune depth, mind expiry premiums)

For every LOADED entry bar, compute the REAL-fill P&L at each OTM depth (ATM,1,2,3,4,6,8,12)
using the actual per-strike chain premiums + the held-strike exit sim, at PERFECT direction
(side = the way the move actually went) to isolate the STRIKE-DEPTH effect from direction.
Reports avg net per depth, and bucketed by days-to-expiry — so we can see that the right
depth (and premium band) shifts as expiry approaches (premiums shrink near expiry).

RUN: docker compose run --rm --no-deps -v .../ops:/app/ops --entrypoint sh strategy_app \
       -c 'pip install pymongo -q; python ops/research/otm_depth_pnl.py'
"""
from __future__ import annotations

import os
from collections import defaultdict

from strategy_app.cost_model import TradingCostModel
from strategy_app.position.exit_sim import simulate_exit_real
from strategy_app.senses.context import build_contexts
from strategy_app.senses.move import MoveSense

HORIZON = 10
DEPTHS = [0, 1, 2, 3, 4, 6, 8, 12]
LOT = 30
_COST = TradingCostModel()


def _load():
    from pymongo import MongoClient
    host = os.getenv("MONGO_HOST", "mongo")
    db = MongoClient(f"mongodb://{host}:27017")[os.getenv("MONGO_DB", "trading_ai")]
    coll = db[os.getenv("BIGMOVE_SOURCE_COLL", "phase1_market_snapshots")]
    days = sorted(str(d) for d in coll.distinct("trade_date_ist") if d)
    days_bars, dte_by_day = {}, {}
    for day in days:
        rows = []
        for d in coll.find({"trade_date_ist": day}).sort("timestamp", 1):
            s = (d.get("payload") or {}).get("snapshot") or {}
            f = s.get("futures_bar") or {}
            ca = s.get("chain_aggregates") or {}
            sc = s.get("session_context") or {}
            if sc.get("days_to_expiry") is not None:
                dte_by_day[day] = int(sc.get("days_to_expiry"))
            chain = {}
            for r in (s.get("strikes") or []):
                k = r.get("strike")
                if k is None:
                    continue
                chain[int(round(float(k)))] = {"ce": r.get("ce_ltp"), "pe": r.get("pe_ltp"),
                                               "ce_h": r.get("ce_high"), "ce_l": r.get("ce_low"),
                                               "pe_h": r.get("pe_high"), "pe_l": r.get("pe_low")}
            rows.append({"c": f.get("fut_close"), "h": f.get("fut_high"), "l": f.get("fut_low"),
                         "ovol": (ca.get("total_ce_volume") or 0) + (ca.get("total_pe_volume") or 0),
                         "ooi": (ca.get("total_ce_oi") or 0) + (ca.get("total_pe_oi") or 0), "chain": chain})
        days_bars[day] = rows
    return days_bars, dte_by_day


def _fin(x):
    try:
        x = float(x)
        return x if x == x and x > 0 else None
    except (TypeError, ValueError):
        return None


def _strike_path(bars, i, strike, side, horizon):
    """Premium %-path (best,worst,close) for a specific held strike+side over the horizon."""
    row0 = (bars[i].get("chain") or {}).get(strike)
    if not row0:
        return None, None
    entry = _fin(row0.get("ce" if side == "CE" else "pe"))
    if not entry:
        return None, None
    pk = "ce" if side == "CE" else "pe"
    path, last = [], None
    for x in bars[i + 1:i + 1 + horizon]:
        r = (x.get("chain") or {}).get(strike)
        if r:
            last = r
        rr = last or {}
        px = _fin(rr.get(pk))
        if px is None:
            continue
        hi = _fin(rr.get(pk + "_h")) or px
        lo = _fin(rr.get(pk + "_l")) or px
        path.append(((hi - entry) / entry, (lo - entry) / entry, (px - entry) / entry))
    return entry, path


def main() -> None:
    days_bars, dte_by_day = _load()
    ctxs = build_contexts(days_bars, horizon=HORIZON)
    move = MoveSense()
    # depth -> list of (dte, net%, entry_premium)
    agg: dict[int, list] = defaultdict(list)
    for ctx in ctxs:
        if move.evaluate(ctx.as_mapping()).verdict not in ("loaded", "released"):
            continue
        if ctx.future_signed_move_pt is None or ctx.future_signed_move_pt == 0:
            continue
        side = "CE" if ctx.future_signed_move_pt > 0 else "PE"   # perfect direction
        bars = days_bars[ctx.day]
        chain = bars[ctx.index].get("chain") or {}
        if not chain:
            continue
        atm = min(chain, key=lambda k: abs(k - ctx.close))
        ks = sorted(chain)
        step = min((b - a for a, b in zip(ks, ks[1:]) if b > a), default=100)
        dte = dte_by_day.get(ctx.day, -1)
        for d in DEPTHS:
            strike = atm + d * step if side == "CE" else atm - d * step
            entry, path = _strike_path(bars, ctx.index, strike, side, HORIZON)
            if not entry or not path:
                continue
            ev = entry * LOT
            cost = _COST.breakdown(entry_value=ev, exit_value=ev)["total_cost_amount"] / ev
            net = float(simulate_exit_real(path)["exit_pct"]) - cost
            agg[d].append((dte, net, entry))

    print("OTM-depth vs REAL-fill P&L @ perfect direction (loaded bars). Net = % of premium.\n")
    print(f"{'depth':>6} {'n':>4} {'avg_net%':>9} {'avg_prem':>9} {'win%':>6}")
    for d in DEPTHS:
        rows = agg[d]
        if not rows:
            print(f"{d:>6} {0:>4}")
            continue
        nets = [n for _, n, _ in rows]
        avg = sum(nets) / len(nets) * 100
        prem = sum(p for _, _, p in rows) / len(rows)
        win = sum(1 for n in nets if n > 0) / len(nets) * 100
        print(f"{d:>6} {len(rows):>4} {avg:>8.2f}% {prem:>8.0f} {win:>5.0f}%")

    # by DTE bucket: best depth shifts as premiums shrink near expiry
    print("\nAvg net% by DTE bucket x depth (premiums shrink near expiry -> best depth shifts):")
    buckets = {"<=2": lambda x: x <= 2, "3-7": lambda x: 3 <= x <= 7, ">7": lambda x: x > 7}
    print(f"{'depth':>6} " + " ".join(f"{b:>10}" for b in buckets))
    for d in DEPTHS:
        line = f"{d:>6} "
        for _, fn in buckets.items():
            nets = [n for dte, n, _ in agg[d] if dte >= 0 and fn(dte)]
            line += f"{(sum(nets) / len(nets) * 100 if nets else 0):>9.2f}% "
        print(line)


if __name__ == "__main__":
    main()
