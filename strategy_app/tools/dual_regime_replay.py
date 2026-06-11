"""Grade every RegimeDirector detector's CONDITIONED direction follow-through.

For each detector (agreement_lever, ema_cross, vwap, fade_vwap, combo) and each bar:
run RegimeDirector.decide() (the REAL code path) -> side; then check whether the
next-HZ futures move actually went that way. Reports coverage + accuracy overall
and on real (>=THR pt) moves. This is the number that decides slot-1 — NOT the
unconditional model precision.

Run inside the dashboard container (has pymongo + strategy_app + mongo):
  docker exec -e HZ=10 -e THR=70 dashboard python /tmp/dual_regime_replay.py
Env: HZ (fwd minutes, default 10), THR (pt, default 70), COLLECTION
(phase1_market_snapshots [live] or phase1_market_snapshots_historical), SLICE (a:b days).
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, "/app")
from pymongo import MongoClient

from strategy_app.brain.regime_director import RegimeDirector, _DETECTORS, ABSTAIN
from strategy_app.market.snapshot_accessor import SnapshotAccessor

HZ = int(os.getenv("HZ", "10"))
THR = float(os.getenv("THR", "70"))
COLLECTION = os.getenv("COLLECTION", "phase1_market_snapshots")
col = MongoClient("mongo", 27017)["trading_ai"][COLLECTION]
days = sorted(d for d in col.distinct("trade_date_ist") if d)
SL = os.getenv("SLICE", "")
if SL:
    a, b = SL.split(":")
    days = days[(int(a) if a else None):(int(b) if b else None)]


def fclose(s):
    fb = s.get("futures_bar") or {}
    v = fb.get("close") or fb.get("fut_close")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# detector -> {"cov":n, "hit":n, "tot":n, "bhit":n, "btot":n, "ce":n, "pe":n}
stats = {name: defaultdict(int) for name in _DETECTORS}
directors = {name: RegimeDirector(name) for name in _DETECTORS}
total_bars = 0

for d in days:
    docs = list(col.find({"trade_date_ist": d}).sort("timestamp", 1))
    snaps = [x for x in ((doc.get("payload") or {}).get("snapshot") for doc in docs) if x]
    closes = [fclose(s) for s in snaps]
    for i, s in enumerate(snaps):
        if i + HZ >= len(snaps):
            break
        c0, cF = closes[i], closes[i + HZ]
        if c0 is None or cF is None:
            continue
        total_bars += 1
        move = cF - c0
        actual = "CE" if move > 0 else "PE"
        big = abs(move) >= THR
        try:
            acc = SnapshotAccessor(s)
        except Exception:
            continue
        for name, director in directors.items():
            side = director.decide(acc).side
            if side == ABSTAIN:
                continue
            st = stats[name]
            st["cov"] += 1
            st[side.lower()] += 1
            st["tot"] += 1
            st["hit"] += (side == actual)
            if big:
                st["btot"] += 1
                st["bhit"] += (side == actual)

print(f"collection={COLLECTION} days={len(days)} HZ={HZ}min THR={THR}pt bars={total_bars}\n")
print(f"{'detector':18}{'cov%':>7}{'acc%':>7}{'n':>7}{'acc%>=THR':>11}{'n>=THR':>8}  CE/PE")
for name in sorted(stats, key=lambda k: -(stats[k]["bhit"] / max(1, stats[k]["btot"]))):
    st = stats[name]
    cov = 100.0 * st["cov"] / max(1, total_bars)
    acc = 100.0 * st["hit"] / max(1, st["tot"])
    bacc = 100.0 * st["bhit"] / max(1, st["btot"])
    print(f"{name:18}{cov:7.1f}{acc:7.1f}{st['tot']:7d}{bacc:11.1f}{st['btot']:8d}  {st['ce']}/{st['pe']}")
print("\nNOTE: acc>=THR on real moves is the slot-1 selector. >56-61% = tradeable; <50% = wrong side.")
