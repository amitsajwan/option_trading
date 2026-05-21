#!/usr/bin/env python3
"""Compare rules backtest vs runtime eval trades for one calendar day."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _fetch_runtime_trades(*, api_base: str, run_id: str, trade_date: str) -> list[dict]:
    params = (
        f"dataset=historical&date_from={trade_date}&date_to={trade_date}"
        f"&run_id={run_id}&page_size=50&sort_by=exit_time&sort_dir=asc"
    )
    url = f"{api_base.rstrip('/')}/api/strategy/evaluation/trades?{params}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        payload = json.loads(resp.read().decode())
    return list(payload.get("rows") or [])


def _rules_trades(*, rule_path: Path, trade_date: str, out_dir: Path) -> list[dict]:
    sys.path.insert(0, str(REPO_ROOT))
    from ml_pipeline_2.scripts.rules_pipeline.run_backtest import run_backtest

    rule_dict = json.loads(rule_path.read_text(encoding="utf-8"))
    run_backtest(
        rule_dict,
        trade_date,
        trade_date,
        out_dir,
        exit_mode="mechanical",
    )
    import pandas as pd

    frame = pd.read_parquet(out_dir / "trades.parquet")
    rows: list[dict] = []
    for _, row in frame.iterrows():
        rows.append(
            {
                "trade_date": str(row.get("trade_date") or trade_date),
                "exit_reason": str(row.get("exit_reason") or ""),
                "pnl_pct_net": float(row.get("net_pnl_pct") or 0.0),
                "mfe_pct": float(row.get("mfe_pct") or 0.0),
                "mae_pct": float(row.get("mae_pct") or 0.0),
                "bars_held": int(row.get("bars_held") or 0),
            }
        )
    return rows


def _fmt_trade(row: dict, *, source: str) -> str:
    return (
        f"{source}: exit={row.get('exit_reason')} "
        f"opt_pnl={float(row.get('pnl_pct_net') or 0):.2%} "
        f"mae={float(row.get('mae_pct') or 0):.2%} "
        f"mfe={float(row.get('mfe_pct') or 0):.2%}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="IST trade date YYYY-MM-DD")
    parser.add_argument("--rule", required=True, help="Path to rule JSON")
    parser.add_argument("--run-id", required=True, help="Runtime eval run_id")
    parser.add_argument("--api-base", default="http://127.0.0.1:8008")
    parser.add_argument(
        "--output-dir",
        default="/tmp/parity_rules",
        help="Scratch dir for rules backtest artifacts",
    )
    args = parser.parse_args()

    rule_path = Path(args.rule)
    if not rule_path.is_absolute():
        rule_path = (REPO_ROOT / rule_path).resolve()
    out_dir = Path(args.output_dir) / f"{rule_path.stem}_{args.date}"

    print(f"=== parity {args.date} rule={rule_path.name} run={args.run_id} ===", flush=True)

    try:
        rules_rows = _rules_trades(rule_path=rule_path, trade_date=args.date, out_dir=out_dir)
    except Exception as exc:
        print(f"RULES_ERROR: {exc}", flush=True)
        return 1

    try:
        runtime_rows = _fetch_runtime_trades(
            api_base=args.api_base,
            run_id=args.run_id,
            trade_date=args.date,
        )
    except Exception as exc:
        print(f"RUNTIME_ERROR: {exc}", flush=True)
        return 1

    print(f"rules_trades={len(rules_rows)} runtime_trades={len(runtime_rows)}", flush=True)
    for idx, row in enumerate(rules_rows):
        print(_fmt_trade(row, source=f"rules[{idx}]"), flush=True)
    for idx, row in enumerate(runtime_rows):
        print(
            _fmt_trade(
                {
                    "exit_reason": row.get("exit_reason"),
                    "pnl_pct_net": row.get("pnl_pct_net"),
                    "mae_pct": row.get("mae_pct"),
                    "mfe_pct": row.get("mfe_pct"),
                },
                source=f"runtime[{idx}]",
            ),
            flush=True,
        )

    if len(rules_rows) != len(runtime_rows):
        print(
            f"MISMATCH count rules={len(rules_rows)} runtime={len(runtime_rows)}",
            flush=True,
        )
        return 2

    mismatches = 0
    for rules_row, runtime_row in zip(rules_rows, runtime_rows, strict=False):
        r_exit = str(rules_row.get("exit_reason") or "").upper()
        t_exit = str(runtime_row.get("exit_reason") or "").upper()
        r_pnl = float(rules_row.get("pnl_pct_net") or 0.0)
        t_pnl = float(runtime_row.get("pnl_pct_net") or 0.0)
        if r_exit != t_exit or abs(r_pnl - t_pnl) > 0.05:
            mismatches += 1
            print(
                f"MISMATCH pair: rules exit={r_exit} pnl={r_pnl:.2%} "
                f"vs runtime exit={t_exit} pnl={t_pnl:.2%}",
                flush=True,
            )

    if mismatches:
        print(f"PARITY_FAIL mismatched_pairs={mismatches}", flush=True)
        return 2
    print("PARITY_OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
