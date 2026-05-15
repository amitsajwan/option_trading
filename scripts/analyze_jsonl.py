#!/usr/bin/env python3
"""Canonical replay analyzer — splits trades by C1's train/valid/holdout windows.

Run on the runtime VM:
    sudo python3 /home/amits/analyze_jsonl.py                  # latest run, all windows
    sudo python3 /home/amits/analyze_jsonl.py --run-id 5eb9e3d9 # specific run
    sudo python3 /home/amits/analyze_jsonl.py --list           # list run_ids
    sudo python3 /home/amits/analyze_jsonl.py --window holdout # holdout-only summary

C1 windows (per ml_pipeline_2/docs/training/MODEL_STATE_20260514.md, line 78):
  train:    2020-08-03 → 2024-04-30  (model SAW for training)
  valid:    2024-05-01 → 2024-07-31  (model saw for hyperparam tuning)
  holdout:  2024-08-01 → 2024-10-31  (truly out-of-sample)

Statistical floor: holdout < 30 trades → results are noise. Flag prominently.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

P = Path("/opt/option_trading/.run/strategy_app_historical/positions.jsonl")
COST = 0.02  # 200 bps round-trip baseline
MIN_HOLDOUT_TRADES = 30  # below this, OOS conclusions are not statistically meaningful

C1_TRAIN_END    = "2024-04-30"
C1_VALID_END    = "2024-07-31"
C1_HOLDOUT_END  = "2024-10-31"


def window_of(date_str: str) -> str:
    d = (date_str or "")[:10]
    if not d:                  return "unknown"
    if d <= C1_TRAIN_END:      return "train"
    if d <= C1_VALID_END:      return "valid"
    if d <= C1_HOLDOUT_END:    return "holdout"
    return "post-holdout"


def load_runs():
    """Group POSITION_CLOSE events by run_id. Skip POSITION_MANAGE (we only score closes)."""
    runs = defaultdict(lambda: {"closes": [], "first_line": None, "first_date": None, "last_date": None})
    if not P.exists():
        sys.exit(f"positions.jsonl not found at {P}")
    with P.open() as f:
        for i, line in enumerate(f):
            try: e = json.loads(line)
            except: continue
            run = e.get("run_id")
            if not run: continue
            if e.get("event") != "POSITION_CLOSE": continue
            info = runs[run]
            if info["first_line"] is None: info["first_line"] = i
            info["closes"].append(e)
            d = (e.get("entry_time") or e.get("timestamp") or "")[:10]
            if info["first_date"] is None or d < info["first_date"]: info["first_date"] = d
            if info["last_date"]  is None or d > info["last_date"]:  info["last_date"]  = d
    return runs


def stats_for(trades):
    if not trades: return None
    n = len(trades)
    pnl = [t.get("pnl_pct") or 0 for t in trades]
    wins = sum(1 for x in pnl if x > 0)
    gross = sum(pnl)
    w_abs = sum(x for x in pnl if x > 0)
    l_abs = sum(-x for x in pnl if x < 0)
    pf = w_abs/l_abs if l_abs > 0 else float("inf")
    avg_g = gross / n * 100
    net   = (gross - n*COST) * 100
    # MDD
    cumG = peakG = mddG = 0
    cumN = peakN = mddN = 0
    for x in pnl:
        cumG += x; peakG = max(peakG, cumG); mddG = max(mddG, peakG - cumG)
        cumN += x - COST; peakN = max(peakN, cumN); mddN = max(mddN, peakN - cumN)
    # MAE
    mae = max((abs(t.get("mae_pct") or 0) for t in trades), default=0.0)
    # smart-strike modes
    modes = defaultdict(int)
    for t in trades:
        er = t.get("entry_reason") or ""
        if "smart_strike_mode=" in er:
            modes[er.split("smart_strike_mode=", 1)[1].split()[0]] += 1
    # by direction
    by_dir = defaultdict(list)
    for t in trades:
        by_dir[t.get("direction") or "?"].append(t.get("pnl_pct") or 0)
    dir_stats = {}
    for d, arr in by_dir.items():
        if not arr: continue
        g = sum(arr); w = sum(1 for x in arr if x > 0)
        dir_stats[d] = {"n": len(arr), "win_pct": w/len(arr)*100, "gross_pct": g*100, "net_pct": (g-len(arr)*COST)*100}
    return {
        "n": n, "wins": wins, "win_rate_pct": wins/n*100,
        "gross_sum_pct": gross*100, "net_at_200bps_pct": net,
        "avg_gross_per_trade_pct": avg_g,
        "pf_gross": pf,
        "mdd_gross_pct": mddG*100, "mdd_net_pct": mddN*100,
        "max_mae_pct": mae*100,
        "modes": dict(modes), "by_direction": dir_stats,
    }


def render(name, s, *, warn_if_under=None):
    if s is None:
        print(f"  {name:18s}  (no trades)")
        return
    warn = ""
    if warn_if_under is not None and s["n"] < warn_if_under:
        warn = f"  ⚠ SAMPLE TOO SMALL ({s['n']}<{warn_if_under}) — treat as directional only"
    print(f"  {name:14s}  n={s['n']:4d}  avg_gross={s['avg_gross_per_trade_pct']:+6.2f}%  "
          f"net@200bps={s['net_at_200bps_pct']:+8.2f}%  PF={s['pf_gross']:5.2f}  "
          f"win={s['win_rate_pct']:5.1f}%  mae_max={s['max_mae_pct']:5.1f}%{warn}")


def verdict_holdout(s):
    if s is None or s["n"] == 0:
        return "NO HOLDOUT TRADES — cannot assess OOS"
    pass_avg = s["avg_gross_per_trade_pct"] >= 2.0
    pass_net = s["net_at_200bps_pct"] > 0
    pass_n   = s["n"] >= MIN_HOLDOUT_TRADES
    if pass_avg and pass_net and pass_n:
        return f"✅ HOLDOUT shows REAL OOS edge ({s['n']} trades, {s['net_at_200bps_pct']:+.1f}% net)"
    if pass_avg and pass_net and not pass_n:
        return f"⚠ HOLDOUT marginally positive but UNDERSAMPLED ({s['n']}<{MIN_HOLDOUT_TRADES}) — need more data"
    if not pass_net and pass_n:
        return f"❌ HOLDOUT NET NEGATIVE ({s['net_at_200bps_pct']:+.1f}%) — strong overfit signal"
    if not pass_net and not pass_n:
        return f"❌ HOLDOUT NET NEGATIVE AND UNDERSAMPLED ({s['n']} trades, {s['net_at_200bps_pct']:+.1f}%) — directional negative, needs more data"
    return "MIXED — see numbers"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None, help="prefix or full run_id (default: most recent)")
    ap.add_argument("--list", action="store_true", help="list all run_ids and exit")
    ap.add_argument("--window", default=None, choices=["train", "valid", "holdout", "all"],
                    help="restrict reporting to a specific window")
    args = ap.parse_args()

    runs = load_runs()
    if not runs:
        sys.exit("No POSITION_CLOSE events in positions.jsonl")

    if args.list:
        print(f"{len(runs)} run_ids found:")
        for run, info in sorted(runs.items(), key=lambda x: x[1]["first_line"] or 0):
            print(f"  {run[:8]}..  closes={len(info['closes']):4d}  {info['first_date']} → {info['last_date']}")
        return

    # Select run
    if args.run_id:
        cand = [r for r in runs if r.startswith(args.run_id)]
        if not cand:
            sys.exit(f"No run_id matching '{args.run_id}'")
        if len(cand) > 1:
            sys.exit(f"Multiple run_ids match '{args.run_id}': {cand}")
        run = cand[0]
    else:
        run = max(runs, key=lambda r: runs[r]["first_line"] or 0)

    info = runs[run]
    print("="*100)
    print(f"REPLAY ANALYSIS — run_id {run}")
    print(f"Date range: {info['first_date']} → {info['last_date']}   total closes: {len(info['closes'])}")
    print("="*100)
    print()
    print(f"C1 model training windows (per MODEL_STATE_20260514.md):")
    print(f"  train:   2020-08-03 → {C1_TRAIN_END}   (CONTAMINATED — model SAW for training)")
    print(f"  valid:   2024-05-01 → {C1_VALID_END}   (lightly contaminated — hyperparam tuning)")
    print(f"  holdout: 2024-08-01 → {C1_HOLDOUT_END}   (CLEAN OOS — never seen)")
    print()

    # Bucket trades
    buckets = defaultdict(list)
    for c in info["closes"]:
        w = window_of(c.get("entry_time") or c.get("timestamp"))
        buckets[w].append(c)

    print("PER-WINDOW SUMMARY:")
    print()
    print(f"  {'window':14s}  {'samples':>20s}  {'economics':>30s}")
    print(f"  {'-'*14}  {'-'*60}")
    targets = ["train", "valid", "holdout", "post-holdout"] if args.window in (None, "all") else [args.window]
    for w in targets:
        s = stats_for(buckets[w])
        warn = MIN_HOLDOUT_TRADES if w == "holdout" else None
        render(w, s, warn_if_under=warn)
    print()

    all_s = stats_for(info["closes"])
    render("OVERALL", all_s)
    print()

    # Smart-strike + direction breakdown for overall
    if all_s and (all_s["modes"] or all_s["by_direction"]):
        print("SMART-STRIKE MODE MIX (overall):")
        for m, c in sorted(all_s["modes"].items(), key=lambda x: -x[1]):
            print(f"  {m:18s}  {c}")
        print()
        print("BY DIRECTION (overall):")
        for d, ds in sorted(all_s["by_direction"].items()):
            print(f"  {d}: n={ds['n']:3d}  win={ds['win_pct']:5.1f}%  gross={ds['gross_pct']:+7.2f}%  net@200bps={ds['net_pct']:+7.2f}%")
        print()

    print("="*100)
    print("VERDICT")
    print("="*100)
    holdout_s = stats_for(buckets["holdout"])
    print(f"  {verdict_holdout(holdout_s)}")
    if all_s and holdout_s:
        contam_share = (all_s["gross_sum_pct"] - (stats_for(buckets["holdout"])["gross_sum_pct"] if holdout_s else 0)) / max(abs(all_s["gross_sum_pct"]), 1e-9) * 100
        train_s = stats_for(buckets["train"])
        if train_s:
            train_share = train_s["gross_sum_pct"] / max(abs(all_s["gross_sum_pct"]), 1e-9) * 100
            print(f"  Training-window contribution: {train_share:+.1f}% of total gross")
    print()


if __name__ == "__main__":
    main()
