#!/usr/bin/env python3
"""OOS validation analysis for trader_master_ml_entry_det_dir_v1 (ML_ENTRY primary voter)."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections import Counter, defaultdict
from statistics import mean, median, stdev

try:
    from pymongo import MongoClient
except ImportError:
    print("pymongo required", file=sys.stderr)
    sys.exit(2)

MONGO_URL = os.getenv("MONGO_URL", "mongodb://mongo:27017")
DB_NAME = os.getenv("MONGO_DB", "trading_ai")
LOT_SIZE = 15
CAPITAL = 100_000

MIN_TRADES = int(os.getenv("OOS_MIN_TRADES", "40"))
MIN_PF = float(os.getenv("OOS_MIN_PF", "1.30"))
MIN_LEG_PF = float(os.getenv("OOS_MIN_LEG_PF", "1.00"))


def pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def pcts(v: float) -> str:
    return f"{v * 100:.1f}%"


def profit_factor(rows: list[dict], *, key: str = "cap_pnl_pct") -> float:
    wins = sum(r[key] for r in rows if r["pnl_pct"] > 0)
    loss = abs(sum(r[key] for r in rows if r["pnl_pct"] <= 0))
    return wins / loss if loss > 0 else float("inf")


def load_trades(db, run_id: str) -> list[dict]:
    rows: list[dict] = []
    for doc in db.strategy_positions_historical.find(
        {"run_id": run_id, "event": "POSITION_CLOSE"},
        {"_id": 0},
    ):
        pnl_pct = float(doc.get("pnl_pct") or 0)
        entry_prem = float(doc.get("entry_premium") or 0)
        lots = max(1, int(doc.get("lots") or 1))
        stop_pct = float(doc.get("stop_loss_pct") or 0)
        direction = str(doc.get("direction") or "")
        exit_rsn = str(doc.get("exit_reason") or "")
        strat = str(doc.get("entry_strategy") or "")
        date = str(doc.get("trade_date_ist") or "")
        ml_prob = doc.get("ml_entry_prob")
        ml_prob = float(ml_prob) if ml_prob is not None else None
        cap_at_risk = entry_prem * lots * LOT_SIZE
        cap_pnl_pct = (pnl_pct * cap_at_risk) / CAPITAL if CAPITAL > 0 else 0.0
        rows.append(
            {
                "date": date,
                "direction": direction,
                "exit_reason": exit_rsn,
                "strategy": strat,
                "pnl_pct": pnl_pct,
                "stop_pct": stop_pct,
                "cap_pnl_pct": cap_pnl_pct,
                "ml_prob": ml_prob,
            }
        )
    return rows


def blocker_histogram(db, run_id: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    coll = db.strategy_decision_traces_historical
    for doc in coll.find({"run_id": run_id}, {"final_outcome": 1, "primary_blocker_gate": 1}):
        outcome = str(doc.get("final_outcome") or "").strip().lower()
        if outcome != "blocked":
            continue
        gate = doc.get("primary_blocker_gate")
        if isinstance(gate, dict):
            label = str(gate.get("gate_id") or gate.get("reason_code") or "unknown")
        else:
            label = str(gate or "unknown")
        counts[label] += 1
    return counts


def vote_funnel(db, run_id: str) -> dict[str, int]:
    votes = db.strategy_votes_historical.count_documents(
        {"run_id": run_id, "strategy": "ML_ENTRY", "signal_type": "ENTRY"}
    )
    closes = db.strategy_positions_historical.count_documents(
        {"run_id": run_id, "event": "POSITION_CLOSE"}
    )
    above65 = db.strategy_votes_historical.count_documents(
        {
            "run_id": run_id,
            "strategy": "ML_ENTRY",
            "signal_type": "ENTRY",
            "confidence": {"$gte": 0.65},
        }
    )
    return {"ml_entry_votes": votes, "votes_ge_065": above65, "closed_trades": closes}


def latest_run_id() -> str:
    with urllib.request.urlopen(
        "http://127.0.0.1:8008/api/strategy/evaluation/runs/latest?dataset=historical",
        timeout=15,
    ) as resp:
        data = json.loads(resp.read().decode())
    run = data if isinstance(data, dict) and data.get("run_id") else data.get("run") or data
    rid = str(run.get("run_id") or "").strip()
    if not rid:
        raise SystemExit("Could not resolve latest historical run_id")
    return rid


def leg_pf(rows: list[dict], direction: str) -> float | None:
    leg = [r for r in rows if r["direction"] == direction]
    if not leg:
        return None
    return profit_factor(leg)


def evaluate_gates(rows: list[dict]) -> list[tuple[str, bool, str]]:
    n = len(rows)
    pf = profit_factor(rows) if rows else 0.0
    ce_pf = leg_pf(rows, "CE")
    pe_pf = leg_pf(rows, "PE")
    stops = sorted({round(r["stop_pct"] * 100) for r in rows if r["stop_pct"] > 0})
    stop_ok = not stops or stops == [20] or (20 in stops and max(stops) <= 25)

    checks: list[tuple[str, bool, str]] = [
        ("trades >= {}".format(MIN_TRADES), n >= MIN_TRADES, f"{n}"),
        ("portfolio PF >= {}".format(MIN_PF), pf >= MIN_PF, f"{pf:.2f}"),
        (
            "CE leg PF >= {}".format(MIN_LEG_PF),
            ce_pf is not None and ce_pf >= MIN_LEG_PF,
            "n/a" if ce_pf is None else f"{ce_pf:.2f}",
        ),
        (
            "PE leg PF >= {}".format(MIN_LEG_PF),
            pe_pf is not None and pe_pf >= MIN_LEG_PF,
            "n/a" if pe_pf is None else f"{pe_pf:.2f}",
        ),
        ("stop config ~20%", stop_ok, str(stops) + "%" if stops else "no stops seen"),
    ]
    return checks


def summarize(label: str, run_id: str, rows: list[dict], db) -> bool:
    print("\n" + "=" * 72)
    print(f"  OOS VALIDATION — {label}")
    print(f"  run_id: {run_id}")
    print("=" * 72)

    if not rows:
        print("\n  NO TRADES")
        checks = evaluate_gates(rows)
        print("\n  ── Pass / fail ──")
        all_ok = True
        for name, ok, detail in checks:
            mark = "PASS" if ok else "FAIL"
            if not ok:
                all_ok = False
            print(f"    [{mark}] {name:<28}  ({detail})")
        return all_ok

    pnls = [r["pnl_pct"] for r in rows]
    cpnls = [r["cap_pnl_pct"] for r in rows]
    wins = [r for r in rows if r["pnl_pct"] > 0]
    n = len(rows)
    pf = profit_factor(rows)

    print(f"\n  Trades        : {n}")
    print(f"  Win rate      : {len(wins)/n*100:.0f}%")
    print(f"  Avg opt PnL   : {pct(mean(pnls))}   median {pct(median(pnls))}")
    if n > 1:
        print(f"  Std dev       : {pcts(stdev(pnls))}")
    print(f"  Total cap PnL : {pct(sum(cpnls))}")
    print(f"  Profit factor : {pf:.2f}")

    for d in ("CE", "PE"):
        leg = [r for r in rows if r["direction"] == d]
        if leg:
            lp = [r["pnl_pct"] for r in leg]
            lpf = leg_pf(rows, d)
            print(
                f"  {d}: n={len(leg):2d}  wr={sum(1 for p in lp if p>0)/len(leg)*100:.0f}%  "
                f"avg={pct(mean(lp))}  leg_PF={lpf:.2f}"
            )

    by_exit: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_exit[r["exit_reason"]].append(r["pnl_pct"])
    print("\n  ── Exit reasons ──")
    for reason, ps in sorted(by_exit.items(), key=lambda x: -len(x[1])):
        wr = sum(1 for p in ps if p > 0) / len(ps)
        print(f"    {reason:<25} n={len(ps):2d}  wr={wr*100:.0f}%  avg={pct(mean(ps))}")

    by_month: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_month[r["date"][:7]].append(r["cap_pnl_pct"])
    print("\n  ── Monthly (cap %) ──")
    for month in sorted(by_month):
        mp = by_month[month]
        print(f"    {month}  n={len(mp):2d}  total={pct(sum(mp))}")

    funnel = vote_funnel(db, run_id)
    print("\n  ── ML_ENTRY funnel ──")
    for k, v in funnel.items():
        print(f"    {k}: {v}")
    if funnel["ml_entry_votes"]:
        conv = funnel["closed_trades"] / funnel["ml_entry_votes"] * 100
        print(f"    vote_to_close_pct: {conv:.2f}%")

    blockers = blocker_histogram(db, run_id)
    if blockers:
        print("\n  ── Top blockers (decision traces, blocked) ──")
        for gate, cnt in blockers.most_common(12):
            print(f"    {gate:<30} {cnt}")

    checks = evaluate_gates(rows)
    print("\n  ── Pass / fail ──")
    all_ok = True
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"    [{mark}] {name:<28}  ({detail})")

    print("\n  " + ("OVERALL: PASS" if all_ok else "OVERALL: FAIL"))
    return all_ok


def main() -> int:
    argv = [a for a in sys.argv[1:] if a]
    run_id = argv[0] if argv and not argv[0].startswith("-") else ""
    label = argv[1] if len(argv) > 1 else os.getenv("OOS_LABEL", "oos")

    if not run_id:
        run_id = latest_run_id()

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)
    db = client[DB_NAME]
    rows = load_trades(db, run_id)
    ok = summarize(label, run_id, rows, db)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
