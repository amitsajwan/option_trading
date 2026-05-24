"""Daily regime feature builder for snapshots_ml_flat_v3.

Adds per-minute rows (constant within each trade_date):
  regime_rv20, regime_dist_sma20, regime_sma20_slope, regime_60d_return

Values for day T are computed from futures daily closes through T-1 (no lookahead).

Usage:
  python -m ml_pipeline_2.scripts.feature_builder.build_daily_regime_v3 --date 2024-10-31
  python -m ml_pipeline_2.scripts.feature_builder.build_daily_regime_v3 \\
      --start 2020-08-03 --end 2024-10-31

Environment:
  OPTION_TRADING_PARQUET_ROOT — parquet_data directory (optional)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

from ml_pipeline_2.scripts.feature_builder.regime_daily import (
    ALL_REGIME_COLUMNS,
    build_full_regime_table,
    load_daily_closes_from_flat_v3,
    load_futures_daily_closes,
    resolve_parquet_root,
)

logger = logging.getLogger(__name__)

_REGIME_CACHE: Optional[pd.DataFrame] = None


def _load_regime_table(parquet_root: Path, flat_v3_root: Path) -> pd.DataFrame:
    global _REGIME_CACHE
    if _REGIME_CACHE is not None:
        return _REGIME_CACHE

    daily = load_futures_daily_closes(parquet_root)
    if daily.empty:
        logger.warning("no futures parquet at %s; falling back to flat v3 closes", parquet_root / "futures")
        daily = load_daily_closes_from_flat_v3(flat_v3_root)
    if daily.empty:
        return pd.DataFrame(columns=["trade_date"] + REGIME_COLUMNS)

    _REGIME_CACHE = build_full_regime_table(daily, parquet_root=parquet_root)
    return _REGIME_CACHE


def build_one_day(
    trade_date: date,
    *,
    parquet_root: Path,
    flat_v3_root: Path,
    out_root: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Merge regime columns onto one day's flat v3 parquet."""
    iso_date = trade_date.isoformat()
    in_path = flat_v3_root / f"year={trade_date.year}" / f"{iso_date}.parquet"
    if not in_path.exists():
        return False, f"missing flat v3: {in_path}"

    out_root = out_root or flat_v3_root
    regime_table = _load_regime_table(parquet_root, flat_v3_root)
    if regime_table.empty:
        return False, "empty regime table (no futures or flat closes)"

    flat = pd.read_parquet(in_path)
    if flat.empty:
        return False, "empty flat v3 day"

    flat["trade_date"] = pd.to_datetime(flat["trade_date"]).dt.normalize()
    td = pd.Timestamp(trade_date).normalize()
    day_regime = regime_table[regime_table["trade_date"] == td]
    if day_regime.empty:
        for col in ALL_REGIME_COLUMNS:
            flat[col] = float("nan")
    else:
        flat = flat.drop(columns=[c for c in ALL_REGIME_COLUMNS if c in flat.columns], errors="ignore")
        flat = flat.merge(day_regime, on="trade_date", how="left")

    out_dir = out_root / f"year={trade_date.year}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{iso_date}.parquet"
    flat.to_parquet(out_path, index=False)

    cov = {c: float(flat[c].notna().mean()) for c in ALL_REGIME_COLUMNS if c in flat.columns}
    return True, f"wrote {len(flat)} rows to {out_path.name}, coverage={cov}"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--date", help="Single date YYYY-MM-DD")
    p.add_argument("--start", help="Backfill start inclusive")
    p.add_argument("--end", help="Backfill end inclusive")
    p.add_argument("--parquet-root", default=None, help="parquet_data root")
    p.add_argument(
        "--flat-v3-root",
        default=None,
        help="snapshots_ml_flat_v3 root (default: <parquet-root>/snapshots_ml_flat_v3)",
    )
    p.add_argument("--out-root", default=None, help="Output root (default: same as flat-v3-root)")
    p.add_argument(
        "--ingest-vix",
        action="store_true",
        help="Run India VIX CSV ingest to parquet before backfill (needs --vix-root)",
    )
    p.add_argument("--vix-root", default=None, help="hist_india_vix_*.csv directory for --ingest-vix")
    p.add_argument("--force-vix", action="store_true", help="Overwrite vix.parquet when using --ingest-vix")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parquet_root = resolve_parquet_root(args.parquet_root)
    flat_v3_root = Path(args.flat_v3_root) if args.flat_v3_root else parquet_root / "snapshots_ml_flat_v3"
    out_root = Path(args.out_root) if args.out_root else flat_v3_root

    if not flat_v3_root.exists():
        logger.error("flat v3 root not found: %s", flat_v3_root)
        return 1

    if args.ingest_vix:
        if not args.vix_root:
            p.error("--ingest-vix requires --vix-root")
        from snapshot_app.pipeline.normalize import normalize_vix_to_parquet

        vix_result = normalize_vix_to_parquet(
            raw_root=Path(args.vix_root).parent,
            parquet_base=parquet_root,
            vix_root=Path(args.vix_root),
            force=args.force_vix,
        )
        logger.info("vix ingest: %s", vix_result)

    global _REGIME_CACHE
    _REGIME_CACHE = None
    _load_regime_table(parquet_root, flat_v3_root)
    n_days = 0 if _REGIME_CACHE is None else len(_REGIME_CACHE)
    logger.info("regime table: %d trading days", n_days)

    if args.date and not (args.start or args.end):
        d = datetime.strptime(args.date, "%Y-%m-%d").date()
        ok, msg = build_one_day(d, parquet_root=parquet_root, flat_v3_root=flat_v3_root, out_root=out_root)
        logger.info("%s -> %s", d, msg)
        return 0 if ok else 1

    if not (args.start and args.end):
        p.error("pass --date OR (--start AND --end)")

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    cur, n_ok, n_skip = start, 0, 0
    while cur <= end:
        ok, msg = build_one_day(cur, parquet_root=parquet_root, flat_v3_root=flat_v3_root, out_root=out_root)
        if ok:
            n_ok += 1
            if n_ok % 50 == 0:
                logger.info("%s OK (%d days)", cur, n_ok)
        else:
            n_skip += 1
            logger.info("%s SKIP: %s", cur, msg)
        cur += timedelta(days=1)

    logger.info("done: %d OK, %d skipped", n_ok, n_skip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
