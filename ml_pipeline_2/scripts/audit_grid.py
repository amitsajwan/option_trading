#!/usr/bin/env python3
"""
Lightweight post-grid audit that summarizes each lane's holdout stats from summary.json.
- Input: --grid-root pointing to a run_staged_grid output directory (contains grid_summary.json, runs/*/summary.json)
- Output: Writes audit_summary.json under the grid root and prints a concise table to stdout.

Notes:
- This audit works without per-trade returns. It ranks by combined_holdout_summary metrics that are already
  persisted by the staged pipeline (trades, net_return_sum, profit_factor, max_drawdown_pct).
- If future runs persist per-trade returns, extend this tool to compute t-stats and bootstrap CIs.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List


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


def _rank(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Primary: higher net_return_sum; tie-breakers: higher profit_factor, lower max_drawdown
    return sorted(rows, key=lambda r: (r["net_return_sum"], r["profit_factor"], -r["max_drawdown_pct"]), reverse=True)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid-root", required=True, help="Path to a run_staged_grid output directory")
    args = ap.parse_args(argv)

    grid_root = Path(args.grid_root).resolve()
    if not grid_root.exists():
        print(f"[audit] grid root not found: {grid_root}", file=sys.stderr)
        return 2

    run_dirs = sorted([p for p in (grid_root / "runs").glob("*/") if (p / "summary.json").exists()])
    rows = [_row_from_summary(p / "summary.json") for p in run_dirs]

    ranked = _rank(rows)
    audit = {
        "grid_root": str(grid_root),
        "lanes": ranked,
        "top": ranked[0] if ranked else None,
        "stats": {
            "lanes_total": len(rows),
            "lanes_with_trades": sum(1 for r in rows if r["trades"] > 0),
            "median_trades": int(sorted(r["trades"] for r in rows)[len(rows)//2]) if rows else 0,
        },
    }
    out_path = grid_root / "audit_summary.json"
    out_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")

    # Console table (concise)
    print("rank  trades  net_sum%  pf   mdd%   stage2_auc  summary")
    for i, r in enumerate(ranked[:12], start=1):
        print(f"{i:>4}  {r['trades']:>6}  {r['net_return_sum']*100:>7.2f}  {r['profit_factor']:>4.2f}  {r['max_drawdown_pct']*100:>6.2f}  "
              f"{(r['stage2_cv_auc'] if not math.isnan(r['stage2_cv_auc']) else 0.0):>9.3f}  {Path(r['summary_path']).parent.name}")
    print(f"[audit] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
