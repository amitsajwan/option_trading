#!/usr/bin/env python3
"""Post-grid audit with proper statistical significance testing.

For each lane in a run_staged_grid output directory this tool produces:
  - Summary-level ranking (trades, net_return_sum, profit_factor, max_drawdown_pct)
  - Per-trade statistical audit when ranked_trades_holdout.parquet is present:
      * t-statistic on per-trade returns vs zero
      * approximate p-value (normal approx)
      * bootstrap 95% confidence interval for the mean
      * outlier decomposition: % of net contributed by top-1 / top-5 days,
        and the realised net if those days are removed
      * max drawdown across the daily-aggregated series
      * PASS flag (t > 2 AND CI strictly above zero AND net-without-top-5-days >= 0)

Why this matters
----------------
The previous audit ranked lanes purely by sum-of-net-returns, which is
what produced the false "+42x improvement" / "+6.27 net edge" claims.
Sign-counting and sum-ranking pass strategies whose entire P&L is driven
by 1-5 lucky days out of dozens; that's not edge, that's outlier survival.
Every promotion gate must now pass the same per-trade significance bar.

Inputs
------
  --grid-root  Directory produced by run_staged_grid (contains grid_summary.json
               and runs/<lane>/summary.json plus, for each lane, an
               analysis/stage12_confidence_execution/ranked_trades_holdout.parquet
               file when the pipeline persists per-trade returns).

Outputs
-------
  - audit_summary.json under the grid root with both summary-level and
    per-trade fields for every lane.
  - Console table sorted by per-trade evidence (PASS lanes first, then by t-stat).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _load_json(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _coerce_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _row_from_summary(summary_path: Path) -> Dict[str, Any]:
    s = _load_json(summary_path)
    hold = (((s.get("holdout_reports") or {}).get("stage3") or {}).get("combined_holdout_summary") or {})
    gates = dict(s.get("gates") or {})
    stage2_cv = dict((s.get("cv_prechecks") or {}).get("stage2_cv") or {})
    return {
        "summary_path": str(summary_path.resolve()),
        "run_dir": str(summary_path.parent.resolve()),
        "trades": int(_coerce_float(hold.get("trades"), 0)),
        "net_return_sum": _coerce_float(hold.get("net_return_sum"), 0.0),
        "profit_factor": _coerce_float(hold.get("profit_factor"), 0.0),
        "max_drawdown_pct": _coerce_float(hold.get("max_drawdown_pct"), 0.0),
        "stage2_cv_auc": _coerce_float(stage2_cv.get("roc_auc"), math.nan),
        "stage2_cv_brier": _coerce_float(stage2_cv.get("brier"), math.nan),
        "stage3_gate_passed": bool(((gates.get("stage3") or {}).get("passed"))),
        "combined_gate_passed": bool(((gates.get("combined") or {}).get("passed"))),
    }


# -----------------------------------------------------------------------------
# Per-trade statistical audit
# -----------------------------------------------------------------------------


def _find_per_trade_parquet(run_dir: Path) -> Optional[Path]:
    """Search standard locations for the per-trade holdout returns parquet."""
    candidates = [
        run_dir / "analysis" / "stage12_confidence_execution" / "ranked_trades_holdout.parquet",
        run_dir / "analysis" / "ranked_trades_holdout.parquet",
        run_dir / "ranked_trades_holdout.parquet",
    ]
    for path in candidates:
        if path.exists():
            return path
    # Last resort: a wider scan but only within run_dir
    matches = list(run_dir.rglob("ranked_trades_holdout.parquet"))
    return matches[0] if matches else None


def _select_return_column(columns: List[str]) -> Optional[str]:
    """Pick the best column representing the per-trade net return.

    Preference order:
      1. oracle_selected_side_return — what the trade actually realised (after cost)
      2. selected_side_net_return  — model-selection variant
      3. net_return                — generic fallback
    """
    preferred = ["oracle_selected_side_return", "selected_side_net_return", "net_return"]
    for name in preferred:
        if name in columns:
            return name
    return None


def _per_trade_stats(returns: List[float]) -> Dict[str, Any]:
    n = len(returns)
    if n == 0:
        return {"n_trades": 0, "available": False, "reason": "no_trades"}
    mean = sum(returns) / n
    if n == 1:
        std = 0.0
    else:
        m = mean
        var = sum((r - m) ** 2 for r in returns) / (n - 1)
        std = math.sqrt(var)
    se = std / math.sqrt(n) if n > 0 else 0.0
    t_stat = mean / se if se > 0 else 0.0
    # Normal-approx two-tailed p-value
    p_value = 2 * (1 - 0.5 * (1 + math.erf(abs(t_stat) / math.sqrt(2)))) if t_stat else 1.0
    # Bootstrap 95% CI for the mean
    rng = random.Random(42)
    boot_means: List[float] = []
    for _ in range(2000):
        s = 0.0
        for _ in range(n):
            s += rng.choice(returns)
        boot_means.append(s / n)
    boot_means.sort()
    ci_lo = boot_means[int(0.025 * len(boot_means))]
    ci_hi = boot_means[int(0.975 * len(boot_means))]
    return {
        "n_trades": n,
        "available": True,
        "mean": mean,
        "std": std,
        "stderr": se,
        "t_stat": t_stat,
        "p_value": p_value,
        "ci95_lo": ci_lo,
        "ci95_hi": ci_hi,
        "ci_contains_zero": ci_lo < 0 < ci_hi,
    }


def _daily_decomp(records: List[Tuple[str, float]]) -> Dict[str, Any]:
    """Aggregate per-trade returns by trade_date and compute outlier metrics."""
    if not records:
        return {"available": False, "reason": "no_records"}
    by_day: Dict[str, float] = {}
    for date, ret in records:
        if not date:
            continue
        by_day[date] = by_day.get(date, 0.0) + ret
    if not by_day:
        return {"available": False, "reason": "no_dates"}
    daily = sorted(by_day.items())
    total = sum(p for _, p in daily)
    daily_sorted_desc = sorted([p for _, p in daily], reverse=True)
    top1 = daily_sorted_desc[0]
    top5 = sum(daily_sorted_desc[:5])
    worst5 = sum(daily_sorted_desc[-5:])
    profitable_days = sum(1 for _, p in daily if p > 0)
    # max drawdown on cumulative daily series
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
        "profitable_days": profitable_days,
        "profitable_day_rate": profitable_days / len(daily) if daily else 0.0,
        "top1_day_return": top1,
        "top1_day_share_of_net": top1 / total if total != 0 else 0.0,
        "top5_days_sum": top5,
        "top5_days_share_of_net": top5 / total if total != 0 else 0.0,
        "net_without_top1_day": total - top1,
        "net_without_top5_days": total - top5,
        "worst5_days_sum": worst5,
        "max_drawdown": max_dd,
    }


def _audit_per_trade(run_dir: Path) -> Dict[str, Any]:
    parquet_path = _find_per_trade_parquet(run_dir)
    if parquet_path is None:
        return {"available": False, "reason": "no_per_trade_parquet"}
    try:
        import pandas as pd  # noqa: PLC0415 — keep optional
    except Exception:
        return {"available": False, "reason": "pandas_unavailable"}
    try:
        df = pd.read_parquet(parquet_path)
    except Exception as exc:
        return {"available": False, "reason": f"parquet_read_failed:{exc}"}
    return_col = _select_return_column(list(df.columns))
    if return_col is None:
        return {"available": False, "reason": "no_return_column",
                "columns_seen": list(df.columns)[:20]}
    # Keep only the rows that represent fired trades (non-null return)
    series = df[return_col]
    mask = series.notna()
    if "selected_side" in df.columns:
        # A blank selected_side means no trade was fired on that snapshot
        mask = mask & df["selected_side"].astype(str).str.strip().ne("")
    fired = df[mask]
    returns = [float(x) for x in fired[return_col].tolist()]
    stats = _per_trade_stats(returns)
    # Daily decomp
    dates: List[str] = []
    if "trade_date" in fired.columns:
        dates = [str(x)[:10] for x in fired["trade_date"].tolist()]
    elif "timestamp" in fired.columns:
        dates = [str(x)[:10] for x in fired["timestamp"].tolist()]
    decomp = _daily_decomp(list(zip(dates, returns)))
    # PASS gate: t > 2 AND CI excludes zero AND net-without-top-5 >= 0
    passed = False
    if stats.get("available"):
        cond_t = bool(stats.get("t_stat", 0) > 2.0)
        cond_ci = bool(stats.get("ci95_lo", -1) > 0.0)
        cond_outlier = bool(decomp.get("net_without_top5_days", 0) >= 0) if decomp.get("available") else False
        passed = cond_t and cond_ci and cond_outlier
    return {
        "available": True,
        "parquet_path": str(parquet_path),
        "return_column": return_col,
        "stats": stats,
        "daily": decomp,
        "passed_significance_bar": passed,
    }


def _rank(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Primary: passed_significance_bar (True first)
    # Secondary: t_stat desc
    # Tertiary: net_return_sum desc
    def key(r: Dict[str, Any]) -> Tuple[int, float, float]:
        per = r.get("per_trade_audit") or {}
        stats = (per.get("stats") or {}) if per.get("available") else {}
        passed = int(bool(per.get("passed_significance_bar", False)))
        t_stat = float(stats.get("t_stat") or 0.0)
        net = float(r.get("net_return_sum") or 0.0)
        return (passed, t_stat, net)
    return sorted(rows, key=key, reverse=True)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid-root", required=True, help="Path to a run_staged_grid output directory")
    args = ap.parse_args(argv)

    grid_root = Path(args.grid_root).resolve()
    if not grid_root.exists():
        print(f"[audit] grid root not found: {grid_root}", file=sys.stderr)
        return 2

    run_dirs = sorted([p for p in (grid_root / "runs").glob("*/") if (p / "summary.json").exists()])
    rows: List[Dict[str, Any]] = []
    for run_dir in run_dirs:
        row = _row_from_summary(run_dir / "summary.json")
        row["per_trade_audit"] = _audit_per_trade(run_dir)
        rows.append(row)

    ranked = _rank(rows)
    audit = {
        "grid_root": str(grid_root),
        "lanes": ranked,
        "top": ranked[0] if ranked else None,
        "stats": {
            "lanes_total": len(rows),
            "lanes_with_trades": sum(1 for r in rows if r["trades"] > 0),
            "lanes_with_per_trade_data": sum(1 for r in rows if (r.get("per_trade_audit") or {}).get("available")),
            "lanes_passing_significance_bar": sum(1 for r in rows if (r.get("per_trade_audit") or {}).get("passed_significance_bar")),
            "median_trades": int(sorted(r["trades"] for r in rows)[len(rows)//2]) if rows else 0,
        },
    }
    out_path = grid_root / "audit_summary.json"
    out_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")

    # Console table (concise)
    print("rank  PASS  trades   net_sum%   pf    t      p      ci95           top5_share   without_top5  lane")
    for i, r in enumerate(ranked[:20], start=1):
        per = r.get("per_trade_audit") or {}
        stats = (per.get("stats") or {}) if per.get("available") else {}
        daily = (per.get("daily") or {}) if per.get("available") else {}
        passed = "Y" if per.get("passed_significance_bar") else "n"
        t_str = f"{stats.get('t_stat'):.2f}" if stats.get('available') else "--"
        p_str = f"{stats.get('p_value'):.3f}" if stats.get('available') else "--"
        ci_str = (f"[{stats.get('ci95_lo')*100:+.2f},{stats.get('ci95_hi')*100:+.2f}]"
                  if stats.get('available') else "[no-data]")
        top5_share = f"{daily.get('top5_days_share_of_net', 0)*100:.0f}%" if daily.get('available') else "--"
        wo5 = f"{daily.get('net_without_top5_days', 0)*100:+.2f}%" if daily.get('available') else "--"
        lane = Path(r['summary_path']).parent.name
        print(f"{i:>4}    {passed}   {r['trades']:>5}   {r['net_return_sum']*100:>+7.2f}  "
              f"{r['profit_factor']:>4.2f}  {t_str:>5}  {p_str:>5}  {ci_str:<14}  {top5_share:>6}  "
              f"{wo5:>10}  {lane}")
    print(f"[audit] wrote {out_path}")
    print(f"[audit] {audit['stats']['lanes_passing_significance_bar']}/{audit['stats']['lanes_total']} lanes passed the significance bar "
          "(t>2 AND CI>0 AND net-without-top-5-days>=0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
