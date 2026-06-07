"""Live actual P&L per day from positions.jsonl — for the brain-vs-live reconciliation.

The user's reality check: backtest metrics "improved" but LIVE P&L decreased. This extracts
what the LIVE engine actually did (real entry->exit premium P&L), per day, deduped for
restarts, so we can put it head-to-head with the brain backtest's per-day numbers and see
whether the brain's selectivity/abstention would have helped or hurt on the same real days.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path


def _find_positions() -> Path | None:
    for cand in [
        os.getenv("POSITIONS_JSONL"),
        os.path.join(os.getenv("STRATEGY_RUN_DIR", "/app/.run/strategy_app"), "positions.jsonl"),
        "/app/.run/strategy_app/positions.jsonl",
        "/opt/option_trading/.run/strategy_app/positions.jsonl",
    ]:
        if cand and Path(cand).exists():
            return Path(cand)
    return None


def main() -> None:
    path = _find_positions()
    if path is None:
        print("positions.jsonl not found (set POSITIONS_JSONL or STRATEGY_RUN_DIR)")
        return
    print(f"reading {path}")

    open_ts: dict[str, str] = {}
    closes: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        run_id = str(d.get("run_id") or "")
        if run_id.lower().startswith("sim"):     # exclude ops/standalone sims
            continue
        pid = d.get("position_id", "")
        evt = d.get("event", "")
        if evt == "POSITION_OPEN" and pid and pid not in open_ts:
            open_ts[pid] = str(d.get("timestamp", ""))
        elif evt == "POSITION_CLOSE" and pid:
            closes[pid] = d

    # dedup logical trades (restarts duplicate them), group by day
    seen: set = set()
    by_day: dict[str, list[dict]] = defaultdict(list)
    for pid, p in closes.items():
        entry_ts = open_ts.get(pid, str(p.get("timestamp", "")))
        day = entry_ts[:10]
        key = (entry_ts[:16], str(p.get("direction", "")), p.get("strike"),
               round(float(p.get("entry_premium") or 0), 1))
        if key in seen:
            continue
        seen.add(key)
        by_day[day].append({
            "pnl_pct": float(p.get("pnl_pct") or 0.0),
            "mfe_pct": float(p.get("mfe_pct") or 0.0),
            "direction": p.get("direction", ""),
            "exit": str(p.get("exit_policy_triggered") or p.get("exit_reason") or ""),
            "run_id": str(p.get("run_id") or ""),
        })

    print(f"\n{'day':>12} {'n':>4} {'sum_pnl%':>9} {'avg%':>7} {'win%':>6} {'>5%':>4} {'maxMFE%':>8}")
    g_n = g_sum = 0.0
    for day in sorted(by_day):
        ts = by_day[day]
        n = len(ts)
        s = sum(t["pnl_pct"] for t in ts) * 100
        wins = sum(1 for t in ts if t["pnl_pct"] > 0)
        big = sum(1 for t in ts if t["mfe_pct"] > 0.05)
        mx = max((t["mfe_pct"] for t in ts), default=0.0) * 100
        print(f"{day:>12} {n:>4} {s:>8.2f}% {s / n if n else 0:>6.2f}% {wins / n * 100 if n else 0:>5.0f}% {big:>4} {mx:>7.1f}%")
        g_n += n
        g_sum += s
    print(f"{'TOTAL':>12} {int(g_n):>4} {g_sum:>8.2f}% {g_sum / g_n if g_n else 0:>6.2f}%")
    print("\n(pnl_pct = realised premium % per trade; same scale as the brain backtest's real-fill exit.)")


if __name__ == "__main__":
    main()
