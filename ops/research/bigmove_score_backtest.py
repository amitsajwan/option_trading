"""BigMoveScore backtest — the validation behind docs/INTELLIGENT_BRAIN_HANDOVER.md (Phase 0).

Direction-agnostic "is a big move loading?" detector, tested on live BankNifty snapshots.
No ML, no engine — pure price/volume/OI on the futures bar + chain aggregates.

RUN: inside the strategy_app container (has pymongo + mongo on the docker network):
    sudo docker cp ops/research/bigmove_score_backtest.py <strategy_app>:/tmp/bm.py
    sudo docker exec <strategy_app> python /tmp/bm.py
Reads trading_ai.phase1_market_snapshots (full 25-strike chain, 1-min bars).

KEY RESULT (7 live days, 2026-05-26..06-05, ~2,400 bars, 10-min horizon, target 100pt):
  base rate (any bar)                          : 34% see >=100pt in 10min
  "loaded" = compression AND oi_build          : 49% (n=229, ~33/day, mean 117pt, 11%>=200pt)
  => clean ~1.5x signal. The sum-of-4 score was NOT monotonic (lone signals are noise);
     the *pair* compression+OI-build is the real phenomenon.
OPEN: "release" timing trigger (velocity AND volume same bar) never fired — too strict;
      try velocity OR volume, or a 2-3 bar window, to time entry within the loaded window.
"""
from __future__ import annotations

import os

from pymongo import MongoClient

DAYS = ["2026-05-26", "2026-05-27", "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]
HORIZON = 10          # minutes — 5-min is structurally dead (base rate of 100pt move only ~16%)
COMPRESS_RATIO = 0.70
VOL_SPIKE = 1.8
VELOCITY_K = 1.5
OI_BUILD = 1.002


def _tr(h, l, pc):
    return max(h - l, abs(h - pc), abs(l - pc))


def _atr(H, L, C):
    return sum(_tr(H[k], L[k], C[k - 1]) for k in range(1, len(H))) / max(len(H) - 1, 1)


def main() -> None:
    mc = MongoClient(f"mongodb://{os.getenv('MONGO_HOST', 'mongo')}:27017")["trading_ai"]
    quad = {"neither": [], "loaded_only": [], "released_only": [], "loaded+released": []}
    base_tot = base_hit = 0

    for day in DAYS:
        docs = list(mc.phase1_market_snapshots.find({"trade_date_ist": day}).sort("timestamp", 1))
        bars = []
        for d in docs:
            s = (d.get("payload") or {}).get("snapshot") or {}
            f = s.get("futures_bar") or {}
            ca = s.get("chain_aggregates") or {}
            bars.append({
                "c": f.get("fut_close"), "h": f.get("fut_high"), "l": f.get("fut_low"),
                "ovol": (ca.get("total_ce_volume") or 0) + (ca.get("total_pe_volume") or 0),
                "ooi": (ca.get("total_ce_oi") or 0) + (ca.get("total_pe_oi") or 0),
            })
        for i in range(len(bars)):
            b = bars[i]
            if b["c"] is None or i < 42:
                continue
            win = [x for x in bars[i + 1:i + 1 + HORIZON] if x["h"] is not None]
            if not win:
                continue
            mv = max(max(x["h"] for x in win) - b["c"], b["c"] - min(x["l"] for x in win))
            base_tot += 1
            base_hit += mv >= 100
            H = [x["h"] for x in bars[i - 15:i]]; L = [x["l"] for x in bars[i - 15:i]]; C = [x["c"] for x in bars[i - 16:i]]
            Hb = [x["h"] for x in bars[i - 41:i - 15]]; Lb = [x["l"] for x in bars[i - 41:i - 15]]; Cb = [x["c"] for x in bars[i - 42:i - 15]]
            if None in H + L + C + Hb + Lb + Cb:
                continue
            atr_build = _atr(H, L, C); atr_base = _atr(Hb, Lb, Cb)
            vol_build = sum(x["ovol"] for x in bars[i - 15:i]) / 15
            ret = abs(b["c"] - bars[i - 1]["c"])
            compression = bool(atr_base and atr_build < COMPRESS_RATIO * atr_base)
            oi_build = bool(b["ooi"] and bars[i - 15]["ooi"] and b["ooi"] > bars[i - 15]["ooi"] * OI_BUILD)
            velocity = bool(atr_build and ret > VELOCITY_K * atr_build)
            volume = bool(vol_build and b["ovol"] > VOL_SPIKE * vol_build)
            loaded = compression and oi_build
            released = velocity and volume      # TODO: too strict — loosen (OR / window)
            key = ("loaded+released" if loaded and released else
                   "loaded_only" if loaded else
                   "released_only" if released else "neither")
            quad[key].append(mv)

    print(f"BigMoveScore | {HORIZON}-min horizon | base rate >=100pt: {base_hit / base_tot * 100:.0f}% (n={base_tot})")
    print(f"{'cell':>16} {'n':>5} {'mean':>5} {'med':>4} {'%>=100':>7} {'%>=200':>7}")
    for k in ("neither", "loaded_only", "released_only", "loaded+released"):
        v = sorted(quad[k])
        if not v:
            print(f"{k:>16} {0:>5}")
            continue
        mean = sum(v) / len(v); med = v[len(v) // 2]
        h100 = sum(1 for m in v if m >= 100) / len(v) * 100
        h200 = sum(1 for m in v if m >= 200) / len(v) * 100
        print(f"{k:>16} {len(v):>5} {mean:>5.0f} {med:>4.0f} {h100:>6.0f}% {h200:>6.0f}%")


if __name__ == "__main__":
    main()
