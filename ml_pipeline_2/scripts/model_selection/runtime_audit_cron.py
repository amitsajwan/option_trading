"""Runtime audit cron — apply the model-selection audit harness to LIVE
JSONL trades on a rolling window so we detect edge decay over time.

Reads:
  /opt/option_trading/.run/strategy_app{_historical}/positions.jsonl
    (configurable via --run-dir)

For each rolling lookback window (default 30 days), filters to
POSITION_CLOSE events of the currently-deployed run/recipe, extracts
per-trade pnl_pct, builds an in-memory DataFrame, and runs the same
audit_run.audit() function the offline model-selection pipeline uses.

Appends one line per invocation to:
  <run-dir>/runtime_audit_history.jsonl

This lets us answer "is the live model still showing the offline edge
the audit harness blessed at deployment?" — and detects the moment it
stops, before the operator notices via P&L.

Designed to be invoked nightly from cron (or manually). Idempotent in
the sense that running it twice in the same hour just appends two
identical-ish records; the latest is always authoritative.

Usage:
    # nightly cron entry (no args = sensible defaults)
    python -m ml_pipeline_2.scripts.model_selection.runtime_audit_cron \\
        --run-dir /opt/option_trading/.run/strategy_app_historical \\
        --lookback-days 30

    # one-shot manual invocation
    python -m ml_pipeline_2.scripts.model_selection.runtime_audit_cron
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_RUN_DIR = Path("/opt/option_trading/.run/strategy_app_historical")
HISTORY_FILENAME = "runtime_audit_history.jsonl"


def _load_closed_trades(positions_path: Path, lookback_start: str) -> "pd.DataFrame":
    """Read positions.jsonl, filter to POSITION_CLOSE within the lookback
    window, extract per-trade fields needed by the audit harness.
    """
    import pandas as pd

    if not positions_path.exists():
        return pd.DataFrame()

    rows: List[dict] = []
    lookback_start_compact = lookback_start.replace("-", "")
    with positions_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event") != "POSITION_CLOSE":
                continue
            snap_id = str(rec.get("snapshot_id") or "")
            if len(snap_id) < 8 or snap_id[:8] < lookback_start_compact:
                continue
            pnl = rec.get("pnl_pct")
            try:
                pnl_f = float(pnl) if pnl is not None else None
            except (TypeError, ValueError):
                pnl_f = None
            if pnl_f is None:
                continue
            # Derive trade_date YYYY-MM-DD from snapshot_id
            trade_date_iso = f"{snap_id[:4]}-{snap_id[4:6]}-{snap_id[6:8]}"
            rows.append({
                "trade_date": trade_date_iso,
                "pnl_pct": pnl_f,
                "snapshot_id": snap_id,
                "run_id": rec.get("run_id"),
                "exit_reason": rec.get("exit_reason"),
            })
    return pd.DataFrame(rows)


def audit_runtime(
    run_dir: Path,
    *,
    lookback_days: int = 30,
    audit_thresholds: Optional[dict] = None,
) -> dict:
    """Run the audit harness against recent live trades.

    Returns the audit dict (same shape as audit_run.audit returns) PLUS
    metadata about the window and run.
    """
    import pandas as pd
    from .audit_run import audit  # type: ignore

    if audit_thresholds is None:
        audit_thresholds = {}

    positions_path = run_dir / "positions.jsonl"
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=lookback_days)
    lookback_start = cutoff.isoformat()
    df = _load_closed_trades(positions_path, lookback_start)

    record: dict = {
        "audited_at": datetime.now(timezone.utc).isoformat() + "Z",
        "run_dir": str(run_dir),
        "lookback_days": int(lookback_days),
        "lookback_start": lookback_start,
        "n_trades_in_window": int(len(df)),
    }

    if len(df) == 0:
        record["available"] = False
        record["reason"] = "no_closed_trades_in_window"
        return record

    # Distinct run_ids in window (model identity drift detector)
    if "run_id" in df.columns:
        record["run_ids_in_window"] = sorted({str(r) for r in df["run_id"].dropna().unique().tolist()})

    # Write a temp parquet for the audit function
    tmp_parquet = run_dir / "_runtime_audit_tmp.parquet"
    try:
        df.to_parquet(tmp_parquet)
        result = audit(
            trades_path=tmp_parquet,
            return_col="pnl_pct",
            date_col="trade_date",
            min_trades=int(audit_thresholds.get("min_trades", 30)),
            max_trades=int(audit_thresholds.get("max_trades", 5000)),
            min_win_rate=float(audit_thresholds.get("min_win_rate", 0.50)),
            t_min=float(audit_thresholds.get("t_min", 2.0)),
            ci_must_exclude_zero=bool(audit_thresholds.get("ci_must_exclude_zero", True)),
            outlier_survival_must_be_nonneg=bool(audit_thresholds.get("outlier_survival_must_be_nonneg", True)),
        )
        record.update(result)
    finally:
        try:
            tmp_parquet.unlink(missing_ok=True)
        except Exception:
            pass

    return record


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR),
                   help=f"Strategy run_dir to audit (default {DEFAULT_RUN_DIR})")
    p.add_argument("--lookback-days", type=int, default=30,
                   help="Rolling window in calendar days (default 30)")
    p.add_argument("--min-trades", type=int, default=30,
                   help="Audit gate: minimum trades in window (default 30 — looser than offline gate because live samples are smaller)")
    p.add_argument("--max-trades", type=int, default=5000)
    p.add_argument("--min-win-rate", type=float, default=0.50)
    p.add_argument("--t-min", type=float, default=2.0)
    p.add_argument("--quiet", action="store_true", help="Suppress stdout summary; only append to history")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.exists():
        print(f"FATAL: run_dir does not exist: {run_dir}", file=sys.stderr)
        return 1

    audit_thresholds = {
        "min_trades": args.min_trades,
        "max_trades": args.max_trades,
        "min_win_rate": args.min_win_rate,
        "t_min": args.t_min,
        "ci_must_exclude_zero": True,
        "outlier_survival_must_be_nonneg": True,
    }

    record = audit_runtime(run_dir, lookback_days=args.lookback_days, audit_thresholds=audit_thresholds)

    history_path = run_dir / HISTORY_FILENAME
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")

    if not args.quiet:
        stats = record.get("stats") or {}
        ci = record.get("ci") or {}
        verdict = "PASS" if record.get("passed") else "FAIL"
        n = record.get("n_trades", record.get("n_trades_in_window", 0))
        if record.get("available", True) and stats:
            print(
                f"[runtime-audit] window={args.lookback_days}d n={n} "
                f"t={stats.get('t', 0):+.2f} "
                f"ci=[{ci.get('ci_lo', 0)*100:+.2f}%,{ci.get('ci_hi', 0)*100:+.2f}%] "
                f"wr={record.get('win_rate', 0)*100:.1f}% "
                f"-> {verdict}"
            )
        else:
            print(f"[runtime-audit] window={args.lookback_days}d n={n} -> "
                  f"{record.get('reason', 'unavailable')}")
        print(f"[runtime-audit] appended to {history_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
