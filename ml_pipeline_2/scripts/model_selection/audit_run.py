"""Statistical audit on a single per-trade parquet/csv file.

Computes the canonical edge gates the rest of the pipeline keys off:

    G1 (statistical edge):
        - t-statistic on per-trade returns > 2.0
        - bootstrap 95% CI lower bound > 0
        - net-without-top-5-days >= 0 (outlier-survival)

    G2 (trade-rate sanity):
        - trades total in [min_trades, max_trades]
        - win rate >= 0.55

A cell PASSES if BOTH gates pass. The script is invoked once per cell by
the orchestrator and writes audit.json next to the input trades file.

Usage:
    python audit_run.py \
        --trades path/to/trades.parquet \
        --return-col pnl_pct \
        --date-col trade_date \
        --min-trades 80 --max-trades 500 \
        --output audit.json

Exit code 0 always (audit always writes a result, even on FAIL — the caller
inspects audit.json["passed"]).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional


def _t_stats(returns: List[float]) -> Dict[str, float]:
    n = len(returns)
    if n == 0:
        return {"n": 0, "mean": 0.0, "std": 0.0, "se": 0.0, "t": 0.0, "p": 1.0}
    mean = sum(returns) / n
    if n == 1:
        std = 0.0
    else:
        var = sum((r - mean) ** 2 for r in returns) / (n - 1)
        std = math.sqrt(var)
    se = std / math.sqrt(n) if n > 0 else 0.0
    t = mean / se if se > 0 else 0.0
    # Two-tailed normal-approx p-value (large n)
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))) if t else 1.0
    return {"n": n, "mean": mean, "std": std, "se": se, "t": t, "p": p}


def _bootstrap_ci(returns: List[float], iters: int = 2000, seed: int = 42) -> Dict[str, float]:
    if len(returns) < 2:
        return {"ci_lo": 0.0, "ci_hi": 0.0}
    rng = random.Random(seed)
    n = len(returns)
    means: List[float] = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            s += rng.choice(returns)
        means.append(s / n)
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    return {"ci_lo": lo, "ci_hi": hi}


def _daily_decomp(records: List[Dict[str, float]]) -> Dict[str, object]:
    """records: list of {'date': 'YYYY-MM-DD', 'ret': float}"""
    if not records:
        return {"available": False, "reason": "empty"}
    by_day: Dict[str, float] = {}
    for r in records:
        d = r.get("date")
        if not d:
            continue
        by_day[d] = by_day.get(d, 0.0) + float(r.get("ret", 0))
    if not by_day:
        return {"available": False, "reason": "no_dates"}
    daily = sorted(by_day.items())
    total = sum(p for _, p in daily)
    pnls = sorted([p for _, p in daily], reverse=True)
    top1 = pnls[0]
    top5 = sum(pnls[:5])
    worst5 = sum(pnls[-5:])
    profitable = sum(1 for _, p in daily if p > 0)
    # Max drawdown on cumulative daily series
    peak = -math.inf
    max_dd = 0.0
    cum = 0.0
    for _, p in daily:
        cum += p
        if cum > peak:
            peak = cum
        if cum - peak < max_dd:
            max_dd = cum - peak
    return {
        "available": True,
        "days": len(daily),
        "profitable_days": profitable,
        "profitable_day_rate": profitable / len(daily),
        "top1_day_share_of_net": (top1 / total) if total != 0 else 0.0,
        "top5_days_share_of_net": (top5 / total) if total != 0 else 0.0,
        "net_without_top1_day": total - top1,
        "net_without_top5_days": total - top5,
        "worst5_days_sum": worst5,
        "max_drawdown": max_dd,
    }


def audit(
    trades_path: Path,
    return_col: str,
    date_col: str,
    min_trades: int,
    max_trades: int,
    min_win_rate: float,
    t_min: float,
    ci_must_exclude_zero: bool,
    outlier_survival_must_be_nonneg: bool,
) -> Dict[str, object]:
    try:
        import pandas as pd
    except Exception as exc:
        return {"available": False, "reason": f"pandas_unavailable: {exc}"}

    if not trades_path.exists():
        return {"available": False, "reason": f"trades_file_missing: {trades_path}"}

    if trades_path.suffix == ".parquet":
        df = pd.read_parquet(trades_path)
    elif trades_path.suffix in (".csv", ".tsv"):
        sep = "\t" if trades_path.suffix == ".tsv" else ","
        df = pd.read_csv(trades_path, sep=sep)
    else:
        return {"available": False, "reason": f"unsupported_format: {trades_path.suffix}"}

    if return_col not in df.columns:
        return {"available": False, "reason": f"return_col_missing: {return_col}",
                "columns_seen": list(df.columns)[:30]}

    returns = [float(x) for x in df[return_col].dropna().tolist()]
    n = len(returns)
    if n == 0:
        return {"available": True, "passed": False, "n_trades": 0,
                "reason": "no_trades", "gates": {"G1": False, "G2": False}}

    # Win rate
    wins = sum(1 for r in returns if r > 0)
    win_rate = wins / n

    # Daily decomp
    records = []
    if date_col in df.columns:
        for date, ret in zip(df[date_col].astype(str).str.slice(0, 10).tolist(),
                             df[return_col].tolist()):
            try:
                records.append({"date": date, "ret": float(ret)})
            except (TypeError, ValueError):
                continue
    daily = _daily_decomp(records)

    # Per-trade stats
    ts = _t_stats(returns)
    ci = _bootstrap_ci(returns)

    # Gates
    cond_t = bool(ts["t"] > t_min)
    cond_ci = bool(ci["ci_lo"] > 0) if ci_must_exclude_zero else True
    cond_outlier = (
        bool(daily.get("net_without_top5_days", 0) >= 0)
        if outlier_survival_must_be_nonneg and daily.get("available")
        else True
    )
    cond_count = bool(min_trades <= n <= max_trades)
    cond_wr = bool(win_rate >= min_win_rate)

    g1 = cond_t and cond_ci and cond_outlier
    g2 = cond_count and cond_wr
    passed = g1 and g2

    return {
        "available": True,
        "passed": passed,
        "gates": {
            "G1_statistical_edge": g1,
            "G2_trade_rate_sanity": g2,
            "details": {
                "t_gt_2": cond_t,
                "ci_excludes_zero": cond_ci,
                "outlier_survival": cond_outlier,
                "trade_count_in_range": cond_count,
                "win_rate_min": cond_wr,
            },
        },
        "n_trades": n,
        "stats": ts,
        "ci": ci,
        "win_rate": win_rate,
        "daily": daily,
        "thresholds": {
            "t_min": t_min,
            "ci_must_exclude_zero": ci_must_exclude_zero,
            "min_trades": min_trades,
            "max_trades": max_trades,
            "min_win_rate": min_win_rate,
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--trades", required=True, help="Path to per-trade parquet/csv with one trade per row")
    ap.add_argument("--return-col", default="pnl_pct", help="Column name for per-trade return (decimal, not %)")
    ap.add_argument("--date-col", default="trade_date", help="Column name for trade date (used for daily decomp)")
    ap.add_argument("--min-trades", type=int, default=80, help="Trade-count lower bound for G2")
    ap.add_argument("--max-trades", type=int, default=500, help="Trade-count upper bound for G2")
    ap.add_argument("--min-win-rate", type=float, default=0.55, help="Win-rate lower bound for G2")
    ap.add_argument("--t-min", type=float, default=2.0, help="t-statistic threshold for G1")
    ap.add_argument("--allow-ci-include-zero", action="store_true",
                    help="If set, G1 does NOT require CI to strictly exclude zero (use sparingly)")
    ap.add_argument("--allow-outlier-driven", action="store_true",
                    help="If set, G1 does NOT require net-without-top-5 to be non-negative")
    ap.add_argument("--output", default=None, help="Where to write audit.json (default: alongside trades file)")
    args = ap.parse_args(argv)

    trades_path = Path(args.trades).resolve()
    out_path = Path(args.output) if args.output else trades_path.with_name("audit.json")

    result = audit(
        trades_path=trades_path,
        return_col=args.return_col,
        date_col=args.date_col,
        min_trades=args.min_trades,
        max_trades=args.max_trades,
        min_win_rate=args.min_win_rate,
        t_min=args.t_min,
        ci_must_exclude_zero=not args.allow_ci_include_zero,
        outlier_survival_must_be_nonneg=not args.allow_outlier_driven,
    )
    result["trades_path"] = str(trades_path)
    out_path.write_text(json.dumps(result, indent=2))

    # Console summary
    if result.get("available") and "stats" in result:
        s = result["stats"]
        ci = result["ci"]
        passed = result.get("passed", False)
        print(f"[audit] {trades_path.name}: "
              f"n={s['n']}  t={s['t']:+.2f}  ci=[{ci['ci_lo']:+.4f},{ci['ci_hi']:+.4f}]  "
              f"wr={result.get('win_rate', 0):.1%}  -> {'PASS' if passed else 'FAIL'}")
    else:
        print(f"[audit] {trades_path.name}: unavailable ({result.get('reason')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
