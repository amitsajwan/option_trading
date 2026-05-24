#!/usr/bin/env python3
"""Compare three OOS validation runs and emit solution-oriented analysis."""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from statistics import mean

try:
    from pymongo import MongoClient
except ImportError:
    print("pymongo required", file=sys.stderr)
    sys.exit(2)

# Reuse metrics from single-run analyzer
from analyze_oos_validation_run import (  # type: ignore
    CAPITAL,
    MIN_LEG_PF,
    MIN_PF,
    MIN_TRADES,
    blocker_histogram,
    evaluate_gates,
    leg_pf,
    load_trades,
    profit_factor,
    vote_funnel,
)

MONGO_URL = os.getenv("MONGO_URL", "mongodb://mongo:27017")
DB_NAME = os.getenv("MONGO_DB", "trading_ai")

WINDOWS = {
    "oos_primary": ("2024-05-01", "2024-07-31", "Primary OOS (pre in-sample)"),
    "oos_secondary": ("2023-05-01", "2023-07-31", "Secondary OOS (year prior)"),
    "in_sample_sanity": ("2024-08-01", "2024-10-31", "In-sample sanity (breakthrough window)"),
}


def summarize_run(db, label: str, run_id: str) -> dict:
    rows = load_trades(db, run_id)
    n = len(rows)
    pf = profit_factor(rows) if rows else 0.0
    ce_pf = leg_pf(rows, "CE")
    pe_pf = leg_pf(rows, "PE")
    ce_n = sum(1 for r in rows if r["direction"] == "CE")
    pe_n = sum(1 for r in rows if r["direction"] == "PE")
    cap_total = sum(r["cap_pnl_pct"] for r in rows)
    wr = sum(1 for r in rows if r["pnl_pct"] > 0) / n if n else 0.0

    by_exit: Counter[str] = Counter()
    for r in rows:
        by_exit[r["exit_reason"]] += 1

    by_month: dict[str, int] = defaultdict(int)
    for r in rows:
        by_month[r["date"][:7]] += 1

    by_strat: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_strat[r["strategy"]].append(r["pnl_pct"])

    checks = evaluate_gates(rows)
    passed = all(ok for _, ok, _ in checks)

    funnel = vote_funnel(db, run_id)
    blockers = blocker_histogram(db, run_id)

    time_n = by_exit.get("TIME_STOP", 0)
    trail_n = by_exit.get("TRAILING_STOP", 0)
    stop_n = sum(v for k, v in by_exit.items() if "STOP" in k and k != "TIME_STOP")

    return {
        "label": label,
        "run_id": run_id,
        "trades": n,
        "wr": wr,
        "pf": pf,
        "ce_pf": ce_pf,
        "pe_pf": pe_pf,
        "ce_n": ce_n,
        "pe_n": pe_n,
        "cap_total_pct": cap_total,
        "passed": passed,
        "checks": checks,
        "by_month": dict(by_month),
        "by_exit": dict(by_exit),
        "strategies": {k: {"n": len(v), "avg": mean(v) if v else 0} for k, v in by_strat.items()},
        "funnel": funnel,
        "blockers_top5": blockers.most_common(5),
        "time_stop_pct": time_n / n if n else 0,
        "trail_pct": trail_n / n if n else 0,
        "hard_stop_pct": stop_n / n if n else 0,
    }


def print_table(summaries: list[dict]) -> None:
    hdr = f"{'Window':<22} {'Trades':>6} {'WR':>5} {'PF':>5} {'Cap%':>7} {'CE_PF':>6} {'PE_PF':>6} {'PASS':>5}"
    print(hdr)
    print("-" * len(hdr))
    for s in summaries:
        ce = f"{s['ce_pf']:.2f}" if s["ce_pf"] is not None else "n/a"
        pe = f"{s['pe_pf']:.2f}" if s["pe_pf"] is not None else "n/a"
        print(
            f"{s['label']:<22} {s['trades']:>6} {s['wr']*100:>4.0f}% {s['pf']:>5.2f} "
            f"{s['cap_total_pct']*100:>+6.1f}% {ce:>6} {pe:>6} {'YES' if s['passed'] else 'NO':>5}"
        )


def solution_analysis(summaries: list[dict]) -> None:
    by_label = {s["label"]: s for s in summaries}
    primary = by_label.get("oos_primary")
    secondary = by_label.get("oos_secondary")
    sanity = by_label.get("in_sample_sanity")

    print("\n" + "=" * 72)
    print("  SOLUTION ANALYSIS")
    print("=" * 72)

    if not sanity:
        print("\n  Missing in_sample_sanity — cannot assess integration regression.")
        return

    # 1. Integration health
    print("\n  1) Integration regression check (in_sample_sanity)")
    if sanity["trades"] >= 55 and sanity["pf"] >= 1.5:
        print(
            f"     PASS: VM reproduces breakthrough band ({sanity['trades']} trades, PF {sanity['pf']:.2f})."
        )
        print("     Engine + ML_ENTRY primary voter wiring is intact on this host.")
        integration_ok = True
    elif sanity["trades"] >= 40 and sanity["pf"] >= 1.2:
        print(
            f"     WEAK: {sanity['trades']} trades, PF {sanity['pf']:.2f} — below breakthrough (61 / 1.98) but tradeable."
        )
        integration_ok = True
    else:
        print(
            f"     FAIL: {sanity['trades']} trades, PF {sanity['pf']:.2f} — fix deployment/config before OOS conclusions."
        )
        integration_ok = False

    # 2. OOS generalization
    print("\n  2) Out-of-sample generalization")
    oos_runs = [s for s in (primary, secondary) if s]
    oos_pass = sum(1 for s in oos_runs if s["passed"])
    if oos_pass == len(oos_runs) and oos_runs:
        print("     Both OOS windows PASS — edge generalizes; proceed to cap/TIME_STOP tuning (plan B/A).")
    elif oos_pass == 0 and oos_runs:
        print("     Both OOS windows FAIL — edge is likely **regime/window-specific**, not a wiring bug alone.")
    else:
        print("     Mixed OOS — one window passes, one fails; treat as **partial** generalization.")

    # 3. CE vs PE pattern
    print("\n  3) Direction / CE weakness pattern")
    ce_bad = [s for s in summaries if s["ce_pf"] is not None and s["ce_pf"] < 1.0 and s["ce_n"] >= 8]
    pe_ok = [s for s in summaries if s["pe_pf"] is not None and s["pe_pf"] >= 1.0]
    if ce_bad and len(ce_bad) >= 2:
        print("     Structural: **CE leg loses** in multiple windows while PE often holds up.")
        print("     Root: `_resolve_direction()` uses 5m fut momentum (no direction ML) → CE bias in chop/up-drifts.")
        print("     Fix class: direction quality, not entry HPO (entry HPO is orthogonal).")
    elif primary and primary["ce_pf"] is not None and primary["ce_pf"] < 1.0:
        print("     CE drag in primary OOS only — may be period-specific; confirm with secondary.")

    # 4. Blockers
    print("\n  4) Blocker budget (top issues across runs)")
    agg: Counter[str] = Counter()
    for s in summaries:
        for gate, cnt in s["blockers_top5"]:
            agg[gate] += cnt
    for gate, cnt in agg.most_common(6):
        note = ""
        if "no_entry_votes" in gate:
            note = " — brain consensus sees 0 CE/PE votes (ML silent + rules silent); not engine no_selection"
        elif gate == "risk_pause":
            note = " — consecutive-loss / DD pause; tune only after OOS pass"
        elif gate == "session_trade_cap":
            note = " — 6 trades/session default; tune only after OOS pass"
        print(f"     {gate:<40} {cnt:>5}{note}")

    # 5. Exit mix
    print("\n  5) Exit economics")
    for s in summaries:
        if s["trades"] < 10:
            continue
        print(
            f"     {s['label']:<22} TIME_STOP {s['time_stop_pct']*100:.0f}%  "
            f"TRAIL {s['trail_pct']*100:.0f}%  HARD_STOP {s['hard_stop_pct']*100:.0f}%"
        )

    # 6. Recommended actions (ordered)
    print("\n  6) Recommended actions (ordered)")
    recs: list[str] = []
    if not integration_ok:
        recs.append("Re-run in_sample_sanity after rebuild; confirm ENTRY_ML_MIN_PROB=0.65 and a133936 in image.")
    elif sanity and sanity["passed"] and oos_pass == 0:
        recs.append(
            "A. **Direction fix (priority):** Add lightweight direction filter for ML_ENTRY "
            "(e.g. require PE when fut_return_5m>0 and CE loses in-window, or enable direction-only bundle with holdout gate)."
        )
        recs.append(
            "B. **Brain gate bypass for ML_ENTRY profile:** Skip `consensus_gate:no_entry_votes` when ML_ENTRY would "
            "vote on next evaluation — reduces false blocks on silent rule days (343 blocks in OOS primary)."
        )
        recs.append(
            "C. **Regime calendar:** Tag May–Jul 2024 vs Aug–Oct; consider trading only windows that pass secondary+sanity."
        )
        recs.append("D. Do NOT lower ENTRY_ML_MIN_PROB to 'fix' OOS — that increases low-quality entries.")
        recs.append("E. Defer session_trade_cap / TIME_STOP tuning until A+B tested on oos_primary replay.")
    elif oos_pass >= 1:
        recs.append("Proceed with plan B: pilot RISK_MAX_SESSION_TRADES=8 on paper replay.")
        recs.append("Then plan A: TIME_STOP / MFE giveback on trail winners.")
    else:
        recs.append("Gather more data: extend OOS to full H1 2024 or 2023-H2 with same frozen config.")

    for i, r in enumerate(recs, 1):
        print(f"     {i}. {r}")

    print("\n  7) Monthly trade distribution")
    for s in summaries:
        if s["by_month"]:
            months = ", ".join(f"{m}:{n}" for m, n in sorted(s["by_month"].items()))
            print(f"     {s['label']:<22} {months}")


def main() -> int:
    # Args: label=run_id pairs or JSON file
    mapping: dict[str, str] = {}
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        mapping = json.loads(open(sys.argv[1], encoding="utf-8").read())
    else:
        args = sys.argv[1:]
        if len(args) % 2 != 0:
            print(
                "Usage: analyze_oos_validation_compare.py label run_id [label run_id ...]\n"
                "   or: analyze_oos_validation_compare.py /tmp/oos_validation_runs.json",
                file=sys.stderr,
            )
            return 2
        for i in range(0, len(args), 2):
            mapping[args[i]] = args[i + 1]

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)
    db = client[DB_NAME]

    summaries: list[dict] = []
    for label in ("oos_primary", "oos_secondary", "in_sample_sanity"):
        rid = mapping.get(label, "").strip()
        if not rid:
            print(f"WARN: missing run_id for {label}", file=sys.stderr)
            continue
        summaries.append(summarize_run(db, label, rid))

    if not summaries:
        return 2

    print("\n" + "=" * 72)
    print("  THREE-WINDOW COMPARISON  (frozen: prob>=0.65, ML_ENTRY primary voter)")
    print("=" * 72)
    print_table(summaries)

    for s in summaries:
        print(f"\n  --- {s['label']} ({s['run_id']}) ---")
        for gate, cnt in s["blockers_top5"]:
            print(f"    blocker {gate}: {cnt}")
        if s["strategies"]:
            print("    entry strategies:")
            for name, info in sorted(s["strategies"].items(), key=lambda x: -x[1]["n"]):
                print(f"      {name}: n={info['n']} avg_opt={info['avg']*100:+.1f}%")

    solution_analysis(summaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
