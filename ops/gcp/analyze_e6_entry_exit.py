#!/usr/bin/env python3
"""Deep-dive on the E6 Aug-Oct replay:

1. Did we get sufficient entries (per-day vote vs trade conversion)?
2. Were entries blocked later (decision_reason_code, risk halt, slot competition)?
3. Were exits leaving profit on the table (MFE vs realized PnL)?
4. Are we exiting when direction is reversing (exit_reason vs forward move)?
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from statistics import median

POS = "/opt/option_trading/.run/strategy_app_historical/positions.jsonl"
VOTES = "/opt/option_trading/.run/strategy_app_historical/votes.jsonl"
RID = "2e77b80b-b6bf-4c5e-bdb9-5291c50e32b9"


def main() -> int:
    # Load all votes for this run
    votes = []
    with open(VOTES) as f:
        for line in f:
            e = json.loads(line)
            if e.get("run_id") == RID:
                votes.append(e)

    # Load positions
    opens = []
    closes = []
    with open(POS) as f:
        for line in f:
            e = json.loads(line)
            if e.get("event") == "POSITION_OPEN":
                opens.append(e)
            elif e.get("event") == "POSITION_CLOSE":
                closes.append(e)

    # ---- 1. ENTRY SUFFICIENCY ----
    print("=" * 80)
    print("(1) ENTRY SUFFICIENCY")
    print("=" * 80)

    entry_votes_by_day = defaultdict(lambda: defaultdict(int))
    for v in votes:
        if v.get("signal_type") == "ENTRY":
            td = v.get("trade_date")
            entry_votes_by_day[td][v.get("strategy", "?")] += 1

    trades_by_day = defaultdict(int)
    for c in closes:
        td = (c.get("entry_time") or c.get("timestamp") or "")[:10]
        trades_by_day[td] += 1

    total_days = len(entry_votes_by_day)
    total_entry_votes = sum(sum(d.values()) for d in entry_votes_by_day.values())
    total_ml_entry_votes = sum(d.get("ML_ENTRY", 0) for d in entry_votes_by_day.values())
    total_trades = len(closes)
    days_with_trades = sum(1 for n in trades_by_day.values() if n > 0)

    print(f"trading days observed: {total_days}")
    print(f"days with >=1 trade:   {days_with_trades} ({days_with_trades/total_days*100:.0f}%)")
    print(f"total ENTRY votes:     {total_entry_votes}")
    print(f"  ML_ENTRY votes:      {total_ml_entry_votes}")
    print(f"  other strategy:      {total_entry_votes - total_ml_entry_votes}")
    print(f"total trades opened:   {total_trades}")
    print(f"vote -> trade rate:    {total_trades/total_entry_votes*100:.2f}%")
    print(f"trades/day average:    {total_trades/total_days:.2f}")
    print(f"trades/day median:     {median(trades_by_day.values()) if trades_by_day else 0:.1f}")
    print(f"trades/day max:        {max(trades_by_day.values()) if trades_by_day else 0}")
    print()
    print("Days with 0 trades but ML_ENTRY voted ENTRY:")
    zero_trade_days = [d for d in entry_votes_by_day if trades_by_day.get(d, 0) == 0 and entry_votes_by_day[d].get("ML_ENTRY", 0) > 0]
    print(f"  count: {len(zero_trade_days)} / {total_days}")
    if zero_trade_days[:5]:
        for d in sorted(zero_trade_days)[:5]:
            print(f"    {d}: ML_ENTRY voted {entry_votes_by_day[d]['ML_ENTRY']}x — 0 trades")

    # ---- 2. WERE ENTRIES BLOCKED LATER? ----
    print()
    print("=" * 80)
    print("(2) WHY DIDN'T VOTES BECOME TRADES?")
    print("=" * 80)

    decision_codes = Counter()
    for v in votes:
        if v.get("signal_type") == "ENTRY":
            decision_codes[(v.get("strategy", "?"), v.get("decision_reason_code") or "none")] += 1

    print("decision_reason_code by strategy (ENTRY votes only):")
    for (s, rc), n in sorted(decision_codes.items(), key=lambda x: -x[1])[:15]:
        print(f"  {s:25s} {rc:20s} {n}")

    # ML_ENTRY conversion specifically: 1335 votes -> 17 trades. Where did 1318 go?
    ml_votes_with_outcomes = Counter()
    snap_to_position = {(c.get("entry_snapshot_id")): c for c in closes}
    for v in votes:
        if v.get("strategy") != "ML_ENTRY" or v.get("signal_type") != "ENTRY":
            continue
        sid = v.get("snapshot_id")
        if sid in snap_to_position:
            ml_votes_with_outcomes["became_trade"] += 1
        else:
            ml_votes_with_outcomes["no_trade_this_snapshot"] += 1
    print()
    print(f"ML_ENTRY ENTRY votes (n={total_ml_entry_votes}):")
    for k, v in ml_votes_with_outcomes.most_common():
        print(f"  {k}: {v}")
    # Likely reason: a position is ALREADY OPEN when ML_ENTRY votes; or the
    # arbiter picked a same-snapshot rival.

    # Count ML_ENTRY votes that fired while a position was already open
    open_intervals = []  # (open_ts, close_ts)
    for o in opens:
        oid = o.get("position_id") or o.get("id")
        ot = o.get("timestamp")
        # find matching close
        for c in closes:
            cid = c.get("position_id") or c.get("id")
            if cid == oid:
                open_intervals.append((ot, c.get("timestamp")))
                break

    def is_position_open_at(ts: str) -> bool:
        return any(open_t <= ts <= close_t for open_t, close_t in open_intervals if open_t and close_t)

    ml_during_open = 0
    ml_total = 0
    for v in votes:
        if v.get("strategy") == "ML_ENTRY" and v.get("signal_type") == "ENTRY":
            ml_total += 1
            ts = v.get("timestamp")
            if ts and is_position_open_at(ts):
                ml_during_open += 1
    print(f"  ML_ENTRY votes that fired while a position was already open: {ml_during_open}/{ml_total} ({ml_during_open/ml_total*100:.1f}%)")
    print(f"  → these were blocked by the 'one position at a time' constraint, not by policy.")

    # ---- 3. EXIT QUALITY: MFE vs REALIZED PNL ----
    print()
    print("=" * 80)
    print("(3) DID WE MISS PROFITS / TARGETS?")
    print("=" * 80)

    exit_reasons = Counter(c.get("exit_reason", "?") for c in closes)
    print("exit reasons:")
    for r, n in exit_reasons.most_common():
        print(f"  {r}: {n}")

    print()
    print("MFE (max favorable excursion) vs realized PnL per trade:")
    print(f"{'pnl%':>8} {'mfe%':>8} {'mae%':>8} {'left%':>8} {'reason':>15} {'bars':>5}")
    gap_total = 0.0
    missed_target_count = 0
    for c in sorted(closes, key=lambda x: float(x.get("pnl_pct", 0))):
        pnl = float(c.get("pnl_pct", 0)) * 100
        mfe = float(c.get("mfe_pct", 0)) * 100
        mae = float(c.get("mae_pct", 0)) * 100
        left = mfe - pnl  # how much profit we left on the table
        gap_total += max(0, left)
        if mfe >= 15.0 and pnl < mfe * 0.5:
            missed_target_count += 1
        reason = c.get("exit_reason", "?")[:14]
        bars = c.get("bars_held", 0)
        print(f"{pnl:>7.2f}% {mfe:>7.2f}% {mae:>7.2f}% {left:>7.2f}% {reason:>15s} {bars:>5}")

    print()
    print(f"total profit left on table (sum of MFE-PnL gaps): {gap_total:.2f}%")
    print(f"trades where MFE >= 15% but exited at <50% of MFE: {missed_target_count}")
    print()
    print("Trades that hit MFE >= 20% (clear winners that may have given back gains):")
    for c in closes:
        mfe = float(c.get("mfe_pct", 0)) * 100
        pnl = float(c.get("pnl_pct", 0)) * 100
        if mfe >= 20:
            print(f"  {c.get('entry_time', '')[:16]} {c.get('direction')}@{c.get('strike')}  MFE=+{mfe:.1f}% -> exit={pnl:+.1f}%  ({c.get('exit_reason')})  bars={c.get('bars_held')}")

    # ---- 4. EXITING ON REVERSALS? ----
    print()
    print("=" * 80)
    print("(4) ARE WE EXITING WHEN DIRECTION REVERSES?")
    print("=" * 80)
    # Group exits by reason and compute average pnl
    print(f"{'exit_reason':>15} {'n':>4} {'avg_pnl%':>10} {'avg_mfe%':>10} {'avg_mae%':>10} {'avg_bars':>10}")
    by_reason = defaultdict(list)
    for c in closes:
        by_reason[c.get("exit_reason", "?")].append(c)
    for r, group in by_reason.items():
        n = len(group)
        avg_pnl = sum(float(g.get("pnl_pct", 0)) for g in group) / n * 100
        avg_mfe = sum(float(g.get("mfe_pct", 0)) for g in group) / n * 100
        avg_mae = sum(float(g.get("mae_pct", 0)) for g in group) / n * 100
        avg_bars = sum(int(g.get("bars_held", 0)) for g in group) / n
        print(f"  {r:>15s} {n:>4} {avg_pnl:>9.2f}% {avg_mfe:>9.2f}% {avg_mae:>9.2f}% {avg_bars:>10.1f}")

    print()
    print("Interpretation guide:")
    print("  TIME_STOP at +pnl with mfe>>pnl → ran into max_hold_bars while still in winner; trailing didn't catch")
    print("  TIME_STOP at -pnl with mae<<pnl → held losing trade to time limit (no stop hit)")
    print("  STOP_LOSS at the stop level    → planned exit on adverse move (good)")
    print("  TRAILING_STOP at +pnl          → trailing did its job")
    print("  STRATEGY_EXIT                  → external signal (regime change, opposing strategy)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
