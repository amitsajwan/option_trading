#!/usr/bin/env python3
"""
Comparison: CLEAN run (46efdc16, stop=20%, trailing=35% activation, prob≥0.65)
vs dir_fix (5a8b6684, trailing=12% activation, same stop/prob).
Isolates the impact of the trailing activation fix (5133479) + config pipeline fix (83bad06).

CLEAN run: first fully correct replay — all strategies get stop=20%/target=70%/trailing=35%.
Prior runs (9b17c897, 514900d7) were contaminated by the all-null risk_config bug.
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
from statistics import mean, median, stdev

try:
    from pymongo import MongoClient
except ImportError:
    print("pymongo not available"); sys.exit(1)

MONGO_URL = os.getenv("MONGO_URL", "mongodb://mongo:27017")
DB_NAME   = os.getenv("MONGO_DB", "trading_ai")

STOP_FIX_RUN = "46efdc16-a21c-4e81-9f1a-e6e4ac362c76"   # CLEAN: stop=20% trailing=35% prob≥0.65 (83bad06 fix)
DIR_FIX_RUN  = "5a8b6684-3cb4-4788-b35e-eb3c3816b4a8"   # trailing=12%, stop=20% (prior)
OLD_RUN      = "9e3789a3-deb5-4dcd-ba8a-9a646a1033bd"   # stop=40% bug, prob>=0.65

LOT_SIZE = 15
CAPITAL  = 100_000

client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
db     = client[DB_NAME]


def load_trades(run_id: str) -> list[dict]:
    rows = []
    for doc in db.strategy_positions_historical.find(
        {"run_id": run_id, "event": "POSITION_CLOSE"},
        {"_id": 0}
    ):
        pnl_pct    = float(doc.get("pnl_pct") or 0)
        entry_prem = float(doc.get("entry_premium") or 0)
        exit_prem  = float(doc.get("exit_premium") or 0)
        lots       = max(1, int(doc.get("lots") or 1))
        stop_pct   = float(doc.get("stop_loss_pct") or 0)
        target_pct = float(doc.get("target_pct") or 0)
        mfe        = float(doc.get("mfe_pct") or 0)
        mae        = float(doc.get("mae_pct") or 0)
        direction  = str(doc.get("direction") or "")
        exit_rsn   = str(doc.get("exit_reason") or "")
        strat      = str(doc.get("entry_strategy") or "")
        date       = str(doc.get("trade_date_ist") or "")
        ml_prob    = doc.get("ml_entry_prob")
        ml_prob    = float(ml_prob) if ml_prob is not None else None

        cap_at_risk = entry_prem * lots * LOT_SIZE
        cap_pnl_amt = pnl_pct * cap_at_risk
        cap_pnl_pct = cap_pnl_amt / CAPITAL if CAPITAL > 0 else 0

        rows.append({
            "date": date, "direction": direction, "exit_reason": exit_rsn,
            "strategy": strat, "pnl_pct": pnl_pct, "mfe": mfe, "mae": mae,
            "stop_pct": stop_pct, "target_pct": target_pct,
            "entry_prem": entry_prem, "exit_prem": exit_prem,
            "lots": lots, "cap_at_risk": cap_at_risk,
            "cap_pnl_pct": cap_pnl_pct, "ml_prob": ml_prob,
        })
    return rows


def pct(v):  return f"{v*100:+.1f}%"
def pcts(v): return f"{v*100:.1f}%"


def summarize(label: str, rows: list[dict]) -> None:
    if not rows:
        print(f"\n{'='*70}\n  {label}: NO TRADES\n{'='*70}"); return

    pnls   = [r["pnl_pct"] for r in rows]
    cpnls  = [r["cap_pnl_pct"] for r in rows]
    wins   = [r for r in rows if r["pnl_pct"] > 0]
    losers = [r for r in rows if r["pnl_pct"] <= 0]
    n      = len(rows)
    gwin   = sum(r["cap_pnl_pct"] for r in wins)
    gloss  = abs(sum(r["cap_pnl_pct"] for r in losers))
    pf     = gwin / gloss if gloss > 0 else float("inf")

    # stop config sanity check
    stops_seen = sorted(set(round(r["stop_pct"] * 100) for r in rows if r["stop_pct"] > 0))
    tgts_seen  = sorted(set(round(r["target_pct"] * 100) for r in rows if r["target_pct"] > 0))

    print(f"\n{'='*70}")
    print(f"  {label}  ({n} trades)")
    print(f"{'='*70}")
    print(f"  Config seen   : stop={stops_seen}%  target={tgts_seen}%")
    print(f"  Win rate      : {len(wins)}/{n} = {len(wins)/n*100:.0f}%")
    print(f"  Avg opt PnL   : {pct(mean(pnls))}   median {pct(median(pnls))}")
    if n > 1:
        print(f"  Std dev       : {pcts(stdev(pnls))}")
    print(f"  Avg cap PnL   : {pct(mean(cpnls))}   total {pct(sum(cpnls))}  (base ₹{CAPITAL:,})")
    print(f"  Profit factor : {pf:.2f}")
    print(f"  Best / worst  : {pct(max(pnls))} / {pct(min(pnls))}  (option %)")
    print(f"  Avg MFE/MAE   : {pct(mean(r['mfe'] for r in rows))} / {pct(mean(r['mae'] for r in rows))}")

    # Top-5 stripped net (outlier survival test)
    sorted_by_cap = sorted(rows, key=lambda r: r["cap_pnl_pct"], reverse=True)
    top5_stripped = sum(r["cap_pnl_pct"] for r in sorted_by_cap[5:])
    print(f"  Net w/o top-5 : {pct(top5_stripped)}  (outlier survival)")

    print(f"\n  ── Exit reasons ──")
    by_exit: dict[str, list] = defaultdict(list)
    for r in rows:
        by_exit[r["exit_reason"]].append(r["pnl_pct"])
    for reason, ps in sorted(by_exit.items(), key=lambda x: -len(x[1])):
        avg = mean(ps)
        wr  = sum(1 for p in ps if p > 0) / len(ps)
        print(f"    {reason:<25} n={len(ps):2d}  wr={wr*100:.0f}%  avg={pct(avg)}")

    print(f"\n  ── Entry strategy ──")
    by_strat: dict[str, list] = defaultdict(list)
    for r in rows:
        by_strat[r["strategy"]].append(r["pnl_pct"])
    for strat, ps in sorted(by_strat.items(), key=lambda x: -sum(x[1]), reverse=False):
        wr   = sum(1 for p in ps if p > 0) / len(ps)
        wins_cap = sum(r["cap_pnl_pct"] for r in rows if r["strategy"] == strat and r["pnl_pct"] > 0)
        loss_cap = sum(r["cap_pnl_pct"] for r in rows if r["strategy"] == strat and r["pnl_pct"] <= 0)
        spf  = wins_cap / abs(loss_cap) if loss_cap < 0 else float("inf")
        print(f"    {strat:<28} n={len(ps):2d}  wr={wr*100:.0f}%  avg={pct(mean(ps))}  PF={spf:.2f}")

    print(f"\n  ── Direction split ──")
    for d in ["CE", "PE"]:
        dr = [r for r in rows if r["direction"] == d]
        if dr:
            dp = [r["pnl_pct"] for r in dr]
            dw = sum(1 for p in dp if p > 0)
            print(f"    {d}: n={len(dr):2d}  wr={dw/len(dr)*100:.0f}%  avg={pct(mean(dp))}  total={pct(sum(dp))}")

    # ML probability bands — DET_DIRECTION only
    ml_trades = [r for r in rows if r["ml_prob"] is not None and r["ml_prob"] > 0]
    if ml_trades:
        print(f"\n  ── ML probability bands (DET_DIRECTION, n={len(ml_trades)}) ──")
        bands = [
            ("0.65–0.68", 0.65, 0.68),
            ("0.68–0.72", 0.68, 0.72),
            ("0.72–0.76", 0.72, 0.76),
            ("0.76+    ", 0.76, 1.01),
        ]
        for blabel, lo, hi in bands:
            br = [r for r in ml_trades if lo <= r["ml_prob"] < hi]
            if br:
                bp    = [r["pnl_pct"] for r in br]
                bw    = sum(1 for p in bp if p > 0)
                bstop = sum(1 for r in br if "STOP" in r["exit_reason"])
                print(f"    {blabel}  n={len(br):2d}  wr={bw/len(br)*100:.0f}%  avg={pct(mean(bp))}  stops={bstop}")

    print(f"\n  ── Monthly ──")
    by_month: dict[str, list] = defaultdict(list)
    for r in rows:
        by_month[r["date"][:7]].append(r["pnl_pct"])
    for month in sorted(by_month):
        mp = by_month[month]
        mw = sum(1 for p in mp if p > 0)
        mc = sum(r["cap_pnl_pct"] for r in rows if r["date"][:7] == month)
        print(f"    {month}  n={len(mp):2d}  wr={mw/len(mp)*100:.0f}%  avg={pct(mean(mp))}  total_opt={pct(sum(mp))}  cap={pct(mc)}")

    print(f"\n  ── Per-trade detail ──")
    print(f"  {'Date':<12} {'Strategy':<28} {'D':<3} {'Prob':>5} {'Opt%':>7} {'Cap%':>7} "
          f"{'MFE':>6} {'MAE':>6} {'Stp':>5} {'Tgt':>5} {'Prem':>6} L  Exit")
    print("  " + "-"*120)
    for r in sorted(rows, key=lambda x: x["date"]):
        prob_s = f"{r['ml_prob']:.3f}" if r['ml_prob'] is not None else "  —  "
        print(
            f"  {r['date']:<12} {r['strategy']:<28} {r['direction']:<3} "
            f"{prob_s:>5} "
            f"{pct(r['pnl_pct']):>7} "
            f"{pct(r['cap_pnl_pct']):>7} "
            f"{pct(r['mfe']):>6} "
            f"{pct(r['mae']):>6} "
            f"{r['stop_pct']*100:>4.0f}% "
            f"{r['target_pct']*100:>4.0f}% "
            f"{r['entry_prem']:>6.0f} "
            f"{r['lots']}  {r['exit_reason']}"
        )


# ── run ───────────────────────────────────────────────────────────────────────
print("\n" + "█"*70)
print("  TRAILING-FIX ANALYSIS  (clean run: stop=20% trailing=35% vs prior runs)")
print("  All runs: prob>=0.65, profile trader_master_ml_entry_det_dir_v1")
print("█"*70)

stop_fix_trades = load_trades(STOP_FIX_RUN)
dir_fix_trades  = load_trades(DIR_FIX_RUN)
old_trades      = load_trades(OLD_RUN)

print(f"\nTrade counts:  stop_fix={len(stop_fix_trades)}  dir_fix={len(dir_fix_trades)}  old={len(old_trades)}")
print("(stop_fix may still be running if < dir_fix count)")

summarize("CLEAN     prob≥0.65, stop=20%, trailing=35% activation (correct)", stop_fix_trades)
summarize("DIR_FIX   prob≥0.65, stop=20%, trailing=12% activation (prior)", dir_fix_trades)
summarize("OLD       prob≥0.55, stop=40% bug, trailing=12%",                old_trades)

# ── head-to-head summary ──────────────────────────────────────────────────────
print(f"\n{'='*70}")
print("  HEAD-TO-HEAD SUMMARY")
print(f"{'='*70}")
for label, trades in [
    ("CLEAN   (20% stp, 35% trail)", stop_fix_trades),
    ("DIR_FIX (20% stp, 12% trail)", dir_fix_trades),
    ("OLD     (55% thr, 40% stop) ", old_trades),
]:
    if not trades:
        print(f"  {label}: no trades"); continue
    pnls  = [r["pnl_pct"] for r in trades]
    cpnls = [r["cap_pnl_pct"] for r in trades]
    wins  = [r for r in trades if r["pnl_pct"] > 0]
    stops = [r for r in trades if "STOP" in r["exit_reason"]]
    tgts  = [r for r in trades if r["exit_reason"] == "TARGET_HIT"]
    time_s= [r for r in trades if r["exit_reason"] == "TIME_STOP"]
    n = len(trades)
    gloss = abs(sum(r["cap_pnl_pct"] for r in trades if r["pnl_pct"] <= 0))
    gwin  = sum(r["cap_pnl_pct"] for r in trades if r["pnl_pct"] > 0)
    pf    = gwin / gloss if gloss > 0 else float("inf")
    print(f"\n  {label} ({n} trades):")
    print(f"    Win rate      {len(wins)/n*100:.0f}%  ({len(wins)}W / {n-len(wins)}L)")
    print(f"    Avg opt PnL   {pct(mean(pnls))}")
    print(f"    Total cap PnL {pct(sum(cpnls))}")
    print(f"    Profit factor {pf:.2f}")
    print(f"    STOP exits    {len(stops)}/{n} ({len(stops)/n*100:.0f}%)  "
          f"avg {pct(mean(r['pnl_pct'] for r in stops)) if stops else 'n/a'}")
    print(f"    TARGET exits  {len(tgts)}/{n} ({len(tgts)/n*100:.0f}%)  "
          f"avg {pct(mean(r['pnl_pct'] for r in tgts)) if tgts else 'n/a'}")
    print(f"    TIME exits    {len(time_s)}/{n} ({len(time_s)/n*100:.0f}%)  "
          f"avg {pct(mean(r['pnl_pct'] for r in time_s)) if time_s else 'n/a'}")

# ── DET_DIRECTION isolation ────────────────────────────────────────────────────
print(f"\n{'='*70}")
print("  DET_DIRECTION ISOLATION  (ML-gated trades only)")
print(f"{'='*70}")
for label, trades in [
    ("STOP_FIX", stop_fix_trades),
    ("DIR_FIX ", dir_fix_trades),
]:
    det = [r for r in trades if r["strategy"] == "DET_DIRECTION"]
    if not det:
        print(f"  {label}: no DET_DIRECTION trades"); continue
    pnls = [r["pnl_pct"] for r in det]
    wins = [r for r in det if r["pnl_pct"] > 0]
    stops= [r for r in det if "STOP" in r["exit_reason"]]
    tgts = [r for r in det if r["exit_reason"] == "TARGET_HIT"]
    gloss = abs(sum(r["pnl_pct"] for r in det if r["pnl_pct"] <= 0))
    gwin  = sum(r["pnl_pct"] for r in det if r["pnl_pct"] > 0)
    pf    = gwin / gloss if gloss > 0 else float("inf")
    print(f"\n  {label} DET_DIRECTION ({len(det)} trades):")
    print(f"    WR={len(wins)/len(det)*100:.0f}%  avg={pct(mean(pnls))}  total={pct(sum(pnls))}  PF={pf:.2f}")
    print(f"    stops={len(stops)} avg={pct(mean(r['pnl_pct'] for r in stops)) if stops else 'n/a'}  "
          f"targets={len(tgts)} avg={pct(mean(r['pnl_pct'] for r in tgts)) if tgts else 'n/a'}")
